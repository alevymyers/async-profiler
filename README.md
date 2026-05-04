# Portfolio Tracker

A personal net-worth dashboard that combines live brokerage data, crypto prices, manual accounts, and loan liabilities into a single local web app. All data lives in JSON files on disk — no database, no cloud sync.

## What it tracks

| Tab | Purpose |
|-----|---------|
| Overview | Net worth total, asset breakdown bar chart, summary chips |
| Crypto | Manual crypto holdings with live prices (cryptoprices.cc + yfinance fallback) |
| Accounts | Manual accounts (401k, IRA, savings, etc.) + Schwab live summary panel |
| Loans | Liabilities subtracted from net worth, with auto-computed balance from start date |
| Kraken | Kraken spot ledger import, FIFO P&L, per-asset performance |
| History | Schwab CSV imports, FIFO P&L, per-symbol performance, transaction list |
| Schwab | Live Schwab brokerage positions via OAuth2 API |
| Charts | Colorblind-friendly allocation pie charts with callout labels |

---

## Setup

### 1. Clone and install

```bash
git clone <repository-url>
cd portfolio-tracker
python -m venv .venv
source .venv/bin/activate    # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run the app

```bash
python app.py
```

Open **https://127.0.0.1:8080** in your browser. On first run, a self-signed SSL certificate is generated in `data/` — your browser will warn you; click through. SSL is required for the Schwab OAuth callback.

---

## Schwab Integration

The Schwab tab shows live positions and quotes via the Schwab Individual Trader API.

### One-time developer setup

1. Go to [developer.schwab.com](https://developer.schwab.com) and sign in with your Schwab account.
2. Create a new app. Set the **Callback URL** to exactly: `https://127.0.0.1:8080/callback`
3. After approval, copy your **App Key** (client ID) and **Secret** (client secret).

### Configure credentials

Create `data/schwab_config.json` (this file is gitignored):

```json
{
  "client_id": "your-app-key-here",
  "client_secret": "your-secret-here"
}
```

Alternatively, set environment variables instead of the file:

```bash
export SCHWAB_CLIENT_ID=your-app-key-here
export SCHWAB_CLIENT_SECRET=your-secret-here
```

### Connect OAuth

1. Open the **Schwab** tab and click **Connect**.
2. Copy the authorization URL shown, paste it into your browser, and log in to Schwab.
3. After authorizing, Schwab redirects to `https://127.0.0.1:8080/callback` — the app captures the token automatically.
4. Tokens are saved in `data/schwab_token.json` (gitignored). The access token auto-refreshes.
5. **Refresh tokens expire after 7 days.** After that, click **Connect** again to re-authorize.

### Using live data

- Click **Refresh** on the Schwab tab to load positions and quotes (they do not auto-load on startup).
- Options are shown in human-readable format (`SATL 01/15/2027 10.00 C`) but quoted via the OCC format internally.
- The Schwab panel on the **Accounts** tab shows a live summary of all connected accounts.

---

## Importing transaction history

### Schwab CSV

1. Log in to Schwab → **Accounts** → **History** → **Export**.
2. Filename must match the pattern `Brokerage_XXXNNN_Transactions_YYYYMMDD-HHmmss.csv`.
3. Upload on the **History** tab. Transactions are deduplicated by MD5 hash — re-importing the same file is safe.
4. Multiple accounts (Brokerage, HSA, Roth IRA) can all be imported; they are labeled by the last 3 digits of the account number in the filename (e.g. `XXX787` → HSA).

Schwab CSV columns: `Date`, `Action`, `Symbol`, `Description`, `Quantity`, `Price`, `Fees & Comm`, `Amount`
- Dates may include an "as of" suffix — handled automatically.
- `Amount` already includes fees; they are not double-counted in P&L.

### Kraken ledger CSV

1. Log in to Kraken → **History** → **Export** → select **Ledgers**, export as CSV.
2. Upload on the **Kraken** tab.
3. `earn` rows and non-crypto subclass rows are skipped automatically.
4. Withdrawals reduce open lots via FIFO without creating a P&L event.

Kraken CSV columns: `txid`, `refid`, `time`, `type`, `subtype`, `aclass`, `subclass`, `asset`, `wallet`, `amount`, `fee`, `balance`, `amountusd`

---

## Loans

Loans tab tracks liabilities subtracted from net worth. Each loan supports automatic balance computation:

- **Original Amount** — the starting loan balance (stored and editable).
- **Start Date** — when you started making payments. If set, the current balance is computed as: `max(0, original − months_elapsed × monthly_payment)`.
- **Monthly Payment** — used for both the auto-computed balance and payoff estimate.
- **Interest Rate** — display only (does not affect balance calculation).

If no start date is set, the original amount is used as the balance directly.

---

## Data files

All files live in `data/` and are gitignored. The app creates them automatically on first use.

| File | Contents |
|------|----------|
| `portfolio.json` | Crypto holdings |
| `accounts.json` | Manual accounts |
| `loans.json` | Loans and liabilities |
| `trades.json` | Schwab transactions + import manifest |
| `kraken.json` | Kraken ledger transactions + import manifest |
| `imports/` | Raw copies of imported CSV files |
| `imports/kraken/` | Raw copies of Kraken CSV files |
| `schwab_config.json` | Schwab client ID + secret (you create this) |
| `schwab_token.json` | Schwab OAuth tokens (auto-managed) |
| `ssl_cert.pem` / `ssl_key.pem` | Self-signed cert (auto-generated) |

**Never commit the `data/` directory.** It contains credentials and personal financial data. The `.gitignore` excludes it entirely.

Transaction CSV exports (Schwab, Kraken) are also gitignored via `*.csv` — do not override this.

---

## Privacy mode

Press `H` or click **Hide** in the header to toggle privacy mode.

- **Hidden**: dollar totals and quantities that reveal net worth.
- **Always shown**: unit prices, average cost basis, percentages, day change %.
- Resets to OFF on every page load (not persisted).

---

## Technical details

- **Backend**: Flask (Python 3.13), HTTPS on port 8080
- **Frontend**: Vanilla JS, Tailwind CSS (CDN), Chart.js 4 — single HTML file, no build step
- **Accounting**: FIFO lot matching for both Schwab and Kraken
- **Price sources**: cryptoprices.cc (primary), yfinance (fallback)
- **Storage**: local JSON files, no database

### API routes (for Claude / automation)

| Method | Route | Purpose |
|--------|-------|---------|
| GET/POST | `/api/holdings` | Crypto holdings |
| GET/POST | `/api/accounts` | Manual accounts |
| GET/POST | `/api/loans` | Loans |
| PUT/DELETE | `/api/loans/<id>` | Edit/delete a loan |
| GET | `/api/prices` | Live crypto prices |
| POST | `/api/trades/import` | Import Schwab CSV |
| GET | `/api/trades/performance` | FIFO P&L for Schwab |
| POST | `/api/kraken/import` | Import Kraken CSV |
| GET | `/api/kraken/performance` | FIFO P&L for Kraken |
| GET | `/api/schwab/auth-url` | Start Schwab OAuth |
| GET | `/api/schwab/status` | Token status |
| GET | `/api/schwab/positions` | Live positions |
| GET | `/api/schwab/quotes` | Live quotes |
| POST | `/api/schwab/fetch-transactions` | Pull transactions from API |
| POST | `/api/schwab/disconnect` | Clear token |
