# Portfolio Tracker

A lightweight web application for tracking personal net worth across crypto, brokerage accounts, exchanges, loans, and more. Import transaction data from Schwab and Kraken to calculate realized/unrealized P&L using FIFO (First-In-First-Out) accounting.

## Tabs

| Tab | Purpose |
|-----|---------|
| Overview | Net worth total, asset breakdown bar chart, stat chips |
| Crypto | Manual crypto holdings with live prices (cryptoprices.cc, yfinance fallback) |
| Accounts | Manual accounts (401k, IRA, savings, etc.) + Schwab live summary panel |
| Loans | Liabilities subtracted from net worth |
| Kraken | Kraken spot ledger import, FIFO P&L, per-asset performance |
| History | Schwab CSV imports, FIFO P&L, per-symbol performance, transaction list |
| Schwab | Live Schwab brokerage positions via OAuth2 API |
| Charts | Allocation pie charts across all portfolios and by account |

## Features

- **Crypto Tracking**: Manage crypto holdings with real-time prices via `cryptoprices.cc` and yfinance fallback.
- **Manual Accounts**: Track cash, 401k, IRA, savings, and other account balances.
- **Loan Tracking**: Record liabilities that are subtracted from net worth.
- **Schwab CSV Import**: Import Schwab transaction history for detailed trade performance analysis.
- **Schwab Live API**: Connect via OAuth2 for real-time positions, quotes (including options), and transactions.
- **Kraken Import**: Import Kraken spot ledger CSV for crypto exchange P&L tracking.
- **Performance Analytics**: FIFO-based realized/unrealized P&L for both Schwab and Kraken.
- **Charts**: Colorblind-friendly allocation pie charts with callout labels, rendered by Chart.js 4.
- **Privacy Mode**: Toggle (or press `H`) to hide dollar totals and quantities while keeping prices and percentages visible.
- **Data Persistence**: Local JSON files — no database required.

## Installation

1. **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd portfolio-tracker
    ```

2. **Set up a virtual environment**:
    ```bash
    python -m venv .venv
    source .venv/bin/activate   # On macOS/Linux
    # .\.venv\Scripts\activate   # On Windows
    ```

3. **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4. **Run the application**:
    ```bash
    python app.py
    ```
    The app will be available at `https://127.0.0.1:8080` (self-signed SSL certificate generated on first run).

## Usage

### Crypto Holdings
- Add, update, or delete cryptocurrency holdings on the Crypto tab.
- Prices are fetched automatically from `cryptoprices.cc` with yfinance fallback.

### Importing Schwab Trades
1. Export your transaction history from Schwab as a CSV (format: `Brokerage_XXXNNN_Transactions_YYYYMMDD-HHmmss.csv`).
2. Upload the CSV on the History tab.
3. Transactions are deduplicated by MD5 hash of (date, action, symbol, qty, price, amount).
4. FIFO P&L is recalculated automatically per symbol and globally.

### Importing Kraken Ledger
1. Export your spot ledger from Kraken (CSV).
2. Upload on the Kraken tab.
3. `earn` rows and non-crypto subclass rows are skipped automatically.
4. Withdrawals reduce open lots via FIFO without creating P&L events.

### Connecting Schwab Live API
1. On the Schwab tab, click **Connect** to get an OAuth2 authorization URL.
2. Copy the URL, paste it into your browser, and authorize the app.
3. The app will receive the callback and store tokens in `data/schwab_token.json`.
4. Refresh tokens expire after 7 days — you'll need to reconnect after that.
5. Positions and quotes do NOT auto-load — click **Refresh** on the Schwab tab.

### Privacy Mode
- Click **Hide** in the header, or press `H`, to toggle privacy mode.
- **Hidden**: dollar totals, quantities (that reveal net worth).
- **Always shown**: prices, average cost basis, percentages.
- Resets to OFF on every page load.

## Data Structure

All data is stored in the `data/` directory:

| File | Contents |
|------|----------|
| `portfolio.json` | Crypto holdings: `{ holdings: [...] }` |
| `accounts.json` | Manual accounts: `{ accounts: [...] }` |
| `loans.json` | Loans/liabilities |
| `trades.json` | Schwab transactions + import manifest: `{ transactions: [...], imports: [...] }` |
| `kraken.json` | Kraken ledger transactions + import manifest |
| `schwab_token.json` | Schwab OAuth2 tokens (gitignored) |
| `imports/` | Raw copies of imported CSV files |

### Schwab CSV Format
Columns: `Date`, `Action`, `Symbol`, `Description`, `Quantity`, `Price`, `Fees & Comm`, `Amount`
- Dates may include an "as of" suffix — parsed correctly.
- Options appear as e.g. `SATL 01/15/2027 10.00 C`.
- `Amount` field already includes fees (no double-counting).

### Kraken CSV Format
Spot ledger export with columns: `txid`, `refid`, `time`, `type`, `subtype`, `aclass`, `subclass`, `asset`, `wallet`, `amount`, `fee`, `balance`, `amountusd`

## Technical Details

- **Backend**: Flask (Python 3.13), port 8080 with self-signed SSL
- **Frontend**: Vanilla JS, Tailwind CSS (CDN), Chart.js 4
- **Accounting**: FIFO (First-In-First-Out) lot matching
- **Price Sources**: `cryptoprices.cc` primary, yfinance fallback
- **No database**: Portable JSON file storage
