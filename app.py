import csv
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date, datetime, timedelta, timezone
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
GRANTS_FILE    = os.path.join(DATA_DIR, "grants.json")

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
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
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


def _save_secure(path, data):
    """Same as _save but chmod 600 — for files containing tokens or secrets."""
    _save(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

def _safe_filename(name):
    return name.replace("/", "_").replace("\\", "_")

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
def load_grants():     return _load(GRANTS_FILE, {"grants": []})
def save_grants(d):    _save(GRANTS_FILE, d)


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
         "amount": amount, "added_at": _utcnow_iso()}
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
                except (TypeError, ValueError): return jsonify({"error": "Invalid amount"}), 400
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
        for fut in as_completed(ex.submit(fetch, s) for s in symbols):
            sym, d = fut.result()
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
        "updated_at": _utcnow_iso(),
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
                except (TypeError, ValueError): return jsonify({"error": "Invalid value"}), 400
            a["updated_at"] = _utcnow_iso()
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
        original_balance = float(data.get("original_balance", data.get("balance", 0)))
        balance          = original_balance
        monthly_payment  = float(data.get("monthly_payment", 0))
        rate_raw         = data.get("interest_rate")
        interest_rate    = float(rate_raw) if rate_raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid number"}), 400

    db   = load_loans()
    loan = {
        "id":               str(uuid.uuid4()),
        "name":             name,
        "type":             data.get("type", "other").strip(),
        "balance":          balance,
        "original_balance": original_balance,
        "start_date":       data.get("start_date", "").strip(),
        "monthly_payment":  monthly_payment,
        "interest_rate":    interest_rate,
        "notes":            data.get("notes", "").strip(),
        "updated_at":       _utcnow_iso(),
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
            for k in ("name", "type", "notes", "start_date"):
                if k in data:
                    l[k] = data[k]
            for k in ("balance", "monthly_payment", "interest_rate", "original_balance"):
                if k in data:
                    try:    l[k] = float(data[k]) if data[k] not in (None, "") else 0.0
                    except (TypeError, ValueError): return jsonify({"error": f"Invalid {k}"}), 400
            # keep balance in sync with original_balance
            if "original_balance" in data:
                l["balance"] = l["original_balance"]
            l["updated_at"] = _utcnow_iso()
            save_loans(db)
            return jsonify(l)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/loans/<lid>", methods=["DELETE"])
def delete_loan(lid):
    db = load_loans()
    db["loans"] = [l for l in db["loans"] if l["id"] != lid]
    save_loans(db)
    return "", 204


# ── Grants (RSU / stock options) ────────────────────────────────────

@app.route("/api/grants", methods=["GET"])
def get_grants():
    return jsonify(load_grants()["grants"])

@app.route("/api/grants", methods=["POST"])
def add_grant():
    data   = request.json or {}
    symbol = data.get("symbol", "").strip().upper()
    grant_date = data.get("grant_date", "").strip()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400
    if not grant_date:
        return jsonify({"error": "Grant date is required"}), 400
    try:
        grant_price          = float(data.get("grant_price", 0))
        total_shares         = float(data.get("total_shares", 0))
        vest_interval_months = int(data.get("vest_interval_months", 6))
        vest_pct_per_period  = float(data.get("vest_pct_per_period", 12.5))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid number"}), 400

    name = data.get("name", "").strip() or f"{symbol} RSU {grant_date[:4]}"
    db   = load_grants()
    grant = {
        "id":                   str(uuid.uuid4()),
        "name":                 name,
        "symbol":               symbol,
        "grant_date":           grant_date,
        "grant_price":          grant_price,
        "total_shares":         total_shares,
        "vest_interval_months": vest_interval_months,
        "vest_pct_per_period":  vest_pct_per_period,
        "notes":                data.get("notes", "").strip(),
        "created_at":           _utcnow_iso(),
    }
    db["grants"].append(grant)
    save_grants(db)
    return jsonify(grant), 201

@app.route("/api/grants/<gid>", methods=["PUT"])
def update_grant(gid):
    data = request.json or {}
    db   = load_grants()
    for g in db["grants"]:
        if g["id"] == gid:
            for k in ("name", "symbol", "grant_date", "notes"):
                if k in data:
                    g[k] = str(data[k]).strip()
            if "symbol" in data:
                g["symbol"] = g["symbol"].upper()
            for k in ("grant_price", "total_shares", "vest_pct_per_period"):
                if k in data:
                    try:    g[k] = float(data[k])
                    except (TypeError, ValueError): return jsonify({"error": f"Invalid {k}"}), 400
            if "vest_interval_months" in data:
                try:    g["vest_interval_months"] = int(data["vest_interval_months"])
                except (TypeError, ValueError): return jsonify({"error": "Invalid vest_interval_months"}), 400
            if not g.get("name"):
                g["name"] = f"{g['symbol']} RSU {g['grant_date'][:4]}"
            save_grants(db)
            return jsonify(g)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/grants/<gid>", methods=["DELETE"])
def delete_grant(gid):
    db = load_grants()
    db["grants"] = [g for g in db["grants"] if g["id"] != gid]
    save_grants(db)
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
    BUY_ACTIONS   = {"Buy", "Buy to Open", "Reinvest Shares", "Buy to Close"}
    SELL_ACTIONS  = {"Sell", "Sell to Close", "Sell to Open"}
    DIV_ACTIONS   = {"Cash Dividend", "Qualified Dividend", "Credit Interest",
                     "Non-Qualified Div", "Special Dividend"}
    SPLIT_ACTIONS = {"Stock Split", "Forward Split", "Reverse Split"}

    # FIFO queue keyed by (symbol, account) so cross-account sells don't consume
    # shares that live in a different brokerage. Schwab tracks basis per-account
    # for tax purposes; matching that here keeps LT/ST classification correct.
    open_lots    = {}   # (sym, account) -> list[lot dict]
    closed_lots  = []   # all matched lot records
    dividends    = 0.0
    descriptions = {}
    buy_counts   = {}
    sell_counts  = {}
    last_tx_dates = {}  # sym -> most-recent any-transaction date

    # Within a single date, run buys/splits before sells so same-day buys
    # can cover same-day sells (Schwab CSVs only have date precision, not
    # intraday timestamps, so without this a same-day sell-then-buy orphans).
    def _action_priority(action):
        if action in BUY_ACTIONS or action in SPLIT_ACTIONS:
            return 0
        if action in SELL_ACTIONS:
            return 1
        return 2  # dividends, interest, etc. — last

    for tx in sorted(transactions, key=lambda x: (x["date"], _action_priority(x["action"]))):
        sym     = tx["symbol"]
        action  = tx["action"]
        qty     = tx["quantity"] or 0
        amount  = tx["amount"]   or 0
        account = tx.get("account_name", "Unknown")
        desc    = tx.get("description", "")
        key     = (sym, account)

        if sym and desc and sym not in descriptions:
            descriptions[sym] = desc

        if sym and tx["date"]:
            if sym not in last_tx_dates or tx["date"] > last_tx_dates[sym]:
                last_tx_dates[sym] = tx["date"]

        if action in BUY_ACTIONS and sym and qty > 1e-9:
            cost = abs(amount)
            open_lots.setdefault(key, []).append({
                "open_date":     tx["date"],
                "qty":           qty,
                "remaining_qty": qty,
                "unit_cost":     cost / qty,
                "account":       account,
            })
            buy_counts[sym] = buy_counts.get(sym, 0) + 1

        elif action in SPLIT_ACTIONS and sym and qty > 1e-9:
            # Scale all open lots for this (symbol, account) — adds shares without
            # changing total cost basis. qty here is the NUMBER OF SHARES ADDED.
            # Per-share cost basis = total_cost / (existing_qty + added_qty).
            lots = open_lots.get(key, [])
            existing_qty = sum(l["remaining_qty"] for l in lots if l["remaining_qty"] > 1e-9)
            if existing_qty <= 1e-9:
                continue  # nothing to split
            ratio = (existing_qty + qty) / existing_qty
            for l in lots:
                if l["remaining_qty"] <= 1e-9:
                    continue
                l["remaining_qty"] *= ratio
                l["qty"]           *= ratio
                l["unit_cost"]     /= ratio

        elif action in SELL_ACTIONS and sym and qty > 1e-9:
            proceeds       = abs(amount)
            proceeds_per_u = proceeds / qty
            remaining_sell = qty
            sell_counts[sym] = sell_counts.get(sym, 0) + 1

            lots = open_lots.get(key, [])
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

            open_lots[key] = [l for l in lots if l["remaining_qty"] > 1e-9]

            # Orphan sell — shares sold from an account with no matching buy in
            # history (CSV window doesn't reach back far enough, or transferred in).
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

    # Attach open positions — aggregate across all (sym, account) buckets per symbol
    open_by_sym = {}   # sym -> [lot, lot, ...]
    for (sym, _acct), lots in open_lots.items():
        for l in lots:
            if l["remaining_qty"] > 1e-9:
                open_by_sym.setdefault(sym, []).append(l)

    for sym, remaining in open_by_sym.items():
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

    # ── LT/ST splits for tax tracking ────────────────────────────
    # Long-term = holding period > 365 days (US IRS threshold).
    # Realized split is calendar year-to-date (since Jan 1) — matches how
    # capital gains are reported on Schedule D / Form 8949.
    today      = _date.today()
    year_start = _date(today.year, 1, 1).isoformat()

    # Per-symbol split of OPEN lots by current holding period.
    # Frontend layers on current price to compute unrealized LT/ST.
    for sym, s in by_symbol.items():
        lt_qty = st_qty = lt_basis = st_basis = 0.0
        for l in s.get("open_lots", []):
            if l.get("open_date"):
                held_days = (today - _date.fromisoformat(l["open_date"])).days
            else:
                held_days = 0
            qty  = l["qty"]
            cost = l["unit_cost"] * qty
            if held_days > 365:
                lt_qty   += qty
                lt_basis += cost
            else:
                st_qty   += qty
                st_basis += cost
        s["open_lt_qty"]      = round(lt_qty, 6)
        s["open_st_qty"]      = round(st_qty, 6)
        s["open_lt_basis"]    = lt_basis
        s["open_st_basis"]    = st_basis
        s["open_lt_avg_cost"] = lt_basis / lt_qty if lt_qty > 1e-9 else 0.0
        s["open_st_avg_cost"] = st_basis / st_qty if st_qty > 1e-9 else 0.0

    # Realized P&L year-to-date (current calendar year), split LT/ST.
    realized_lt_ytd = 0.0
    realized_st_ytd = 0.0
    for lot in closed_lots:
        if lot.get("orphan") or lot.get("realized_pnl") is None:
            continue
        if (lot.get("close_date") or "") < year_start:
            continue
        if lot.get("is_long_term"):
            realized_lt_ytd += lot["realized_pnl"]
        else:
            realized_st_ytd += lot["realized_pnl"]

    return {
        "by_symbol":          by_symbol,
        "total_realized_pnl": sum(v["realized_pnl"] for v in by_symbol.values()),
        "total_dividends":    dividends,
        "total_fees":         0.0,   # fees are baked into Schwab amounts
        "realized_lt_ytd":    realized_lt_ytd,
        "realized_st_ytd":    realized_st_ytd,
        "realized_ytd_year":  today.year,
        "sharpe_by_year":     _calc_sharpe_stats(closed_lots),
        "sharpe_risk_free":   SHARPE_RISK_FREE_ANNUAL,
    }


SHARPE_RISK_FREE_ANNUAL = 0.05  # 5% — adjust to match prevailing T-bill rate


def _calc_sharpe_stats(closed_lots):
    """
    Monthly return series from settled (closed) lots only.
    Monthly return = realized_pnl / cost_basis for all lots closed that month.
    Annual Sharpe  = (mean_monthly_excess / std_monthly) * sqrt(12).
    """
    from collections import defaultdict
    import statistics

    monthly = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0})
    for lot in closed_lots:
        if lot.get("orphan") or lot.get("cost_basis") is None or lot.get("realized_pnl") is None:
            continue
        cd = lot.get("close_date")
        if not cd:
            continue
        ym = cd[:7]  # "YYYY-MM"
        monthly[ym]["pnl"]  += lot["realized_pnl"]
        monthly[ym]["cost"] += lot["cost_basis"]

    # Only months where we actually had capital at work
    monthly_returns = {ym: d["pnl"] / d["cost"]
                       for ym, d in monthly.items() if d["cost"] > 0}

    rfm = SHARPE_RISK_FREE_ANNUAL / 12  # monthly risk-free rate

    def _stats(returns_list, ym_set):
        if len(returns_list) < 2:
            return None
        mean_r   = sum(returns_list) / len(returns_list)
        mean_ex  = sum(r - rfm for r in returns_list) / len(returns_list)
        std_r    = statistics.stdev(returns_list)
        sharpe   = (mean_ex / std_r) * (12 ** 0.5) if std_r > 1e-9 else 0.0
        total_pnl  = sum(monthly[ym]["pnl"]  for ym in ym_set)
        total_cost = sum(monthly[ym]["cost"] for ym in ym_set)
        roc = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        return {
            "sharpe":              round(sharpe, 3),
            "return_on_capital":   round(roc, 2),
            "mean_monthly_return": round(mean_r * 100, 4),
            "std_monthly":         round(std_r * 100, 4),
            "months":              len(returns_list),
            "total_pnl":           round(total_pnl, 2),
            "total_cost":          round(total_cost, 2),
        }

    # Group by year
    by_year = defaultdict(list)
    for ym, ret in monthly_returns.items():
        by_year[ym[:4]].append(ym)

    result = {}
    for year in sorted(by_year.keys()):
        ym_set   = set(by_year[year])
        rets     = [monthly_returns[ym] for ym in sorted(ym_set)]
        stats    = _stats(rets, ym_set)
        if stats:
            result[year] = stats

    all_ym  = set(monthly_returns.keys())
    all_ret = list(monthly_returns.values())
    stats   = _stats(all_ret, all_ym)
    if stats:
        result["all"] = stats

    return result


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
    safe_name = _safe_filename(f.filename)
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
        "imported_at":            _utcnow_iso(),
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
            except (TypeError, ValueError): return 0.0

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

    safe_name = _safe_filename(f.filename)
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
        "imported_at":            _utcnow_iso(),
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
    _save_secure(SCHWAB_TOKEN_FILE, d)


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
            "connected_at":      _utcnow_iso(),
        })
        return redirect("/?schwab_connected=1")
    except requests.HTTPError:
        return redirect(f"/?schwab_error=token_exchange_failed_{r.status_code}")
    except Exception:
        return redirect("/?schwab_error=unknown")


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


def _build_label_map(cfg=None):
    """Return last3 → human label, preferring nicknames then CSV filenames."""
    if cfg is None:
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
            if os.path.exists(SCHWAB_TOKEN_FILE):
                os.remove(SCHWAB_TOKEN_FILE)
            return jsonify({"error": "token_expired"}), 401
        r.raise_for_status()
        accounts_data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": f"Schwab API error: {e}"}), 502

    cfg = _load(SCHWAB_CONFIG_FILE, {})
    nicknames = cfg.get("account_nicknames") or {}
    label_map = _build_label_map(cfg)
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
                "strike_price":   float(inst["strikePrice"]) if inst.get("strikePrice") is not None else None,
                "put_call":       inst.get("putCall", ""),
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

        nickname = nicknames.get(last4, "")
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

    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
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
            "imported_at":            _utcnow_iso(),
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


# ── Credit Card & Bank ─────────────────────────────────────────────

APPLE_CARD_FILE = os.path.join(DATA_DIR, "apple_card.json")
BANK_FILE       = os.path.join(DATA_DIR, "bank.json")


def _load_apple_card():
    d = _load(APPLE_CARD_FILE, {"transactions": [], "imports": []})
    d.setdefault("imports", [])
    return d


def _save_apple_card(data):
    _save(APPLE_CARD_FILE, data)


@app.route("/apple-card")
@app.route("/transactions")
def apple_card_page():
    return render_template("apple_card.html")


@app.route("/api/apple-card/transactions")
def apple_card_transactions():
    return jsonify(_load_apple_card())


def _detect_cc_format(headers):
    h = {k.strip().lower() for k in headers}
    if "amount (usd)" in h:
        return "apple"
    if "date" in h and "name" in h:
        return "fidelity"
    if "transaction date" in h and "post date" in h:
        return "chase"
    return "apple"  # default fallback


def _parse_cc_row(row, fmt):
    """Normalize a CSV row from any supported CC format into a common dict.
    Returns None if the row should be skipped (e.g. payment rows)."""
    if fmt == "fidelity":
        tx_date    = row.get("Date", "")
        merchant   = row.get("Name", "").strip()
        amount_raw = row.get("Amount", "0")
        # Fidelity: negative = purchase (debit), positive = payment (credit)
        # Normalise to Apple convention: positive = purchase, negative = payment
        try:
            amount = -float(amount_raw.replace(",", "").replace("$", ""))
        except ValueError:
            amount = 0.0
        tx_type = "Payment" if amount < 0 else "Purchase"
        return dict(
            tx_date=tx_date, clearing_date="",
            description=merchant, merchant=merchant,
            category="Other", type=tx_type,
            amount=amount, purchaser="",
        )
    elif fmt == "chase":
        tx_date    = row.get("Transaction Date", "")
        clear_date = row.get("Post Date", "")
        desc       = row.get("Description", "").strip()
        category   = row.get("Category", "Other").strip() or "Other"
        type_raw   = row.get("Type", "").strip().lower()
        amount_raw = row.get("Amount", "0")
        memo       = row.get("Memo", "").strip()
        try:
            # Chase: Sale is negative (charge), Payment is positive (credit)
            # Negate so positive = charge, matching Apple Card convention
            amount = -float(amount_raw.replace(",", "").replace("$", ""))
        except ValueError:
            amount = 0.0
        tx_type = "Payment" if type_raw in ("payment", "credit", "return") else "Purchase"
        merchant = desc
        full_desc = f"{desc} — {memo}" if memo else desc
        return dict(
            tx_date=tx_date, clearing_date=clear_date,
            description=full_desc, merchant=merchant,
            category=category, type=tx_type,
            amount=amount, purchaser="",
        )
    else:  # apple
        tx_date    = row.get("Transaction Date", "")
        clear_date = row.get("Clearing Date", "")
        desc       = row.get("Description", "")
        merchant   = row.get("Merchant", "") or desc
        category   = row.get("Category", "Other")
        tx_type    = row.get("Type", "Purchase")
        amount_raw = row.get("Amount (USD)", "0")
        purchaser  = row.get("Purchased By", "")
        try:
            amount = float(amount_raw.replace(",", "").replace("$", ""))
        except ValueError:
            amount = 0.0
        return dict(
            tx_date=tx_date, clearing_date=clear_date,
            description=desc, merchant=merchant,
            category=category, type=tx_type,
            amount=amount, purchaser=purchaser,
        )


@app.route("/api/apple-card/imports/<import_id>", methods=["DELETE"])
def apple_card_delete_import(import_id):
    data = _load_apple_card()
    target = next((i for i in data["imports"] if i["id"] == import_id), None)
    if not target:
        return jsonify({"error": "Not found"}), 404
    before = len(data["transactions"])
    data["transactions"] = [t for t in data["transactions"] if t.get("import_id") != import_id]
    data["imports"] = [i for i in data["imports"] if i["id"] != import_id]
    _save_apple_card(data)
    return jsonify({"removed_transactions": before - len(data["transactions"])})


@app.route("/api/apple-card/import", methods=["POST"])
def apple_card_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400

    filename = _safe_filename(f.filename)
    card_name = request.form.get("card_name", "").strip()

    content = f.read().decode("utf-8-sig")
    reader = csv.DictReader(StringIO(content))
    fmt = _detect_cc_format(reader.fieldnames or [])

    if not card_name:
        card_name = "Apple Card" if fmt == "apple" else ("Fidelity" if fmt == "fidelity" else "Credit Card")

    import_id = str(uuid.uuid4())
    data = _load_apple_card()
    existing_hashes = {t["hash"] for t in data["transactions"]}

    new_rows, dupes = [], 0
    for row in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in row.items()}
        parsed = _parse_cc_row(row, fmt)
        if parsed is None:
            continue

        h = hashlib.md5(f"{parsed['tx_date']}|{parsed['description']}|{parsed['amount']}".encode()).hexdigest()
        if h in existing_hashes:
            dupes += 1
            continue

        existing_hashes.add(h)
        new_rows.append({
            "hash":      h,
            "id":        str(uuid.uuid4()),
            "import_id": import_id,
            "card_name": card_name,
            "source":    fmt,
            **parsed,
        })

    data["transactions"].extend(new_rows)
    data["imports"].append({
        "id":                   import_id,
        "filename":             filename,
        "card_name":            card_name,
        "source":               fmt,
        "imported_at":          _utcnow_iso(),
        "new_transactions":     len(new_rows),
        "duplicate_transactions": dupes,
    })
    _save_apple_card(data)

    return jsonify({"imported": len(new_rows), "duplicates": dupes, "total": len(data["transactions"]), "card_name": card_name})


@app.route("/api/apple-card/clear", methods=["POST"])
def apple_card_clear():
    _save_apple_card({"transactions": [], "imports": []})
    return jsonify({"ok": True})


# ── Bank ────────────────────────────────────────────────────────────

# Keywords that identify a credit-card payment (debit from bank to pay CC bill)
_CC_PAYMENT_PATTERNS = re.compile(
    r"(apple card|credit card|apple pay|card payment|autopay|online payment"
    r"|echeck payment|payment thank you|minimum payment|balance payment)",
    re.IGNORECASE,
)


def _load_bank():
    d = _load(BANK_FILE, {"transactions": [], "imports": []})
    d.setdefault("imports", [])
    return d


def _save_bank(data):
    _save(BANK_FILE, data)


@app.route("/api/bank/transactions")
def bank_transactions():
    return jsonify(_load_bank())


@app.route("/api/bank/imports/<import_id>", methods=["DELETE"])
def bank_delete_import(import_id):
    data = _load_bank()
    target = next((i for i in data["imports"] if i["id"] == import_id), None)
    if not target:
        return jsonify({"error": "Not found"}), 404
    before = len(data["transactions"])
    data["transactions"] = [t for t in data["transactions"] if t.get("import_id") != import_id]
    data["imports"] = [i for i in data["imports"] if i["id"] != import_id]
    _save_bank(data)
    return jsonify({"removed_transactions": before - len(data["transactions"])})


def _norm_tx_date(s):
    """Normalize CSV (MM/DD/YY) and Plaid (ISO) dates to YYYY-MM-DD for matching."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _bank_match_key(t):
    """(date, abs(amount), account-last4). Used to detect the same real-world
    transaction across Plaid + CSV imports, which hash differently."""
    acct = str(t.get("account") or "")
    return (
        _norm_tx_date(t.get("tx_date")),
        round(abs(float(t.get("amount") or 0)), 2),
        acct[-4:],
    )


def _dedup_bank_cross_source(data):
    """Whenever a CSV row exists for a (date, amount, last-4) key, drop every
    non-pending Plaid row for that key. CSV is treated as the bank's
    authoritative record (longer history, fuller descriptions, no
    Plaid-side double-reporting). Pending Plaid rows are preserved because
    they typically haven't posted to the CSV yet."""
    from collections import defaultdict
    groups = defaultdict(lambda: {"csv": [], "plaid": []})
    for idx, t in enumerate(data["transactions"]):
        bucket = "plaid" if t.get("plaid_id") else "csv"
        groups[_bank_match_key(t)][bucket].append(idx)

    drop = set()
    for k, g in groups.items():
        if not g["csv"]:
            continue
        for i in g["plaid"]:
            if not data["transactions"][i].get("pending"):
                drop.add(i)

    if drop:
        data["transactions"] = [
            t for i, t in enumerate(data["transactions"]) if i not in drop
        ]
    return len(drop)


@app.route("/api/bank/dedup-sources", methods=["POST"])
def bank_dedup_sources():
    """One-shot cleanup of cross-source duplicates already in bank.json."""
    data = _load_bank()
    removed = _dedup_bank_cross_source(data)
    if removed:
        _save_bank(data)
    return jsonify({"removed": removed, "total": len(data["transactions"])})


@app.route("/api/bank/import", methods=["POST"])
def bank_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400

    filename = _safe_filename(f.filename)
    account_name = request.form.get("account_name", "").strip() or "Bank Account"

    content = f.read().decode("utf-8-sig")
    reader = csv.DictReader(StringIO(content))

    data = _load_bank()
    existing_hashes = {t["hash"] for t in data["transactions"]}

    # Periodic re-imports for the same account (e.g. Capital One's 90-day
    # CSV cap, re-pulled quarterly) collapse into a single imports[] entry so
    # the user sees one growing record instead of N stacked rows. Matched
    # case-insensitively on account_name; rename to keep imports separate.
    match_key = account_name.casefold()
    existing_import = next(
        (i for i in data["imports"]
         if (i.get("account_name") or "").casefold() == match_key),
        None,
    )
    import_id = existing_import["id"] if existing_import else str(uuid.uuid4())

    def _pick(row, *keys, default=""):
        for k in keys:
            if row.get(k, "").strip():
                return row[k].strip()
        return default

    def _parse_amount(raw):
        try:
            return float(raw.replace(",", "").replace("$", "").strip())
        except (ValueError, AttributeError):
            return 0.0

    def _normalize_type(raw, amount):
        """Return canonical 'Credit' or 'Debit'. Falls back to amount sign."""
        t = raw.strip().lower()
        if t in ("credit", "cr", "deposit", "dep", "incoming", "received"):
            return "Credit"
        if t in ("debit", "dr", "withdrawal", "wd", "wdl", "outgoing", "payment", "purchase"):
            return "Debit"
        # Infer from sign: positive = credit, negative = debit
        return "Credit" if amount > 0 else "Debit"

    new_rows, dupes, skipped_cc = [], 0, 0
    for row in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in row.items()}

        acct_num = _pick(row, "Account Number", "Account #", "AccountNumber", "Acct", default="")
        desc     = _pick(row, "Transaction Description", "Description", "Memo", "Payee", "Details", default="")
        tx_date  = _pick(row, "Transaction Date", "Date", "Posting Date", "Post Date", default="")
        balance  = _pick(row, "Balance", "Running Balance", "Ledger Balance", default="")

        # Amount: try combined column first, then separate debit/credit columns
        amt_raw = _pick(row, "Transaction Amount", "Amount", "Transaction Amount (USD)", default="")
        if not amt_raw:
            # Some CSVs split into Credit Amount / Debit Amount columns
            credit_raw = _pick(row, "Credit Amount", "Credit", "Deposits", "Deposit Amount", default="")
            debit_raw  = _pick(row, "Debit Amount",  "Debit",  "Withdrawals", "Withdrawal Amount", default="")
            if credit_raw:
                amt_raw = credit_raw
            elif debit_raw:
                amt_raw = "-" + debit_raw.lstrip("-")
            else:
                amt_raw = "0"

        amount   = _parse_amount(amt_raw)
        type_raw = _pick(row, "Transaction Type", "Type", "Transaction Type Code", "Tran Type", default="")
        tx_type  = _normalize_type(type_raw, amount)

        # Skip credit card payment rows
        if _CC_PAYMENT_PATTERNS.search(desc):
            skipped_cc += 1
            continue

        # Use absolute amount; tx_type carries the direction
        amount = abs(amount)

        h = hashlib.md5(f"{acct_num}|{tx_date}|{desc}|{amt_raw}".encode()).hexdigest()
        if h in existing_hashes:
            dupes += 1
            continue

        existing_hashes.add(h)
        new_rows.append({
            "hash":         h,
            "id":           str(uuid.uuid4()),
            "import_id":    import_id,
            "account_name": account_name,
            "account":      acct_num,
            "description":  desc,
            "tx_date":      tx_date,
            "tx_type":      tx_type,
            "amount":       amount,
            "balance":      balance,
        })

    data["transactions"].extend(new_rows)

    # If a Plaid sync already pulled some of these rows, drop the Plaid copy
    # (CSV is more detailed and is the long-term archive).
    cross_removed = _dedup_bank_cross_source(data)

    now_iso = _utcnow_iso()
    refresh_record = {
        "filename":               filename,
        "imported_at":            now_iso,
        "new_transactions":       len(new_rows),
        "duplicate_transactions": dupes,
        "skipped_cc":             skipped_cc,
    }
    if existing_import:
        existing_import["filename"]               = filename       # latest CSV
        existing_import["imported_at"]            = now_iso        # latest run
        existing_import["new_transactions"]       = (existing_import.get("new_transactions") or 0) + len(new_rows)
        existing_import["duplicate_transactions"] = (existing_import.get("duplicate_transactions") or 0) + dupes
        existing_import["skipped_cc"]             = (existing_import.get("skipped_cc") or 0) + skipped_cc
        existing_import.setdefault("refreshes", []).append(refresh_record)
    else:
        data["imports"].append({
            "id":                     import_id,
            "filename":               filename,
            "account_name":           account_name,
            "imported_at":            now_iso,
            "new_transactions":       len(new_rows),
            "duplicate_transactions": dupes,
            "skipped_cc":             skipped_cc,
            "refreshes":              [refresh_record],
        })
    _save_bank(data)

    return jsonify({
        "imported":         len(new_rows),
        "duplicates":       dupes,
        "skipped_cc":       skipped_cc,
        "total":            len(data["transactions"]),
        "merged":           existing_import is not None,
        "plaid_superseded": cross_removed,
    })


@app.route("/api/bank/clear", methods=["POST"])
def bank_clear():
    _save_bank({"transactions": [], "imports": []})
    return jsonify({"ok": True})


# ── Plaid ──────────────────────────────────────────────────────────

PLAID_CONFIG_FILE = os.path.join(DATA_DIR, "plaid_config.json")
PLAID_ITEMS_FILE  = os.path.join(DATA_DIR, "plaid_items.json")
PLAID_ENV_URLS = {
    "sandbox":    "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


def _plaid_config():
    """Return {client_id, secret, env} or None if not configured.
    Tuple form: (cfg_or_None, error_message_or_None)."""
    if not os.path.exists(PLAID_CONFIG_FILE):
        return None
    try:
        with open(PLAID_CONFIG_FILE) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  plaid_config.json parse error: line {e.lineno} col {e.colno}: {e.msg}")
        return None
    if not cfg or not cfg.get("client_id") or not cfg.get("secret"):
        return None
    cfg.setdefault("env", "sandbox")
    return cfg


def _plaid_config_error():
    """Return a human-readable parse error if the config exists but is malformed."""
    if not os.path.exists(PLAID_CONFIG_FILE):
        return None
    try:
        with open(PLAID_CONFIG_FILE) as f:
            json.load(f)
        return None
    except json.JSONDecodeError as e:
        return f"plaid_config.json line {e.lineno}, col {e.colno}: {e.msg}"


def _plaid_base_url(cfg):
    return PLAID_ENV_URLS.get(cfg.get("env", "sandbox"), PLAID_ENV_URLS["sandbox"])


def _plaid_post(cfg, path, body):
    """POST to Plaid API. Always injects client_id + secret. Raises on error."""
    payload = {"client_id": cfg["client_id"], "secret": cfg["secret"], **body}
    r = requests.post(
        f"{_plaid_base_url(cfg)}{path}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error_message": r.text[:200]}
        raise RuntimeError(err.get("error_message") or err.get("error_code") or f"Plaid {r.status_code}")
    return r.json()


def _load_plaid_items():
    d = _load(PLAID_ITEMS_FILE, {"items": []})
    d.setdefault("items", [])
    return d


def _save_plaid_items(data):
    _save_secure(PLAID_ITEMS_FILE, data)


def _plaid_item_public(item):
    """Strip access_token before returning to frontend."""
    return {k: v for k, v in item.items() if k != "access_token"}


@app.route("/api/plaid/status")
def plaid_status():
    cfg = _plaid_config()
    items = _load_plaid_items()["items"]
    return jsonify({
        "configured":  cfg is not None,
        "env":         cfg["env"] if cfg else None,
        "config_error": _plaid_config_error(),
        "items":       [_plaid_item_public(i) for i in items],
    })


@app.route("/api/plaid/link-token", methods=["POST"])
def plaid_link_token():
    cfg = _plaid_config()
    if not cfg:
        return jsonify({"error": "Plaid not configured. Create data/plaid_config.json."}), 400

    body = request.get_json(silent=True) or {}
    item_id = body.get("item_id")  # if provided, generate update-mode token

    req = {
        "client_name":   "Portfolio Tracker",
        "country_codes": ["US"],
        "language":      "en",
        "user":          {"client_user_id": "portfolio-tracker-local"},
        # Pull max history Plaid allows (~2 years). Default would be 90 days.
        "transactions":  {"days_requested": 730},
    }

    # Required for OAuth institutions (Chase, BofA, etc.) in production.
    # Must exactly match a redirect URI registered in the Plaid dashboard.
    redirect_uri = cfg.get("redirect_uri")
    if redirect_uri:
        req["redirect_uri"] = redirect_uri

    if item_id:
        item = next((i for i in _load_plaid_items()["items"] if i["item_id"] == item_id), None)
        if not item:
            return jsonify({"error": "Item not found"}), 404
        req["access_token"] = item["access_token"]
    else:
        req["products"] = ["transactions"]

    try:
        res = _plaid_post(cfg, "/link/token/create", req)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"link_token": res["link_token"], "expiration": res.get("expiration")})


@app.route("/plaid-callback")
def plaid_callback():
    """OAuth return URL for institutions like Chase that redirect off-site for auth.
    Plaid Link is re-opened in 'received-redirect' mode, picks up the URL params,
    then fires onSuccess like a normal connect."""
    return render_template("plaid_callback.html")


@app.route("/api/plaid/exchange", methods=["POST"])
def plaid_exchange():
    cfg = _plaid_config()
    if not cfg:
        return jsonify({"error": "Plaid not configured"}), 400

    body = request.get_json(silent=True) or {}
    public_token = body.get("public_token")
    if not public_token:
        return jsonify({"error": "Missing public_token"}), 400

    try:
        exch = _plaid_post(cfg, "/item/public_token/exchange", {"public_token": public_token})
        access_token = exch["access_token"]
        item_id      = exch["item_id"]

        # Pull accounts + institution name
        acct_res  = _plaid_post(cfg, "/accounts/get", {"access_token": access_token})
        inst_id   = acct_res.get("item", {}).get("institution_id")
        inst_name = "Unknown"
        if inst_id:
            inst_res = _plaid_post(cfg, "/institutions/get_by_id", {
                "institution_id": inst_id,
                "country_codes":  ["US"],
            })
            inst_name = inst_res.get("institution", {}).get("name", inst_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    accounts = [{
        "account_id": a["account_id"],
        "name":       a.get("name", ""),
        "mask":       a.get("mask", ""),
        "type":       a.get("type", ""),
        "subtype":    a.get("subtype", ""),
    } for a in acct_res.get("accounts", [])]

    data = _load_plaid_items()
    # Replace if same item_id already exists (re-auth)
    data["items"] = [i for i in data["items"] if i["item_id"] != item_id]
    data["items"].append({
        "item_id":           item_id,
        "access_token":      access_token,
        "institution_id":    inst_id,
        "institution_name":  inst_name,
        "accounts":          accounts,
        "cursor":            "",
        "connected_at":      _utcnow_iso(),
        "last_synced_at":    None,
    })
    _save_plaid_items(data)

    return jsonify({
        "ok": True,
        "item_id": item_id,
        "institution_name": inst_name,
        "accounts": accounts,
    })


def _plaid_tx_hash(plaid_id):
    return hashlib.md5(f"plaid|{plaid_id}".encode()).hexdigest()


def _ingest_plaid_tx_bank(tx, account, item, import_id):
    """Map a Plaid tx onto bank.json schema. Plaid: positive amount = outflow."""
    amt = float(tx.get("amount") or 0)
    tx_type = "Debit" if amt >= 0 else "Credit"
    mask = account.get("mask") or ""
    label = account.get("name") or item.get("institution_name", "Bank")
    if mask:
        label = f"{label} ···{mask}"
    desc = tx.get("merchant_name") or tx.get("name") or ""
    return {
        "hash":             _plaid_tx_hash(tx["transaction_id"]),
        "id":               str(uuid.uuid4()),
        "import_id":        import_id,
        "account_name":     label,
        "account":          mask,
        "description":      desc,
        "tx_date":          tx.get("date") or tx.get("authorized_date") or "",
        "tx_type":          tx_type,
        "amount":           abs(amt),
        "balance":          "",
        "plaid_id":         tx["transaction_id"],
        "plaid_account_id": tx["account_id"],
        "pending":          bool(tx.get("pending")),
    }


def _ingest_plaid_tx_cc(tx, account, item, import_id):
    """Map a Plaid tx onto apple_card.json schema. Apple convention: positive = purchase."""
    amt = float(tx.get("amount") or 0)
    tx_type = "Payment" if amt < 0 else "Purchase"
    mask = account.get("mask") or ""
    card_label = account.get("name") or item.get("institution_name", "Credit Card")
    if mask:
        card_label = f"{card_label} ···{mask}"
    desc = tx.get("merchant_name") or tx.get("name") or ""
    pfc = (tx.get("personal_finance_category") or {}).get("primary") or "Other"
    return {
        "hash":             _plaid_tx_hash(tx["transaction_id"]),
        "id":               str(uuid.uuid4()),
        "import_id":        import_id,
        "card_name":        card_label,
        "source":           "plaid",
        "tx_date":          tx.get("date") or tx.get("authorized_date") or "",
        "clearing_date":    tx.get("date") or "",
        "description":      desc,
        "merchant":         desc,
        "category":         pfc.replace("_", " ").title(),
        "type":             tx_type,
        "amount":           amt,
        "purchaser":        "",
        "plaid_id":         tx["transaction_id"],
        "plaid_account_id": tx["account_id"],
        "pending":          bool(tx.get("pending")),
    }


def _ensure_plaid_import_record(data, import_id, label, source_filename):
    """Make sure an imports[] record exists for this Plaid item. Return ref."""
    rec = next((i for i in data["imports"] if i["id"] == import_id), None)
    if rec:
        return rec
    rec = {
        "id":                     import_id,
        "filename":                source_filename,
        "account_name":           label,   # bank
        "card_name":              label,   # cc
        "source":                 "plaid",
        "imported_at":            _utcnow_iso(),
        "new_transactions":       0,
        "duplicate_transactions": 0,
    }
    data["imports"].append(rec)
    return rec


@app.route("/api/plaid/sync", methods=["POST"])
def plaid_sync():
    cfg = _plaid_config()
    if not cfg:
        return jsonify({"error": "Plaid not configured"}), 400

    body = request.get_json(silent=True) or {}
    only_item_id = body.get("item_id")  # optional: sync just one

    items_data = _load_plaid_items()
    items = items_data["items"]
    if only_item_id:
        items = [i for i in items if i["item_id"] == only_item_id]
        if not items:
            return jsonify({"error": "Item not found"}), 404

    bank_data = _load_bank()
    cc_data   = _load_apple_card()
    bank_hashes = {t["hash"] for t in bank_data["transactions"]}
    cc_hashes   = {t["hash"] for t in cc_data["transactions"]}

    # Index existing plaid txs by plaid_id for modified/removed handling
    bank_by_plaid = {t.get("plaid_id"): t for t in bank_data["transactions"] if t.get("plaid_id")}
    cc_by_plaid   = {t.get("plaid_id"): t for t in cc_data["transactions"]   if t.get("plaid_id")}

    results = []
    for item in items:
        accts = {a["account_id"]: a for a in item.get("accounts", [])}
        cursor = item.get("cursor") or ""
        added_count = modified_count = removed_count = 0
        loops = 0

        while True:
            loops += 1
            if loops > 20:  # safety: max 20 pages
                break
            req = {"access_token": item["access_token"], "count": 500}
            if cursor:
                req["cursor"] = cursor
            try:
                res = _plaid_post(cfg, "/transactions/sync", req)
            except RuntimeError as e:
                results.append({"item_id": item["item_id"], "error": str(e)})
                break

            for tx in res.get("added", []):
                acct = accts.get(tx["account_id"], {})
                atype = acct.get("type", "depository")
                if atype == "credit":
                    label = (acct.get("name") or item.get("institution_name", "Credit Card"))
                    if acct.get("mask"):
                        label += f" ···{acct['mask']}"
                    rec = _ensure_plaid_import_record(cc_data, item["item_id"], label, f"Plaid · {item.get('institution_name','')}")
                    new_tx = _ingest_plaid_tx_cc(tx, acct, item, item["item_id"])
                    if new_tx["hash"] in cc_hashes:
                        continue
                    cc_hashes.add(new_tx["hash"])
                    cc_data["transactions"].append(new_tx)
                    cc_by_plaid[new_tx["plaid_id"]] = new_tx
                    rec["new_transactions"] = rec.get("new_transactions", 0) + 1
                    added_count += 1
                elif atype == "depository":
                    label = (acct.get("name") or item.get("institution_name", "Bank"))
                    if acct.get("mask"):
                        label += f" ···{acct['mask']}"
                    rec = _ensure_plaid_import_record(bank_data, item["item_id"], label, f"Plaid · {item.get('institution_name','')}")
                    new_tx = _ingest_plaid_tx_bank(tx, acct, item, item["item_id"])
                    if new_tx["hash"] in bank_hashes:
                        continue
                    bank_hashes.add(new_tx["hash"])
                    bank_data["transactions"].append(new_tx)
                    bank_by_plaid[new_tx["plaid_id"]] = new_tx
                    rec["new_transactions"] = rec.get("new_transactions", 0) + 1
                    added_count += 1
                # else: skip investment/loan/other for now

            for tx in res.get("modified", []):
                pid = tx["transaction_id"]
                acct = accts.get(tx["account_id"], {})
                atype = acct.get("type", "depository")
                existing = cc_by_plaid.get(pid) if atype == "credit" else bank_by_plaid.get(pid)
                if not existing:
                    continue
                updated = (_ingest_plaid_tx_cc if atype == "credit" else _ingest_plaid_tx_bank)(
                    tx, acct, item, item["item_id"]
                )
                # Preserve id, but update everything else
                existing.update({k: v for k, v in updated.items() if k != "id"})
                modified_count += 1

            for tx in res.get("removed", []):
                pid = tx["transaction_id"]
                if pid in bank_by_plaid:
                    bank_data["transactions"] = [t for t in bank_data["transactions"] if t.get("plaid_id") != pid]
                    bank_by_plaid.pop(pid, None)
                    removed_count += 1
                if pid in cc_by_plaid:
                    cc_data["transactions"] = [t for t in cc_data["transactions"] if t.get("plaid_id") != pid]
                    cc_by_plaid.pop(pid, None)
                    removed_count += 1

            cursor = res.get("next_cursor", cursor)
            if not res.get("has_more"):
                break

        item["cursor"] = cursor
        item["last_synced_at"] = _utcnow_iso()
        results.append({
            "item_id":          item["item_id"],
            "institution_name": item.get("institution_name"),
            "added":            added_count,
            "modified":         modified_count,
            "removed":          removed_count,
        })

    # Plaid may re-add rows the user also has from a CSV import.
    # Drop Plaid copies whenever a CSV row already covers the same tx.
    cross_removed = _dedup_bank_cross_source(bank_data)

    _save_bank(bank_data)
    _save_apple_card(cc_data)
    _save_plaid_items(items_data)
    return jsonify({"results": results, "plaid_superseded": cross_removed})


@app.route("/api/plaid/items/<item_id>", methods=["DELETE"])
def plaid_delete_item(item_id):
    cfg = _plaid_config()
    data = _load_plaid_items()
    item = next((i for i in data["items"] if i["item_id"] == item_id), None)
    if not item:
        return jsonify({"error": "Not found"}), 404

    # Best-effort: tell Plaid to revoke
    if cfg:
        try:
            _plaid_post(cfg, "/item/remove", {"access_token": item["access_token"]})
        except RuntimeError:
            pass

    # Drop transactions tagged with this item's import_id and the import records
    bank_data = _load_bank()
    cc_data   = _load_apple_card()
    n_bank = len(bank_data["transactions"])
    n_cc   = len(cc_data["transactions"])
    bank_data["transactions"] = [t for t in bank_data["transactions"] if t.get("import_id") != item_id]
    bank_data["imports"]      = [i for i in bank_data["imports"]      if i["id"]           != item_id]
    cc_data["transactions"]   = [t for t in cc_data["transactions"]   if t.get("import_id") != item_id]
    cc_data["imports"]        = [i for i in cc_data["imports"]        if i["id"]           != item_id]
    _save_bank(bank_data)
    _save_apple_card(cc_data)

    data["items"] = [i for i in data["items"] if i["item_id"] != item_id]
    _save_plaid_items(data)

    return jsonify({
        "ok": True,
        "removed_bank_transactions": n_bank - len(bank_data["transactions"]),
        "removed_cc_transactions":   n_cc   - len(cc_data["transactions"]),
    })


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
