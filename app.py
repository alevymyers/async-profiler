import csv
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date, datetime, timedelta
from io import StringIO

import requests
from flask import Flask, jsonify, redirect, render_template, request

# yfinance for equities fallback
try:
    import yfinance as yf
except Exception:
    yf = None

app = Flask(__name__)

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
IMPORTS_DIR = os.path.join(DATA_DIR, "imports")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
ACCOUNTS_FILE  = os.path.join(DATA_DIR, "accounts.json")
TRADES_FILE    = os.path.join(DATA_DIR, "trades.json")
LOANS_FILE     = os.path.join(DATA_DIR, "loans.json")
KRAKEN_FILE    = os.path.join(DATA_DIR, "kraken.json")

os.makedirs(IMPORTS_DIR, exist_ok=True)

# ── Schwab API constants ────────────────────────────────────────────
SCHWAB_CONFIG_FILE = os.path.join(DATA_DIR, "schwab_config.json")
SCHWAB_TOKEN_FILE  = os.path.join(DATA_DIR, "schwab_token.json")
SCHWAB_AUTH_URL    = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL   = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_TRADER_URL  = "https://api.schwabapi.com/trader/v1"
SCHWAB_MKT_URL     = "https://api.schwabapi.com/marketdata/v1"
SCHWAB_REDIRECT    = "https://127.0.0.1:8080/callback"

# ── SSL cert paths (for local HTTPS required by OAuth callback) ─────
CERT_FILE = os.path.join(DATA_DIR, "ssl_cert.pem")
KEY_FILE  = os.path.join(DATA_DIR, "ssl_key.pem")


def _ensure_ssl_cert():
    """Generate a persistent self-signed cert for local HTTPS on 127.0.0.1."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return True

    # Method 1: cryptography library
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import ipaddress as _ip

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with open(KEY_FILE, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow())
            .not_valid_after(datetime.utcnow() + timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.IPAddress(_ip.IPv4Address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        with open(CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        print("  Generated self-signed SSL cert (cryptography).")
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"  cryptography cert gen failed: {e}")

    # Method 2: openssl CLI (available by default on macOS/Linux)
    try:
        import subprocess
        r = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", KEY_FILE, "-out", CERT_FILE,
                "-days", "3650", "-nodes",
                "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            print("  Generated self-signed SSL cert (openssl CLI).")
            return True
        print(f"  openssl CLI failed: {r.stderr.decode()[:200]}")
    except FileNotFoundError:
        print("  openssl CLI not found.")
    except Exception as e:
        print(f"  openssl CLI error: {e}")

    print("  ERROR: Could not generate SSL cert. Schwab OAuth will not work.")
    print("  Fix: pip install cryptography  OR  brew install openssl")
    return False


# ── DB helpers ─────────────────────────────────────────────────────

def _load(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)   # atomic write

def load_portfolio():
    # Migrate from old root location if needed
    old = os.path.join(BASE_DIR, "portfolio.json")
    if os.path.exists(old) and not os.path.exists(PORTFOLIO_FILE):
        shutil.move(old, PORTFOLIO_FILE)
    return _load(PORTFOLIO_FILE, {"holdings": []})

def save_portfolio(d): _save(PORTFOLIO_FILE, d)
def load_accounts():   return _load(ACCOUNTS_FILE, {"accounts": []})
def save_accounts(d):  _save(ACCOUNTS_FILE, d)
def load_trades():     return _load(TRADES_FILE, {"transactions": [], "imports": []})
def save_trades(d):    _save(TRADES_FILE, d)
def load_loans():      return _load(LOANS_FILE, {"loans": []})
def save_loans(d):     _save(LOANS_FILE, d)
def load_kraken():     return _load(KRAKEN_FILE, {"transactions": [], "imports": []})
def save_kraken(d):    _save(KRAKEN_FILE, d)


# ── Main route ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Crypto holdings ────────────────────────────────────────────────

@app.route("/api/holdings", methods=["GET"])
def get_holdings():
    return jsonify(load_portfolio()["holdings"])

@app.route("/api/holdings", methods=["POST"])
def add_holding():
    data   = request.json or {}
    symbol = data.get("symbol", "").strip().upper()
    name   = data.get("name",   "").strip() or symbol
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400

    db = load_portfolio()
    for h in db["holdings"]:
        if h["symbol"] == symbol:
            h["amount"] = amount
            save_portfolio(db)
            return jsonify(h)

    h = {"id": str(uuid.uuid4()), "symbol": symbol, "name": name,
         "amount": amount, "added_at": datetime.utcnow().isoformat()}
    db["holdings"].append(h)
    save_portfolio(db)
    return jsonify(h), 201

@app.route("/api/holdings/<hid>", methods=["PUT"])
def update_holding(hid):
    data = request.json or {}
    db   = load_portfolio()
    for h in db["holdings"]:
        if h["id"] == hid:
            if "amount" in data:
                try:    h["amount"] = float(data["amount"])
                except: return jsonify({"error": "Invalid amount"}), 400
            if "name" in data:
                h["name"] = data["name"]
            save_portfolio(db)
            return jsonify(h)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/holdings/<hid>", methods=["DELETE"])
def delete_holding(hid):
    db = load_portfolio()
    db["holdings"] = [h for h in db["holdings"] if h["id"] != hid]
    save_portfolio(db)
    return "", 204

@app.route("/api/price/<symbol>")
def get_price(symbol):
    symbol = symbol.upper()
    try:
        r = requests.get(f"https://cryptoprices.cc/{symbol}/", timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return jsonify({"symbol": symbol, "price": float(r.text.strip())})
    except requests.RequestException as e:
        # Try yfinance as a fallback for equities/tickers
        if yf and " " not in symbol:
            try:
                t = yf.Ticker(symbol)
                data = t.history(period='1d')
                if not data.empty:
                    price = float(data['Close'].iloc[-1])
                    return jsonify({"symbol": symbol, "price": price})
            except Exception:
                pass
        return jsonify({"error": f"Network error: {e}"}), 502
    except ValueError:
        return jsonify({"error": "Unknown symbol or no price data"}), 404

@app.route("/api/prices", methods=["POST"])
def get_prices_bulk():
    symbols = list({s.upper() for s in (request.json or {}).get("symbols", [])})
    if not symbols:
        return jsonify({})

    def fetch(sym):
        try:
            r = requests.get(f"https://cryptoprices.cc/{sym}/", timeout=8,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return sym, {"price": float(r.text.strip()), "error": None}
        except Exception as e:
            # Fallback to yfinance for non-option tickers
            if yf and " " not in sym:
                try:
                    t = yf.Ticker(sym)
                    data = t.history(period='1d')
                    if not data.empty:
                        return sym, {"price": float(data['Close'].iloc[-1]), "error": None}
                except Exception:
                    pass
            return sym, {"price": None, "error": str(e)}

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 12)) as ex:
        for sym, d in [f.result() for f in as_completed(ex.submit(fetch, s) for s in symbols)]:
            results[sym] = d
    return jsonify(results)


# ── Manual accounts ────────────────────────────────────────────────

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    return jsonify(load_accounts()["accounts"])

@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        value = float(data.get("value", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid value"}), 400

    db  = load_accounts()
    acct = {
        "id":         str(uuid.uuid4()),
        "name":       name,
        "type":       data.get("type",  "other").strip(),
        "value":      value,
        "notes":      data.get("notes", "").strip(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    db["accounts"].append(acct)
    save_accounts(db)
    return jsonify(acct), 201

@app.route("/api/accounts/<aid>", methods=["PUT"])
def update_account(aid):
    data = request.json or {}
    db   = load_accounts()
    for a in db["accounts"]:
        if a["id"] == aid:
            for k in ("name", "type", "notes"):
                if k in data: a[k] = data[k]
            if "value" in data:
                try:    a["value"] = float(data["value"])
                except: return jsonify({"error": "Invalid value"}), 400
            a["updated_at"] = datetime.utcnow().isoformat()
            save_accounts(db)
            return jsonify(a)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/accounts/<aid>", methods=["DELETE"])
def delete_account(aid):
    db = load_accounts()
    db["accounts"] = [a for a in db["accounts"] if a["id"] != aid]
    save_accounts(db)
    return "", 204


# ── Loans ──────────────────────────────────────────────────────────

@app.route("/api/loans", methods=["GET"])
def get_loans():
    return jsonify(load_loans()["loans"])

@app.route("/api/loans", methods=["POST"])
def add_loan():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        balance         = float(data.get("balance", 0))
        monthly_payment = float(data.get("monthly_payment", 0))
        rate_raw        = data.get("interest_rate")
        interest_rate   = float(rate_raw) if rate_raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid number"}), 400

    db   = load_loans()
    loan = {
        "id":              str(uuid.uuid4()),
        "name":            name,
        "type":            data.get("type", "other").strip(),
        "balance":         balance,
        "monthly_payment": monthly_payment,
        "interest_rate":   interest_rate,
        "notes":           data.get("notes", "").strip(),
        "updated_at":      datetime.utcnow().isoformat(),
    }
    db["loans"].append(loan)
    save_loans(db)
    return jsonify(loan), 201

@app.route("/api/loans/<lid>", methods=["PUT"])
def update_loan(lid):
    data = request.json or {}
    db   = load_loans()
    for l in db["loans"]:
        if l["id"] == lid:
            for k in ("name", "type", "notes"):
                if k in data:
                    l[k] = data[k]
            for k in ("balance", "monthly_payment", "interest_rate"):
                if k in data:
                    try:    l[k] = float(data[k]) if data[k] not in (None, "") else 0.0
                    except: return jsonify({"error": f"Invalid {k}"}), 400
            l["updated_at"] = datetime.utcnow().isoformat()
            save_loans(db)
            return jsonify(l)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/loans/<lid>", methods=["DELETE"])
def delete_loan(lid):
    db = load_loans()
    db["loans"] = [l for l in db["loans"] if l["id"] != lid]
    save_loans(db)
    return "", 204


# ── Trade history: CSV parsing ─────────────────────────────────────

def _parse_dollar(s):
    if not s:
        return None
    try:
        return float(s.strip().replace(",", "").replace("$", ""))
    except ValueError:
        return None

_OCC_RE = re.compile(r'^([A-Z]+)\s+(\d{2})(\d{2})(\d{2})([CP])(\d{8})$')

def _occ_to_human(symbol):
    """Convert OCC option symbol to human-readable format matching Schwab CSV.

    'SATL  270115C00010000' → 'SATL 01/15/2027 10.00 C'
    Returns the symbol unchanged if it doesn't match OCC format.
    """
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return symbol
    root, yy, mm, dd, cp, strike_str = m.groups()
    strike = int(strike_str) / 1000.0
    year = 2000 + int(yy)
    return f"{root} {mm}/{dd}/{year} {strike:.2f} {cp}"


def _tx_hash(date, action, symbol, qty, price, amount):
    """Canonical dedup hash shared by CSV and API parsers."""
    _q = qty   if qty   else None
    _p = round(price,  4) if price  else None
    _a = round(amount, 2) if amount is not None else 0
    return hashlib.md5(f"{date}|{action}|{symbol}|{_q}|{_p}|{_a}".encode()).hexdigest()


def _parse_schwab_csv(content):
    """Parse a Schwab transaction CSV. Returns list of transaction dicts."""
    reader = csv.DictReader(StringIO(content))
    txs    = []
    for row in reader:
        date_str = row.get("Date", "").split(" as of ")[0].strip()
        try:
            date = datetime.strptime(date_str, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        dt_str = date_str.split(" as of ")[0].strip()
        try:
            date = datetime.strptime(dt_str, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue

        action      = row.get("Action",      "").strip().strip('"')
        symbol      = row.get("Symbol",      "").strip().strip('"')
        description = row.get("Description", "").strip().strip('"')
        qty_str     = row.get("Quantity",    "").strip().strip('"').replace(",", "")
        price_str   = row.get("Price",       "").strip().strip('"')
        fees_str    = row.get("Fees & Comm", "").strip().strip('"')
        amount_str  = row.get("Amount",      "").strip().strip('"')

        qty    = float(qty_str) if qty_str else None
        price  = _parse_dollar(price_str)
        fees   = _parse_dollar(fees_str) or 0.0
        amount = _parse_dollar(amount_str)

        tx_id = _tx_hash(date, action, symbol, qty, price, amount)

        txs.append({
            "id":          tx_id,
            "date":        date,
            "action":      action,
            "symbol":      symbol,
            "description": description,
            "quantity":    qty,
            "price":       price,
            "fees":        fees,
            "amount":      amount,
            "is_option":   bool(symbol and " " in symbol),
        })
    return txs


# ── Trade history: FIFO P&L calculation ───────────────────────────

def _calc_performance(transactions):
    """
    FIFO lot-matching P&L.  Each sell is matched against the oldest open
    buy lots for that symbol in date order.  'amount' already includes
    fees (Schwab convention), so abs(amount) is used directly for cost
    and proceeds — fees are NOT double-counted.

    Returns per-symbol dicts that include:
      closed_lots  – list of matched lot pairs with open/close dates
      open_lots    – remaining unmatched buy lots
      last_close_date – ISO date of most-recent sell for sort support
    """
    BUY_ACTIONS  = {"Buy", "Buy to Open", "Reinvest Shares", "Buy to Close"}
    SELL_ACTIONS = {"Sell", "Sell to Close", "Sell to Open"}
    DIV_ACTIONS  = {"Cash Dividend", "Qualified Dividend", "Credit Interest",
                    "Non-Qualified Div", "Special Dividend"}

    open_lots    = {}   # sym -> list[lot dict]  (acts as FIFO queue)
    closed_lots  = []   # all matched lot records
    dividends    = 0.0
    descriptions = {}
    buy_counts   = {}
    sell_counts  = {}
    last_tx_dates = {}  # sym -> most-recent any-transaction date

    for tx in sorted(transactions, key=lambda x: x["date"]):
        sym     = tx["symbol"]
        action  = tx["action"]
        qty     = tx["quantity"] or 0
        amount  = tx["amount"]   or 0
        account = tx.get("account_name", "Unknown")
        desc    = tx.get("description", "")

        if sym and desc and sym not in descriptions:
            descriptions[sym] = desc

        if sym and tx["date"]:
            if sym not in last_tx_dates or tx["date"] > last_tx_dates[sym]:
                last_tx_dates[sym] = tx["date"]

        if action in BUY_ACTIONS and sym and qty > 1e-9:
            cost = abs(amount)
            open_lots.setdefault(sym, []).append({
                "open_date":     tx["date"],
                "qty":           qty,
                "remaining_qty": qty,
                "unit_cost":     cost / qty,
                "account":       account,
            })
            buy_counts[sym] = buy_counts.get(sym, 0) + 1

        elif action in SELL_ACTIONS and sym and qty > 1e-9:
            proceeds       = abs(amount)
            proceeds_per_u = proceeds / qty
            remaining_sell = qty
            sell_counts[sym] = sell_counts.get(sym, 0) + 1

            lots = open_lots.get(sym, [])
            i = 0
            while remaining_sell > 1e-9 and i < len(lots):
                lot      = lots[i]
                sell_qty = min(lot["remaining_qty"], remaining_sell)
                lot_proc = proceeds_per_u * sell_qty
                lot_cost = lot["unit_cost"] * sell_qty
                open_dt  = _date.fromisoformat(lot["open_date"]) if lot["open_date"] else None
                close_dt = _date.fromisoformat(tx["date"])
                hold     = (close_dt - open_dt).days if open_dt else None

                closed_lots.append({
                    "symbol":       sym,
                    "open_date":    lot["open_date"],
                    "close_date":   tx["date"],
                    "qty":          round(sell_qty, 6),
                    "unit_cost":    lot["unit_cost"],
                    "cost_basis":   lot_cost,
                    "proceeds":     lot_proc,
                    "realized_pnl": lot_proc - lot_cost,
                    "account":      lot["account"],
                    "hold_days":    hold,
                    "is_long_term": hold > 365 if hold is not None else None,
                })

                lot["remaining_qty"] -= sell_qty
                remaining_sell       -= sell_qty
                if lot["remaining_qty"] < 1e-9:
                    i += 1

            open_lots[sym] = [l for l in lots if l["remaining_qty"] > 1e-9]

            # Orphan sell — shares sold with no matching buy in history.
            # Cost basis is unknown; P&L is marked None so it's excluded from totals.
            if remaining_sell > 1e-9:
                closed_lots.append({
                    "symbol": sym, "open_date": None, "close_date": tx["date"],
                    "qty": round(remaining_sell, 6), "unit_cost": None,
                    "cost_basis": None, "proceeds": proceeds_per_u * remaining_sell,
                    "realized_pnl": None,
                    "account": account, "hold_days": None, "is_long_term": None,
                    "orphan": True,
                })

        elif action in DIV_ACTIONS:
            dividends += abs(amount)

    # ── Aggregate per symbol ─────────────────────────────────────
    by_symbol = {}
    unrealized_pnl_sum = 0.0

    for lot in closed_lots:
        sym = lot["symbol"]
        s   = by_symbol.setdefault(sym, {
            "realized_pnl": 0.0, "total_bought": 0.0, "total_sold": 0.0,
            "buy_count":  buy_counts.get(sym, 0),
            "sell_count": sell_counts.get(sym, 0),
            "description": descriptions.get(sym, sym),
            "closed_lots": [], "last_close_date": None,
            "last_tx_date": last_tx_dates.get(sym),
            "incomplete_basis": False,
        })
        if lot.get("orphan"):
            s["incomplete_basis"] = True
        else:
            s["realized_pnl"] += lot["realized_pnl"]
            s["total_bought"] += lot["cost_basis"]
            s["total_sold"]   += lot["proceeds"]
        s["closed_lots"].append(lot)
        if s["last_close_date"] is None or lot["close_date"] > s["last_close_date"]:
            s["last_close_date"] = lot["close_date"]

    # Attach open positions
    for sym, lots in open_lots.items():
        remaining = [l for l in lots if l["remaining_qty"] > 1e-9]
        if not remaining:
            continue
        s = by_symbol.setdefault(sym, {
            "realized_pnl": 0.0, "total_bought": 0.0, "total_sold": 0.0,
            "buy_count":  buy_counts.get(sym, 0),
            "sell_count": sell_counts.get(sym, 0),
            "description": descriptions.get(sym, sym),
            "closed_lots": [], "last_close_date": None,
            "last_tx_date": last_tx_dates.get(sym),
            "incomplete_basis": False,
        })
        total_qty  = sum(l["remaining_qty"] for l in remaining)
        total_cost = sum(l["unit_cost"] * l["remaining_qty"] for l in remaining)
        s["open_lots"]        = [{"open_date": l["open_date"], "qty": round(l["remaining_qty"], 6),
                                   "unit_cost": l["unit_cost"], "account": l["account"]}
                                  for l in remaining]
        s["current_qty"]      = round(total_qty, 6)
        s["current_avg_cost"] = total_cost / total_qty if total_qty > 1e-9 else 0.0
        # Note: Unrealized PnL requires current price, which is NOT available here.
        # We only return the cost basis part which can be used by the frontend once it fetches prices.
        s["current_cost_basis"] = total_cost

    # Defaults for symbols that only have closed lots
    for sym, s in by_symbol.items():
        s.setdefault("open_lots",          [])
        s.setdefault("current_qty",        0.0)
        s.setdefault("current_avg_cost",   0.0)
        s.setdefault("current_cost_basis", 0.0)
        s.setdefault("last_tx_date",       last_tx_dates.get(sym))
        s.setdefault("incomplete_basis",   False)
        s["closed_lots"].sort(key=lambda x: x["close_date"] or "")

    return {
        "by_symbol":          by_symbol,
        "total_realized_pnl": sum(v["realized_pnl"] for v in by_symbol.values()), # This is wrong, wait.
        "total_dividends":    dividends,
        "total_fees":         0.0,   # fees are baked into Schwab amounts
    }



# ── Trade history: routes ──────────────────────────────────────────

@app.route("/api/trades/import", methods=["POST"])
def import_trades():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400

    account_name = request.form.get("account_name", "").strip() or "Unknown Account"
    notes        = request.form.get("notes", "").strip()
    content      = f.read().decode("utf-8-sig")   # strip BOM

    # Save raw CSV copy
    safe_name = f.filename.replace("/", "_").replace("\\", "_")
    with open(os.path.join(IMPORTS_DIR, safe_name), "w", encoding="utf-8") as fp:
        fp.write(content)

    new_txs = _parse_schwab_csv(content)
    if not new_txs:
        return jsonify({"error": "No transactions found in CSV"}), 400

    # Generate the import ID up-front so we can stamp it on every transaction.
    # This lets us cleanly delete an import and all its rows later.
    import_id = str(uuid.uuid4())
    for tx in new_txs:
        tx["account_name"] = account_name
        tx["import_id"]    = import_id

    db = load_trades()
    existing_ids = {t["id"] for t in db["transactions"]}
    seen_in_batch: set = set()
    added = []
    for t in new_txs:
        if t["id"] not in existing_ids and t["id"] not in seen_in_batch:
            seen_in_batch.add(t["id"])
            added.append(t)
    dups  = len(new_txs) - len(added)
    db["transactions"].extend(added)

    dates = [t["date"] for t in new_txs]
    meta  = {
        "id":                     import_id,
        "filename":               safe_name,
        "account_name":           account_name,
        "notes":                  notes,
        "imported_at":            datetime.utcnow().isoformat(),
        "date_range_from":        min(dates),
        "date_range_to":          max(dates),
        "total_rows":             len(new_txs),
        "new_transactions":       len(added),
        "duplicate_transactions": dups,
    }
    db["imports"].append(meta)
    save_trades(db)
    return jsonify({"import": meta, "added": len(added), "duplicates": dups})

@app.route("/api/trades/transactions")
def get_transactions():
    db      = load_trades()
    txs     = db["transactions"]
    account = request.args.get("account")
    if account:
        txs = [t for t in txs if t.get("account_name") == account]
    txs    = sorted(txs, key=lambda x: x["date"], reverse=True)
    limit  = int(request.args.get("limit",  100))
    offset = int(request.args.get("offset",   0))
    return jsonify({"transactions": txs[offset:offset + limit], "total": len(txs)})

@app.route("/api/trades/performance")
def get_performance():
    db      = load_trades()
    txs     = db["transactions"]
    account = request.args.get("account")
    if account:
        txs = [t for t in txs if t.get("account_name") == account]
    return jsonify(_calc_performance(txs))

@app.route("/api/trades/accounts")
def get_trade_accounts():
    """Distinct account names that appear on imported transactions."""
    db    = load_trades()
    names = sorted({t.get("account_name", "Unknown")
                    for t in db["transactions"] if t.get("account_name")})
    return jsonify(names)

@app.route("/api/trades/imports")
def get_imports():
    return jsonify(load_trades()["imports"])

@app.route("/api/trades/imports/<import_id>", methods=["PATCH"])
def rename_import(import_id):
    data = request.json or {}
    db   = load_trades()
    for imp in db["imports"]:
        if imp["id"] == import_id:
            if "account_name" in data:
                new_name = data["account_name"].strip()
                if not new_name:
                    return jsonify({"error": "Name cannot be empty"}), 400
                # Keep transactions in sync
                old_name = imp["account_name"]
                for t in db["transactions"]:
                    if t.get("import_id") == import_id or (
                        not t.get("import_id") and t.get("account_name") == old_name
                    ):
                        t["account_name"] = new_name
                imp["account_name"] = new_name
            if "notes" in data:
                imp["notes"] = data["notes"].strip()
            save_trades(db)
            return jsonify(imp)
    return jsonify({"error": "Import not found"}), 404

@app.route("/api/trades/imports/<import_id>", methods=["DELETE"])
def delete_import(import_id):
    db     = load_trades()
    target = next((i for i in db["imports"] if i["id"] == import_id), None)
    if not target:
        return jsonify({"error": "Import not found"}), 404

    # Primary removal: transactions tagged with this exact import_id.
    # Fallback: transactions with no import_id but matching account_name
    # (these were imported before import_id tracking was added).
    before = len(db["transactions"])
    db["transactions"] = [
        t for t in db["transactions"]
        if not (
            t.get("import_id") == import_id
            or (not t.get("import_id") and t.get("account_name") == target["account_name"])
        )
    ]
    removed = before - len(db["transactions"])

    # Remove the import record
    db["imports"] = [i for i in db["imports"] if i["id"] != import_id]
    save_trades(db)

    # Delete the raw CSV copy
    csv_path = os.path.join(IMPORTS_DIR, target["filename"])
    if os.path.exists(csv_path):
        os.remove(csv_path)

    return jsonify({"removed_transactions": removed, "filename": target["filename"]})


@app.route("/api/trades/imports", methods=["DELETE"])
def delete_all_imports():
    db     = load_trades()
    before = len(db["transactions"])
    for imp in db["imports"]:
        csv_path = os.path.join(IMPORTS_DIR, imp["filename"])
        if os.path.exists(csv_path):
            os.remove(csv_path)
    db["transactions"] = []
    db["imports"]      = []
    save_trades(db)
    return jsonify({"removed_transactions": before, "removed_imports": before})


# ── Kraken: CSV parser ─────────────────────────────────────────────

def _parse_kraken_csv(content):
    """
    Parse a Kraken spot ledger CSV export.
    Columns: txid,refid,time,type,subtype,aclass,subclass,asset,wallet,amount,fee,balance,amountusd
    Returns list of transaction dicts.
    Only processes crypto rows; ignores earn/autoallocation (internal wallet moves).
    """
    reader = csv.DictReader(StringIO(content))
    txs = []
    for row in reader:
        type_    = row.get("type",    "").strip().strip('"')
        subclass = row.get("subclass","").strip().strip('"')

        # Skip internal earn wallet moves; skip non-crypto rows
        if type_ == "earn":
            continue
        if subclass and subclass != "crypto":
            continue

        txid     = row.get("txid",   "").strip().strip('"')
        refid    = row.get("refid",  "").strip().strip('"')
        time_str = row.get("time",   "").strip().strip('"')
        asset    = row.get("asset",  "").strip().strip('"')
        wallet   = row.get("wallet", "").strip().strip('"')

        try:
            dt   = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            date = dt.date().isoformat()
        except ValueError:
            continue

        def _f(key):
            try:    return float(row.get(key, 0) or 0)
            except: return 0.0

        txs.append({
            "id":         txid,
            "refid":      refid,
            "date":       date,
            "datetime":   dt.isoformat(),
            "type":       type_,
            "asset":      asset,
            "wallet":     wallet,
            "amount":     _f("amount"),
            "fee":        _f("fee"),
            "amount_usd": _f("amountusd"),
        })
    return txs


# ── Kraken: FIFO P&L ───────────────────────────────────────────────

def _calc_kraken_performance(transactions):
    """
    FIFO P&L for Kraken ledger transactions.
    - trade + positive amount  → BUY  (amountusd = cost in USD)
    - trade + negative amount  → SELL (amountusd = proceeds in USD)
    - withdrawal               → off-platform transfer; reduces open lots at cost, no P&L
    Withdrawals are tracked so position sizes remain accurate.
    """
    open_lots   = {}   # asset → [lot, ...]
    closed_lots = []
    buy_counts  = {}
    sell_counts = {}

    for tx in sorted(transactions, key=lambda x: x["datetime"]):
        asset  = tx["asset"]
        type_  = tx["type"]
        amount = tx["amount"]
        usd    = abs(tx.get("amount_usd") or 0)

        if type_ == "trade" and amount > 1e-12:
            # ── BUY
            qty       = amount
            unit_cost = usd / qty if qty > 1e-12 else 0.0
            open_lots.setdefault(asset, []).append({
                "open_date":     tx["date"],
                "qty":           qty,
                "remaining_qty": qty,
                "unit_cost":     unit_cost,
            })
            buy_counts[asset] = buy_counts.get(asset, 0) + 1

        elif type_ == "trade" and amount < -1e-12:
            # ── SELL
            qty           = abs(amount)
            proceeds_per_u = usd / qty if qty > 1e-12 else 0.0
            remaining_sell = qty
            sell_counts[asset] = sell_counts.get(asset, 0) + 1

            lots = open_lots.get(asset, [])
            i = 0
            while remaining_sell > 1e-12 and i < len(lots):
                lot      = lots[i]
                sell_qty = min(lot["remaining_qty"], remaining_sell)
                lot_proc = proceeds_per_u * sell_qty
                lot_cost = lot["unit_cost"] * sell_qty
                open_dt  = _date.fromisoformat(lot["open_date"])
                close_dt = _date.fromisoformat(tx["date"])
                hold     = (close_dt - open_dt).days

                closed_lots.append({
                    "asset":        asset,
                    "open_date":    lot["open_date"],
                    "close_date":   tx["date"],
                    "qty":          round(sell_qty, 10),
                    "unit_cost":    lot["unit_cost"],
                    "cost_basis":   lot_cost,
                    "proceeds":     lot_proc,
                    "realized_pnl": lot_proc - lot_cost,
                    "hold_days":    hold,
                    "is_long_term": hold > 365,
                })

                lot["remaining_qty"] -= sell_qty
                remaining_sell       -= sell_qty
                if lot["remaining_qty"] < 1e-12:
                    i += 1

            open_lots[asset] = [l for l in lots if l["remaining_qty"] > 1e-12]

            # Orphan sell (no prior buy history) — full proceeds = gain
            if remaining_sell > 1e-12:
                closed_lots.append({
                    "asset": asset, "open_date": None, "close_date": tx["date"],
                    "qty": round(remaining_sell, 10), "unit_cost": 0.0,
                    "cost_basis": 0.0, "proceeds": proceeds_per_u * remaining_sell,
                    "realized_pnl": proceeds_per_u * remaining_sell,
                    "hold_days": None, "is_long_term": None,
                })

        elif type_ == "withdrawal" and amount < -1e-12:
            # ── WITHDRAWAL  — reduce open lots FIFO, no P&L event
            qty            = abs(amount)
            remaining_wd   = qty
            lots           = open_lots.get(asset, [])
            i = 0
            while remaining_wd > 1e-12 and i < len(lots):
                lot        = lots[i]
                reduce_qty = min(lot["remaining_qty"], remaining_wd)
                lot["remaining_qty"] -= reduce_qty
                remaining_wd         -= reduce_qty
                if lot["remaining_qty"] < 1e-12:
                    i += 1
            open_lots[asset] = [l for l in lots if l["remaining_qty"] > 1e-12]

    # ── Aggregate per asset ──────────────────────────────────────
    by_asset = {}

    for lot in closed_lots:
        asset = lot["asset"]
        s = by_asset.setdefault(asset, {
            "realized_pnl": 0.0, "total_bought_usd": 0.0, "total_sold_usd": 0.0,
            "buy_count":  buy_counts.get(asset, 0),
            "sell_count": sell_counts.get(asset, 0),
            "closed_lots": [], "last_close_date": None,
        })
        s["realized_pnl"]     += lot["realized_pnl"]
        s["total_bought_usd"] += lot["cost_basis"]
        s["total_sold_usd"]   += lot["proceeds"]
        s["closed_lots"].append(lot)
        if not s["last_close_date"] or lot["close_date"] > s["last_close_date"]:
            s["last_close_date"] = lot["close_date"]

    for asset, lots in open_lots.items():
        remaining = [l for l in lots if l["remaining_qty"] > 1e-12]
        if not remaining:
            continue
        s = by_asset.setdefault(asset, {
            "realized_pnl": 0.0, "total_bought_usd": 0.0, "total_sold_usd": 0.0,
            "buy_count":  buy_counts.get(asset, 0),
            "sell_count": sell_counts.get(asset, 0),
            "closed_lots": [], "last_close_date": None,
        })
        total_qty  = sum(l["remaining_qty"]             for l in remaining)
        total_cost = sum(l["unit_cost"] * l["remaining_qty"] for l in remaining)
        s["open_lots"]        = [{"open_date": l["open_date"], "qty": round(l["remaining_qty"], 10),
                                   "unit_cost": l["unit_cost"]} for l in remaining]
        s["current_qty"]      = round(total_qty, 10)
        s["avg_buy_price"]    = total_cost / total_qty if total_qty > 1e-12 else 0.0
        s["current_cost_basis"] = total_cost

    for s in by_asset.values():
        s.setdefault("open_lots",          [])
        s.setdefault("current_qty",        0.0)
        s.setdefault("avg_buy_price",      0.0)
        s.setdefault("current_cost_basis", 0.0)
        s["closed_lots"].sort(key=lambda x: x["close_date"] or "")

    return {
        "by_asset":           by_asset,
        "total_realized_pnl": sum(v["realized_pnl"] for v in by_asset.values()),
    }


# ── Kraken: routes ─────────────────────────────────────────────────

@app.route("/api/kraken/imports")
def kraken_get_imports():
    return jsonify(load_kraken()["imports"])

@app.route("/api/kraken/assets")
def kraken_get_assets():
    db    = load_kraken()
    names = sorted({t["asset"] for t in db["transactions"] if t.get("asset")})
    return jsonify(names)

@app.route("/api/kraken/transactions")
def kraken_get_transactions():
    db     = load_kraken()
    txs    = db["transactions"]
    asset  = request.args.get("asset")
    if asset:
        txs = [t for t in txs if t.get("asset") == asset]
    txs    = sorted(txs, key=lambda x: x["datetime"], reverse=True)
    limit  = int(request.args.get("limit",  100))
    offset = int(request.args.get("offset",   0))
    return jsonify({"transactions": txs[offset:offset + limit], "total": len(txs)})

@app.route("/api/kraken/performance")
def kraken_get_performance():
    db    = load_kraken()
    txs   = db["transactions"]
    asset = request.args.get("asset")
    if asset:
        txs = [t for t in txs if t.get("asset") == asset]
    return jsonify(_calc_kraken_performance(txs))

@app.route("/api/kraken/import", methods=["POST"])
def kraken_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400

    notes   = request.form.get("notes", "").strip()
    content = f.read().decode("utf-8-sig")

    safe_name = f.filename.replace("/", "_").replace("\\", "_")
    kraken_imports_dir = os.path.join(IMPORTS_DIR, "kraken")
    os.makedirs(kraken_imports_dir, exist_ok=True)
    with open(os.path.join(kraken_imports_dir, safe_name), "w", encoding="utf-8") as fp:
        fp.write(content)

    new_txs = _parse_kraken_csv(content)
    if not new_txs:
        return jsonify({"error": "No transactions found in CSV"}), 400

    import_id = str(uuid.uuid4())
    for tx in new_txs:
        tx["import_id"] = import_id

    db = load_kraken()
    existing_ids = {t["id"] for t in db["transactions"]}
    added = [t for t in new_txs if t["id"] not in existing_ids]
    dups  = len(new_txs) - len(added)
    db["transactions"].extend(added)

    dates = [t["date"] for t in new_txs]
    meta  = {
        "id":                     import_id,
        "filename":               safe_name,
        "notes":                  notes,
        "imported_at":            datetime.utcnow().isoformat(),
        "date_range_from":        min(dates),
        "date_range_to":          max(dates),
        "total_rows":             len(new_txs),
        "new_transactions":       len(added),
        "duplicate_transactions": dups,
    }
    db["imports"].append(meta)
    save_kraken(db)
    return jsonify({"import": meta, "added": len(added), "duplicates": dups})

@app.route("/api/kraken/imports/<import_id>", methods=["DELETE"])
def kraken_delete_import(import_id):
    db     = load_kraken()
    target = next((i for i in db["imports"] if i["id"] == import_id), None)
    if not target:
        return jsonify({"error": "Import not found"}), 404

    before = len(db["transactions"])
    db["transactions"] = [t for t in db["transactions"] if t.get("import_id") != import_id]
    removed = before - len(db["transactions"])
    db["imports"] = [i for i in db["imports"] if i["id"] != import_id]
    save_kraken(db)

    csv_path = os.path.join(IMPORTS_DIR, "kraken", target["filename"])
    if os.path.exists(csv_path):
        os.remove(csv_path)

    return jsonify({"removed_transactions": removed, "filename": target["filename"]})


# ── Schwab: helpers ────────────────────────────────────────────────

def _schwab_config():
    """Returns (client_id, client_secret) or (None, None) if not configured."""
    cid = os.environ.get("SCHWAB_CLIENT_ID")
    sec = os.environ.get("SCHWAB_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    if os.path.exists(SCHWAB_CONFIG_FILE):
        d = _load(SCHWAB_CONFIG_FILE, {})
        return d.get("client_id"), d.get("client_secret")
    return None, None


def _load_schwab_token():
    return _load(SCHWAB_TOKEN_FILE, {})


def _save_schwab_token(d):
    _save(SCHWAB_TOKEN_FILE, d)


def _schwab_access_token():
    """
    Returns a valid Bearer token, auto-refreshing if needed.
    Returns None if not connected or if re-auth is required.
    """
    token = _load_schwab_token()
    if not token.get("refresh_token"):
        return None

    # Refresh token is only valid for 7 days
    refresh_age = time.time() - token.get("refresh_issued_at", 0)
    if refresh_age > 7 * 86400:
        return None

    # Access token still valid?
    if token.get("access_token") and time.time() < token.get("expires_at", 0) - 60:
        return token["access_token"]

    # Exchange refresh token for a new access token
    cid, sec = _schwab_config()
    if not cid:
        return None
    try:
        r = requests.post(
            SCHWAB_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
            auth=(cid, sec),
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        token["access_token"]  = d["access_token"]
        token["refresh_token"] = d.get("refresh_token", token["refresh_token"])
        token["expires_at"]    = time.time() + d.get("expires_in", 1800) - 30
        # Don't overwrite refresh_issued_at — it tracks the original auth time
        _save_schwab_token(token)
        return token["access_token"]
    except Exception:
        return None


# ── Schwab: OAuth flow ─────────────────────────────────────────────

@app.route("/callback")
def schwab_callback():
    """Handles the OAuth redirect from Schwab after the user authorizes."""
    error = request.args.get("error")
    if error:
        return redirect("/?schwab_error=" + error)

    code = request.args.get("code")
    if not code:
        return redirect("/?schwab_error=no_code")

    cid, sec = _schwab_config()
    if not cid:
        return redirect("/?schwab_error=not_configured")

    try:
        r = requests.post(
            SCHWAB_TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": SCHWAB_REDIRECT,
            },
            auth=(cid, sec),
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        _save_schwab_token({
            "access_token":      d["access_token"],
            "refresh_token":     d["refresh_token"],
            "expires_at":        time.time() + d.get("expires_in", 1800) - 30,
            "refresh_issued_at": time.time(),
            "connected_at":      datetime.utcnow().isoformat(),
        })
        return redirect("/?schwab_connected=1")
    except requests.HTTPError as e:
        return redirect(f"/?schwab_error=token_exchange_failed_{r.status_code}")
    except Exception as e:
        return redirect(f"/?schwab_error=unknown")


# ── Schwab: API routes ─────────────────────────────────────────────

@app.route("/api/schwab/status")
def schwab_status():
    cid, _ = _schwab_config()
    if not cid:
        return jsonify({"configured": False, "connected": False})

    token = _load_schwab_token()
    if not token.get("refresh_token"):
        return jsonify({"configured": True, "connected": False})

    refresh_age = time.time() - token.get("refresh_issued_at", 0)
    refresh_remaining = max(0, 7 * 86400 - refresh_age)

    return jsonify({
        "configured":          True,
        "connected":           refresh_remaining > 0,
        "expires_at":          token.get("expires_at", 0),
        "refresh_expires_in":  refresh_remaining,
        "connected_at":        token.get("connected_at"),
    })


@app.route("/api/schwab/auth-url")
def schwab_auth_url():
    cid, _ = _schwab_config()
    if not cid:
        return jsonify({"error": "Schwab credentials not configured. Create data/schwab_config.json."}), 400
    url = (
        f"{SCHWAB_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={cid}"
        f"&redirect_uri={SCHWAB_REDIRECT}"
        f"&scope=api"
    )
    return jsonify({"url": url})


@app.route("/api/schwab/disconnect", methods=["POST"])
def schwab_disconnect():
    if os.path.exists(SCHWAB_TOKEN_FILE):
        os.remove(SCHWAB_TOKEN_FILE)
    return jsonify({"ok": True})


def _label_from_filename(fn):
    """Extract (last3, human_label) from a Schwab CSV filename like HSA_Brokerage_XXX787_..."""
    m = re.search(r"XXX(\d{3})", fn, re.IGNORECASE)
    if not m:
        return None, None
    last3  = m.group(1)
    prefix = fn[:m.start()].strip("_- ").replace("_", " ").replace("-", " ").strip()
    parts  = prefix.split()
    seen, clean = set(), []
    for p in parts:
        if p.upper() not in seen:
            seen.add(p.upper())
            clean.append(p)
    label = " ".join(clean)
    label = re.sub(r"Roth Contributory IRA", "Roth IRA", label, flags=re.IGNORECASE)
    label = re.sub(r"Designated Bene Individual", "Brokerage", label, flags=re.IGNORECASE)
    label = re.sub(r"HSA Brokerage", "HSA", label, flags=re.IGNORECASE)
    return last3, label.strip() or None


def _build_label_map():
    """Return last3 → human label, preferring nicknames then CSV filenames."""
    cfg = _load(SCHWAB_CONFIG_FILE, {})
    result = {}
    for last4, nick in (cfg.get("account_nicknames") or {}).items():
        result[last4[-3:]] = nick
    for d in [IMPORTS_DIR, BASE_DIR]:
        try:
            for fn in os.listdir(d):
                last3, label = _label_from_filename(fn)
                if last3 and label and last3 not in result:
                    result[last3] = label
        except OSError:
            pass
    return result


@app.route("/api/schwab/positions")
def schwab_positions():
    access_token = _schwab_access_token()
    if not access_token:
        return jsonify({"error": "not_connected"}), 401

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    try:
        r = requests.get(
            f"{SCHWAB_TRADER_URL}/accounts?fields=positions",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 401:
            _save_schwab_token({})   # invalidate stale token
            return jsonify({"error": "token_expired"}), 401
        r.raise_for_status()
        accounts_data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": f"Schwab API error: {e}"}), 502

    label_map = _build_label_map()
    result_accounts = []
    total_value = 0.0

    for acct in accounts_data:
        sa       = acct.get("securitiesAccount", {})
        acct_num = sa.get("accountNumber", "")
        balances = sa.get("currentBalances", {})

        cash        = float(balances.get("cashBalance") or 0)
        liquid_val  = float(
            balances.get("liquidationValue")
            or balances.get("totalValue")
            or 0
        )

        positions = []
        for pos in sa.get("positions", []):
            inst    = pos.get("instrument", {})
            symbol  = inst.get("symbol", "")
            qty     = float(pos.get("longQuantity") or pos.get("quantity") or 0)
            avg_px  = float(pos.get("averagePrice") or pos.get("averageLongPrice") or 0)
            mkt_val = float(pos.get("marketValue") or 0)
            unreal  = float(
                pos.get("longOpenProfitLoss")
                or pos.get("unrealizedProfitLoss")
                or (mkt_val - avg_px * qty)
            )
            positions.append({
                "symbol":         symbol,
                "description":    inst.get("description", ""),
                "asset_type":     inst.get("assetType", ""),
                "quantity":       qty,
                "avg_price":      avg_px,
                "market_value":   mkt_val,
                "unrealized_pnl": unreal,
            })

        positions.sort(key=lambda p: p["market_value"], reverse=True)
        acct_total  = liquid_val or (cash + sum(p["market_value"] for p in positions))
        last4       = acct_num[-4:] if len(acct_num) >= 4 else acct_num

        # Best-effort account type: check several fields Schwab may populate
        raw_type = (
            sa.get("registeredAccountType")
            or sa.get("accountType")
            or sa.get("registration")
            or sa.get("type")
            or ""
        ).upper()

        cfg      = _load(SCHWAB_CONFIG_FILE, {})
        nickname = (cfg.get("account_nicknames") or {}).get(last4, "")
        label    = nickname or label_map.get(last4[-3:]) or raw_type or "Account"

        result_accounts.append({
            "account_number": last4,
            "type":           raw_type,
            "nickname":       nickname,
            "label":          label,
            "total_value":    acct_total,
            "cash_balance":   cash,
            "positions":      positions,
        })
        total_value += acct_total

    return jsonify({"accounts": result_accounts, "total_value": total_value})


def _parse_schwab_api_transactions(raw_txs, account_name):
    """Map Schwab API transaction objects to our internal CSV-compatible format."""
    FEE_TYPES = {"COMMISSION", "OPT_REG_FEE", "TAF_FEE", "SEC_FEE", "CDT_FEE"}
    txs = []
    for raw in raw_txs:
        tx_type = raw.get("type", "")
        if tx_type not in ("TRADE", "DIVIDEND_OR_INTEREST", "RECEIVE_AND_DELIVER"):
            continue

        trade_date = raw.get("tradeDate") or raw.get("time") or ""
        try:
            date = datetime.strptime(trade_date[:10], "%Y-%m-%d").date().isoformat()
        except (ValueError, TypeError):
            continue

        items = raw.get("transferItems", [])
        if not items:
            continue

        main_item = next((i for i in items if i.get("feeType") not in FEE_TYPES), items[0])
        inst      = main_item.get("instrument", {})
        symbol    = inst.get("symbol", "")
        asset_type = inst.get("assetType", "")
        # Convert OCC option format to human-readable so hashes match CSV imports
        if asset_type == "OPTION" or _OCC_RE.match(symbol.strip()):
            symbol = _occ_to_human(symbol)
        is_option  = asset_type == "OPTION" or bool(symbol and " " in symbol)
        price      = float(main_item.get("price") or 0)
        fees_sum   = sum(abs(float(i.get("cost") or 0))
                         for i in items if i.get("feeType") in FEE_TYPES)
        net_amt    = float(raw.get("netAmount") or 0)
        top_desc   = (raw.get("description") or "").upper()

        # Clean up cash/currency pseudo-symbols
        if asset_type in ("CURRENCY",) or symbol == "CURRENCY_USD":
            symbol = ""

        if tx_type == "TRADE":
            qty = abs(float(main_item.get("amount") or 0))
            # Skip zero-amount trades — Schwab injects synthetic opening-balance
            # records (qty=prior position, netAmount=0) at the start of their
            # data window. These are not real trades and corrupt FIFO lot matching.
            if abs(net_amt) < 0.01:
                continue
            effect = (main_item.get("positionEffect") or "").upper()
            if is_option:
                if net_amt < 0:
                    action = "Buy to Open" if effect == "OPENING" else "Buy to Close"
                else:
                    action = "Sell to Close" if effect == "CLOSING" else "Sell to Open"
            else:
                action = "Buy" if net_amt < 0 else "Sell"

        elif tx_type == "DIVIDEND_OR_INTEREST":
            qty = None
            if "REINVEST" in top_desc:
                action = "Reinvest Shares"
                qty    = abs(float(main_item.get("amount") or 0)) or None
            elif "QUALIFIED" in top_desc or "QUAL" in top_desc:
                action = "Qualified Dividend"
            elif "DIVIDEND" in top_desc or "DIV" in top_desc or "PR YR" in top_desc:
                action = "Cash Dividend"
            else:
                action = "Credit Interest"

        else:  # RECEIVE_AND_DELIVER
            qty    = abs(float(main_item.get("amount") or 0)) or None
            action = raw.get("description") or "Receive/Deliver"

        description = inst.get("description", "") or raw.get("description", "")

        tx_id = _tx_hash(date, action, symbol, qty, price, net_amt)

        txs.append({
            "id":          tx_id,
            "date":        date,
            "action":      action,
            "symbol":      symbol,
            "description": description,
            "quantity":    qty   if qty and qty > 0 else None,
            "price":       price if price > 0 else None,
            "fees":        fees_sum,
            "amount":      net_amt,
            "is_option":   is_option,
            "account_name": account_name,
        })
    return txs


@app.route("/api/schwab/fetch-transactions")
def schwab_fetch_transactions():
    """Pull recent transaction history from Schwab API and merge into trades.json."""
    access_token = _schwab_access_token()
    if not access_token:
        return jsonify({"error": "not_connected"}), 401

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    # Get account number → hash mapping
    try:
        r = requests.get(f"{SCHWAB_TRADER_URL}/accounts/accountNumbers",
                         headers=headers, timeout=10)
        r.raise_for_status()
        accounts_map = {a["accountNumber"]: a["hashValue"] for a in r.json()}
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch account numbers: {e}"}), 502

    last3_to_label = _build_label_map()

    now_dt = datetime.utcnow()
    total_added  = 0
    all_imports  = []
    fetch_errors = []

    for acct_num, hash_val in accounts_map.items():
        label      = last3_to_label.get(acct_num[-3:], f"Account ···{acct_num[-4:]}")
        acct_label = f"{label} ···{acct_num[-4:]}"

        # Fetch in 365-day windows going backwards to HISTORY_FLOOR, never
        # stopping early on empty windows (a gap year would otherwise cut off
        # older data that does exist).
        HISTORY_FLOOR = datetime(2021, 1, 1)
        all_raw = []
        window_end = now_dt
        while window_end > HISTORY_FLOOR:
            window_start = max(window_end - timedelta(days=365), HISTORY_FLOOR)
            start_s = window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            end_s   = window_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                r = requests.get(
                    f"{SCHWAB_TRADER_URL}/accounts/{hash_val}/transactions",
                    params={
                        "startDate": start_s, "endDate": end_s,
                        "types": "TRADE,DIVIDEND_OR_INTEREST,RECEIVE_AND_DELIVER",
                    },
                    headers=headers, timeout=30,
                )
                if r.status_code == 404:
                    break
                if not r.ok:
                    fetch_errors.append(f"{acct_label}: HTTP {r.status_code} — {r.text[:200]}")
                    break
                chunk = r.json()
            except requests.RequestException as e:
                fetch_errors.append(f"{acct_label}: {e}")
                break

            if not isinstance(chunk, list):
                fetch_errors.append(f"{acct_label}: unexpected format — {str(chunk)[:200]}")
                break

            print(f"  [schwab fetch] {acct_label} {start_s[:10]}→{end_s[:10]}: {len(chunk)} rows")
            all_raw.extend(chunk)
            window_end = window_start  # step back one window

        if not all_raw:
            fetch_errors.append(f"{acct_label}: 0 transactions found")
            continue

        new_txs = _parse_schwab_api_transactions(all_raw, acct_label)
        if not new_txs:
            fetch_errors.append(f"{acct_label}: {len(all_raw)} raw rows but 0 parsed")
            continue

        import_id = str(uuid.uuid4())
        for t in new_txs:
            t["import_id"] = import_id

        db = load_trades()
        existing = {t["id"] for t in db["transactions"]}
        seen_in_batch: set = set()
        added = []
        for t in new_txs:
            if t["id"] not in existing and t["id"] not in seen_in_batch:
                seen_in_batch.add(t["id"])
                added.append(t)
        dups  = len(new_txs) - len(added)

        if not added:
            fetch_errors.append(f"{acct_label}: {dups} duplicate(s), nothing new")
            continue

        db["transactions"].extend(added)
        dates = [t["date"] for t in new_txs]
        meta  = {
            "id":                     import_id,
            "filename":               f"schwab_api_{acct_num[-4:]}_{now_dt.strftime('%Y%m%d')}",
            "account_name":           acct_label,
            "notes":                  "Fetched via Schwab API",
            "imported_at":            datetime.utcnow().isoformat(),
            "date_range_from":        min(dates),
            "date_range_to":          max(dates),
            "total_rows":             len(new_txs),
            "new_transactions":       len(added),
            "duplicate_transactions": dups,
        }
        db["imports"].append(meta)
        save_trades(db)
        total_added += len(added)
        all_imports.append(meta)

    return jsonify({"added": total_added, "imports": all_imports, "errors": fetch_errors})


@app.route("/api/schwab/quotes")
def schwab_quotes():
    """Live quotes for a comma-separated list of symbols via Schwab market data."""
    symbols = request.args.get("symbols", "").strip()
    if not symbols:
        return jsonify({})

    access_token = _schwab_access_token()
    if not access_token:
        return jsonify({"error": "not_connected"}), 401

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        r = requests.get(
            f"{SCHWAB_MKT_URL}/quotes",
            params={"symbols": symbols, "fields": "quote,fundamental"},
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502

    result = {}
    for sym, info in data.items():
        q = info.get("quote", {})
        f = info.get("fundamental", {})
        price      = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
        shares     = f.get("sharesOutstanding")  # raw share count
        market_cap = (shares * price) if (shares and price) else None  # raw dollars
        result[sym] = {
            "price":      price,
            "change":     q.get("netChange"),
            "change_pct": q.get("netPercentChange"),
            "market_cap": market_cap,
        }
    return jsonify(result)


# ── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    _ensure_ssl_cert()
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ssl_ctx = (CERT_FILE, KEY_FILE)
        url = "https://127.0.0.1:8080"
        print(f"\n  Portfolio Tracker  →  {url}")
        print("  First visit: click 'Advanced' → 'Proceed to 127.0.0.1' in the browser.\n")
    else:
        ssl_ctx = None
        url = "http://localhost:8080"
        print(f"\n  Portfolio Tracker  →  {url}")
        print("  Warning: running without HTTPS — Schwab OAuth will not work.\n")
    app.run(debug=True, port=8080, ssl_context=ssl_ctx)
