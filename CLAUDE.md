# Portfolio Tracker — Dev Notes

## Stack
- **Backend**: Flask (`app.py`), Python 3.13, runs on port 8080 with self-signed SSL (required for Schwab OAuth)
- **Frontend**: Single-page app in `templates/index.html` — vanilla JS, Tailwind CDN, Chart.js 4
- **Data**: JSON files in `data/` — no database

## Data layout
```
data/
├── portfolio.json   — crypto holdings: { holdings: [...] }
├── accounts.json    — manual accounts: { accounts: [...] }
├── trades.json      — Schwab CSV transactions + import manifest: { transactions: [...], imports: [...] }
├── kraken.json      — Kraken ledger transactions + imports
└── imports/         — raw CSV copies
    └── kraken/      — Kraken CSV copies
```

## Tabs
| Tab | Purpose |
|-----|---------|
| Overview | Net worth total, asset breakdown bar chart, stat chips |
| Crypto | Manual crypto holdings with live prices (cryptoprices.cc + yfinance fallback) |
| Accounts | Manual accounts (401k, IRA, savings, etc.) + Schwab live summary panel |
| Loans | Liabilities subtracted from net worth |
| Kraken | Kraken spot ledger import, FIFO P&L, per-asset performance |
| History | Schwab CSV imports, FIFO P&L, per-symbol performance, transaction list |
| Schwab | Live Schwab brokerage positions via OAuth API |
| Charts | Allocation pie charts (colorblind-friendly, callout labels) |

## Schwab Integration (LIVE — fully implemented)
- OAuth2 against `https://api.schwabapi.com/v1/` with local HTTPS callback on `https://127.0.0.1:8080/callback`
- Token stored in `data/schwab_token.json` (gitignored); auto-refreshes access token
- Refresh token valid 7 days — user must reconnect after that
- Routes: `/api/schwab/auth-url`, `/callback`, `/api/schwab/status`, `/api/schwab/positions`, `/api/schwab/quotes`, `/api/schwab/fetch-transactions`, `/api/schwab/disconnect`
- Positions do NOT auto-load on page startup — user clicks Refresh

### Account label matching
Schwab API returns `account_number` as last-4 digits (e.g. `"2787"`). Imported CSV filenames use `XXXnnn` masking (e.g. `HSA_Brokerage_XXX787_...csv`) — last 3 digits visible. Labels are resolved by matching `account_number.slice(-3)` against the digit suffix after `XXX` in import filenames. Import `account_name` becomes the display label (e.g. "HSA ···2787").

### Option symbols
Human-readable: `SATL 01/15/2027 10.00 C`
OCC format for quotes API: `SATL  270115C00010000` — converted by `toOCCSymbol()` in frontend, response remapped back to original symbol.

## CSV formats supported
**Schwab**: `Brokerage_XXXNNN_Transactions_YYYYMMDD-HHmmss.csv`
- Columns: Date, Action, Symbol, Description, Quantity, Price, Fees & Comm, Amount
- Dates may include "as of" suffix — parsed correctly
- Options appear as symbols with spaces: `SATL 01/15/2027 10.00 C`
- Deduplicates by MD5 hash of (date|action|symbol|qty|price|amount)

**Kraken**: spot ledger CSV export
- Columns: txid, refid, time, type, subtype, aclass, subclass, asset, wallet, amount, fee, balance, amountusd
- `earn` type rows skipped; non-crypto subclass rows skipped
- Withdrawals reduce open lots (FIFO) without creating P&L events

## P&L calculation
Both Schwab and Kraken use FIFO lot matching (`_calc_performance` / `_calc_kraken_performance`).
Schwab: `amount` field already includes fees (no double-counting). Open lots stored per symbol with `account` field.
Kraken: `amount_usd` used for cost/proceeds.

## Import annotation schema (trades.json → imports[])
```json
{
  "id": "uuid",
  "filename": "Brokerage_XXX524_Transactions_20260411.csv",
  "account_name": "Brokerage",
  "notes": "free-text",
  "imported_at": "ISO timestamp",
  "date_range_from": "YYYY-MM-DD",
  "date_range_to": "YYYY-MM-DD",
  "total_rows": 150,
  "new_transactions": 148,
  "duplicate_transactions": 2
}
```

## Known accounts (from imports)
| Last-3 | Account Name | Type |
|--------|-------------|------|
| 524 | Brokerage | Taxable brokerage |
| 787 | HSA | Health Savings Account |
| 555 | Roth IRA | Roth Contributory IRA |

## Privacy mode
- Default: OFF (values visible) on every page load — not persisted
- Toggle: Hide button in header, keyboard shortcut `H`
- **Hidden when on**: dollar totals, quantities (reveal NW)
- **Always shown**: prices (avg cost, unit cost), percentages (%, return %, day %), cost basis per lot
- Implementation: `fmtUSD()` / `fmtAmt()` hide by default; `fmtPrice(p, true)` and `fmtUSDCtx()` always show

## Frontend state (S object key fields)
```js
S.holdings       // crypto holdings array
S.prices         // { symbol: price } live prices
S.accounts       // manual accounts array
S.loans          // loans array
S.imports        // trades.json imports array (loaded at startup)
S.perf           // FIFO P&L result from /api/trades/performance
S.schwabPositions // live positions from Schwab API
S.schwabQuotes   // { symbol: { price, change, change_pct } } — includes options via OCC conversion
S.schwabLotsOpen // Set of symbols with lots expanded in Schwab tab
S.krakenPerf     // Kraken P&L result
```

## Charts tab
- Uses Chart.js 4 with custom `_calloutPlugin` for leader lines + inline labels
- Colorblind-friendly: Paul Tol bright palette (`CB_PALETTE`)
- Charts rendered: All Portfolios, Schwab By Account, Schwab All Positions, per-account breakdowns
- Options shown as ticker only (e.g. `SATL`) in chart labels; full symbol in hover tooltip
- Layout: 2-column CSS grid

## Schwab TODO (postponed)
- OAuth currently requires user to copy auth URL and paste manually (no redirect server running separately)
- Rate limits: 120 req/min market data, unlimited account data
- Reference: https://developer.schwab.com
