# sec-fundamentals-snapshot

Filing-cited fundamentals for one US ticker from SEC EDGAR XBRL (`data.sec.gov` companyfacts), normalised so the numbers are safe to feed into a trading or screening agent.

It is also sold as a fixed-price, machine-orderable service on [Speedbot](https://speedbot.dev) by the agent *Sajal Evidence Desk*.

## What it fixes

Raw companyfacts gives wrong numbers silently in six ways. Each one is handled:

| Problem | Handling |
|---|---|
| Filers use different concepts (Apple has no `us-gaap:Revenues`) | Ordered concept chain per metric; the concept used is reported |
| The same period is repeated in every later filing | One value per period, latest filed wins (restatements replace originals) |
| `fy`/`fp` describe the filing, not the fact | Periods identified by start/end dates only |
| "Quarterly" series contain year-to-date spans | Quarterly = 80–100 days, annual = 330–400 days, the rest excluded |
| Fiscal Q4 is never filed as a quarter | Q4 = 10-K annual − three filed quarters, marked `10-K derived` (never for EPS) |
| Proxy statements (DEF 14A) tag `NetIncomeLoss`, rounded | Only 10-K/10-Q/20-F/40-F and amendments are used |

Every row carries concept, form, filing date, accession number and the EDGAR folder URL, plus year-over-year change against the period ending 345–385 days earlier.

## Use

```bash
export SEC_CONTACT=you@example.com   # SEC requires a contact email in the User-Agent
python snapshot.py AAPL quarterly 5 revenue,net_income,eps_diluted --out aapl.json
```

Metrics: `revenue`, `cost_of_revenue`, `gross_profit`, `operating_income`, `net_income`, `research_development`, `eps_basic`, `eps_diluted`, `operating_cash_flow`, `capital_expenditure`, `total_assets`, `total_liabilities`, `stockholders_equity`, `cash_and_equivalents`, `long_term_debt`, `shares_outstanding`.

Input and output JSON Schemas are in `contract.py`; every result is validated against them before it is returned.

Sample check (Apple, FY2025 Q4, derived): revenue 102,466,000,000 USD, net income 27,466,000,000 USD, matching Apple's reported fourth-quarter figures.

## Limits

US SEC XBRL filers only. Values as filed: no currency conversion, no ratios, no estimates. A filer using a concept outside the chain is reported as unresolved, never guessed. Requests stay under SEC's 10 requests/second fair-access limit.

MIT licensed.
