# Portfolio Tracker — Dev Notes

## Stack
- **Backend**: Flask (`app.py`), Python 3.13, runs on port 8080 with self-signed SSL (required for Schwab OAuth)
- **Frontend**: Two separate apps:
  - `templates/index.html` — portfolio tracker (vanilla JS, Tailwind CDN, Chart.js 4)
  - `templates/apple_card.html` — transactions tracker (same stack), served at `/transactions`
- **Data**: JSON files in `data/` — no database

## Data layout
```
data/
├── portfolio.json   — crypto holdings: { holdings: [...] }
├── accounts.json    — manual accounts: { accounts: [...] }
├── trades.json      — Schwab CSV transactions + import manifest: { transactions: [...], imports: [...] }
├── kraken.json      — Kraken ledger transactions + imports
├── apple_card.json  — CC transactions + import manifest: { transactions: [...], imports: [...] }
├── bank.json        — bank transactions + import manifest: { transactions: [...], imports: [...] }
└── imports/         — raw CSV copies
    └── kraken/      — Kraken CSV copies
```

## Tabs (index.html)
| Tab | Purpose |
|-----|---------|
| Overview | Net worth total, asset breakdown bar chart, stat chips |
| Crypto | Sub-tabs: Holdings (manual crypto + live prices) \| Kraken (spot ledger import, FIFO P&L) |
| Accounts | Manual accounts (401k, IRA, savings, etc.) + Schwab live summary panel |
| Loans | Liabilities subtracted from net worth |
| Grants | RSU/option grants tracker |
| Schwab | Sub-tabs: History (CSV imports, FIFO P&L, per-symbol) \| Live (positions via OAuth) \| Charts (allocation pies) |

### Sub-tab pattern
Used by Crypto and Schwab. `setCryptoSubTab(sub)` / `setSchwabSubTab(sub)` show/hide sub-panes, swap action buttons, and trigger data loads. Legacy `setTab('kraken')` / `setTab('history')` / `setTab('charts')` redirect to the correct consolidated tab+sub-tab. `setTab()` array only includes top-level tabs: `['overview','crypto','accounts','loans','grants','schwab']`.

## Schwab Integration (LIVE — fully implemented)
- OAuth2 against `https://api.schwabapi.com/v1/` with local HTTPS callback on `https://127.0.0.1:8080/callback`
- Token stored in `data/schwab_token.json` (gitignored); auto-refreshes access token
- Refresh token valid 7 days — user must reconnect after that
- Routes: `/api/schwab/auth-url`, `/callback`, `/api/schwab/status`, `/api/schwab/positions`, `/api/schwab/quotes`, `/api/schwab/fetch-transactions`, `/api/schwab/disconnect`
- Positions do NOT auto-load on page startup — user clicks Refresh

### Account label matching
Schwab API returns `account_number` as last-4 digits. Imported CSV filenames use `XXXnnn` masking — last 3 digits visible. Labels are resolved by matching `account_number.slice(-3)` against the digit suffix after `XXX` in import filenames. Import `account_name` becomes the display label (e.g. "HSA ···1234").

### Option symbols
Human-readable: `XYZ 01/15/2027 10.00 C`
OCC format for quotes API: `XYZ   270115C00010000` — converted by `toOCCSymbol()` in frontend, response remapped back to original symbol.

## CSV formats supported

**Schwab**: `Brokerage_XXXNNN_Transactions_YYYYMMDD-HHmmss.csv`
- Columns: Date, Action, Symbol, Description, Quantity, Price, Fees & Comm, Amount
- Deduplicates by MD5 hash of (date|action|symbol|qty|price|amount)

**Kraken**: spot ledger CSV export
- Columns: txid, refid, time, type, subtype, aclass, subclass, asset, wallet, amount, fee, balance, amountusd
- `earn` type rows skipped; non-crypto subclass rows skipped

**Credit card** — auto-detected by `_detect_cc_format(headers)`:
| Format | Detection | Amount sign |
|--------|-----------|-------------|
| Apple Card | has `Amount (USD)` column | positive = purchase |
| Fidelity CC | has `Name` + `Date` columns | negative = purchase (negated on import) |
| Chase / generic | has `Transaction Date` + `Post Date` columns | negative = sale (negated on import) |

**Bank**: flexible column parser (`_pick()` tries multiple column name variants). CC payment rows filtered by `_CC_PAYMENT_PATTERNS` regex at import time.

## P&L calculation
Both Schwab and Kraken use FIFO lot matching (`_calc_performance` / `_calc_kraken_performance`).
Schwab: `amount` field already includes fees (no double-counting). Open lots stored per symbol with `account` field.
Kraken: `amount_usd` used for cost/proceeds.

## Import records schema

All four importable data files (`trades.json`, `kraken.json`, `apple_card.json`, `bank.json`) store an `imports[]` array alongside `transactions[]`. Each import record:

```json
{
  "id": "uuid",
  "filename": "apple_card_2025.csv",
  "card_name": "Apple Card",          // CC only
  "account_name": "Checking",         // Bank only
  "source": "apple|fidelity|chase",   // CC only
  "imported_at": "ISO timestamp",
  "new_transactions": 148,
  "duplicate_transactions": 2
}
```

Each transaction is tagged with `import_id` and `card_name` / `account_name` so imports can be deleted individually (`DELETE /api/apple-card/imports/<id>`, `DELETE /api/bank/imports/<id>`).

## Transactions page (apple_card.html)

Served at `/transactions` (also `/apple-card`). Two top-level tabs: **Credit Card** | **Bank**.

### Credit Card tab
- Filters: card tabs (when 2+ cards), range, category
- Stats: total spent, avg/month, transaction count, avg transaction
- Charts: monthly bar (clickable → jump to month), category breakdown list + donut
- Cards: Recurring Payments (amount-similar, 3+ months), transaction list
- Views: By Date | By Merchant (grouped, expandable)
- Sort: date desc/asc, amount desc/asc; search bar
- Import panel: collapsible list of imported CSVs with delete-by-import
- Card tabs: shown when 2+ distinct `card_name` values exist; filters all charts/stats/list
- Merchant whitelist does NOT apply to CC — categories come from CSV

### Bank tab
- Filters: account tabs (filter pills), range, type (Deposits / Withdrawals), category, hide interest toggle
- Stats: Deposits total (clickable filter), Withdrawals total (clickable filter), Net, Count
- Charts: monthly stacked bar (credits + debits), spending-by-category breakdown list + donut
- Cards: Recurring Payments, Frequent Payees chips
- Frequent Payees: any description appearing 3+ times; grouped by stripped name; dot + amount color = green/red by net direction; click to filter transactions
- `stripBkPrefix()` removes "Withdrawal from", "Deposit from", "Transfer from/to", etc. before grouping
- `bkLogUnmatched()` available in browser console to debug unmatched descriptions
- Import panel: collapsible list with delete-by-import; name modal prompts for account name on import

### Merchant category whitelist (bank only)
`MERCHANT_CATS` in `apple_card.html` — matched case-insensitively as substring (`String.includes()`). No regex, no wildcards — `*` and `+` are literal. Order matters: first match wins.

Categories and colors:
| Category | Color | Key keywords |
|----------|-------|-------------|
| Amazon | `#f97316` | amazon, amzn, whole foods |
| Apple | `#6ee7f7` | apple, itunes |
| Transfers | `#2dd4bf` | venmo, zelle, cashapp, paypal |
| Rent | `#fb7185` | greystar, rent payment, property management |
| Insurance | `#94a3b8` | geico, state farm, allstate, progressive, aetna |
| Subscriptions | `#e879f9` | netflix, spotify, hulu, disney, adobe |
| Interest | `#475569` | interest payment, interest earned, dividend |
| Bills & Utilities | `#7a8fa8` | pg&e, verizon, vzw, att, comcast, city of austin |
| Groceries | `#86efac` | safeway, kroger, walmart, target, costco |
| Gas | `#fb923c` | shell, chevron, exxon, wawa |
| Hotels | `#c084fc` | marriott, hilton, hyatt, wyndham, … |
| Car Rental | `#38bdf8` | hertz, avis, enterprise, zipcar |

## Privacy mode
- Default: OFF (values visible) on every page load — not persisted
- Toggle: Hide button in header, keyboard shortcut `H`
- **Hidden when on**: dollar totals, quantities (reveal NW)
- **Always shown**: prices (avg cost, unit cost), percentages (%, return %, day %), cost basis per lot
- Implementation: `fmtUSD()` / `fmtAmt()` hide by default; `fmtPrice(p, true)` and `fmtUSDCtx()` always show

## Frontend state (S object key fields — index.html)
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

## Schwab TODO (postponed)
- OAuth currently requires user to copy auth URL and paste manually (no redirect server running separately)
- Rate limits: 120 req/min market data, unlimited account data
- Reference: https://developer.schwab.com
