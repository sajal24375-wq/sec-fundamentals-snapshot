"""
SEC EDGAR XBRL -> clean financial time series.

Why this module exists
----------------------
data.sec.gov gives away every US public company's financials for free, so the
value of a paid tool here is not access. It is that the raw shape is genuinely
hostile, in four specific ways that each silently produce wrong numbers:

1. CONCEPT NAMES ARE NOT CONSISTENT ACROSS FILERS.
   Apple does not report `us-gaap:Revenues`. It reports
   `RevenueFromContractWithCustomerExcludingAssessedTax`. Asking for the obvious
   concept returns HTTP 404 and a naive script records "no data" for the largest
   company in the world. Every metric therefore needs an ordered fallback chain.

2. RESTATEMENTS ARE NOT DEDUPLICATED.
   The same fiscal period is reported many times across subsequent filings as
   figures are revised. companyfacts returns all of them, flat. Summing or
   taking the first match double-counts. You must group by period and keep the
   most recently filed value.

3. PERIOD LENGTH IS NOT LABELLED RELIABLY.
   `fp` and `form` disagree often enough to matter, and a "quarterly" series
   silently contains year-to-date and annual durations. The only trustworthy
   signal is the actual span between `start` and `end`.

4. INSTANT AND DURATION FACTS ARE MIXED.
   Balance-sheet items (Assets) are point-in-time and have no `start`. Income
   items (Revenues) are spans. Treating them alike produces nonsense.

Everything below exists to handle one of those four. The module is pure: no
Apify imports, no network globals, so it can be tested directly.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

# SEC's fair-access policy asks for a User-Agent that identifies the requester
# with contact info, and caps traffic at 10 requests/second. Sending a browser
# user-agent here would be both dishonest and against the published guidance.
DEFAULT_UA = "ApifyActor-SECFinancials/0.1 (+https://apify.com; contact via Apify)"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Ordered fallback chains. First concept that yields data for a filer wins.
# These orderings are deliberate: the newer ASC 606 revenue concepts come before
# the legacy ones, because filers that report both use the newer as primary.
METRICS: dict[str, dict[str, Any]] = {
    "revenue": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
            ("us-gaap", "Revenues"),
            ("us-gaap", "SalesRevenueNet"),
            ("us-gaap", "SalesRevenueGoodsNet"),
            ("us-gaap", "RevenuesNetOfInterestExpense"),
        ],
    },
    "cost_of_revenue": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "CostOfRevenue"),
            ("us-gaap", "CostOfGoodsAndServicesSold"),
            ("us-gaap", "CostOfGoodsSold"),
        ],
    },
    "gross_profit": {
        "kind": "duration",
        "concepts": [("us-gaap", "GrossProfit")],
    },
    "operating_income": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "OperatingIncomeLoss"),
            ("us-gaap", "IncomeLossFromContinuingOperations"),
        ],
    },
    "net_income": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "NetIncomeLoss"),
            ("us-gaap", "ProfitLoss"),
            ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        ],
    },
    "research_development": {
        "kind": "duration",
        "concepts": [("us-gaap", "ResearchAndDevelopmentExpense")],
    },
    "operating_cash_flow": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ],
    },
    "capital_expenditure": {
        "kind": "duration",
        "concepts": [
            ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
            ("us-gaap",
             "PaymentsToAcquireProductiveAssets"),
        ],
    },
    "eps_basic": {
        "kind": "duration",
        "concepts": [("us-gaap", "EarningsPerShareBasic")],
    },
    "eps_diluted": {
        "kind": "duration",
        "concepts": [("us-gaap", "EarningsPerShareDiluted")],
    },
    "total_assets": {
        "kind": "instant",
        "concepts": [("us-gaap", "Assets")],
    },
    "total_liabilities": {
        "kind": "instant",
        "concepts": [("us-gaap", "Liabilities")],
    },
    "stockholders_equity": {
        "kind": "instant",
        "concepts": [
            ("us-gaap", "StockholdersEquity"),
            ("us-gaap",
             "StockholdersEquityIncludingPortionAttributableToNoncontrolling"
             "Interest"),
        ],
    },
    "cash_and_equivalents": {
        "kind": "instant",
        "concepts": [
            ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
            ("us-gaap",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ],
    },
    "long_term_debt": {
        "kind": "instant",
        "concepts": [
            ("us-gaap", "LongTermDebtNoncurrent"),
            ("us-gaap", "LongTermDebt"),
        ],
    },
    "shares_outstanding": {
        "kind": "instant",
        "concepts": [
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
        ],
    },
}

ALL_METRICS = tuple(METRICS)


# ------------------------------------------------------------------ fetching

class SecError(RuntimeError):
    """A fetch failed in a way the caller should report, not retry forever."""


@dataclass
class Fetcher:
    """HTTP with SEC-appropriate manners: declared UA, rate cap, retries.

    The retry loop is not optional politeness. data.sec.gov intermittently drops
    connections, and a single un-retried timeout is the difference between a
    clean dataset and a silently missing company.
    """

    user_agent: str = DEFAULT_UA
    min_interval: float = 0.15          # ~6.7 req/s, under SEC's 10/s ceiling
    retries: int = 4
    timeout: int = 30
    _last: float = field(default=0.0, repr=False)

    def get_json(self, url: str) -> Any:
        last_err = ""
        for attempt in range(self.retries):
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()

            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                # Identity encoding on purpose: urllib does not transparently
                # decompress, and feeding gzip bytes to json.loads fails in a
                # confusing way.
                "Accept-Encoding": "identity",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise SecError(f"404 not found: {url}") from None
                last_err = f"HTTP {exc.code}"
                if exc.code in (403, 429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SecError(f"{last_err}: {url}") from None
            except Exception as exc:
                last_err = f"{type(exc).__name__}"
                time.sleep(1.5 * (attempt + 1))
        raise SecError(f"{last_err} after {self.retries} attempts: {url}")


def pad_cik(cik: str | int) -> str:
    """CIKs must be zero-padded to 10 digits or data.sec.gov 404s.

    This is the single most common cause of 'the API has no data for my company'
    in hand-written scripts.
    """
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise SecError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def load_ticker_map(fetcher: Fetcher) -> dict[str, str]:
    """ticker (upper) -> zero-padded CIK."""
    raw = fetcher.get_json(TICKERS_URL)
    out: dict[str, str] = {}
    # The file is a dict of index -> {cik_str, ticker, title}
    rows: Iterable[dict] = raw.values() if isinstance(raw, dict) else raw
    for row in rows:
        t = str(row.get("ticker", "")).strip().upper()
        if t:
            out[t] = pad_cik(row.get("cik_str", ""))
    return out


def resolve(identifier: str, ticker_map: dict[str, str]) -> str:
    """Accept a ticker or a CIK in any format and return a padded CIK."""
    s = str(identifier).strip()
    if not s:
        raise SecError("empty identifier")
    upper = s.upper()
    if upper in ticker_map:
        return ticker_map[upper]
    if upper.startswith("CIK"):
        return pad_cik(upper[3:])
    if any(ch.isdigit() for ch in s) and not any(ch.isalpha() for ch in s):
        return pad_cik(s)
    raise SecError(f"unknown ticker and not a CIK: {identifier!r}")


# --------------------------------------------------------------- normalising

def _days(start: str, end: str) -> int | None:
    try:
        s = time.strptime(start, "%Y-%m-%d")
        e = time.strptime(end, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return round((time.mktime(e) - time.mktime(s)) / 86400)


def _period_kind(fact: dict) -> str | None:
    """'annual', 'quarterly', or None, judged on actual elapsed days.

    Deliberately ignores `fp` and `form`, which disagree with reality often
    enough that trusting them puts year-to-date figures into a quarterly series.
    """
    start, end = fact.get("start"), fact.get("end")
    if not start:
        return "instant" if end else None
    n = _days(start, end)
    if n is None:
        return None
    if 330 <= n <= 400:
        return "annual"
    if 80 <= n <= 100:
        return "quarterly"
    return None          # year-to-date, half-year, or some other span


def _dedupe_restatements(facts: list[dict]) -> list[dict]:
    """One row per period, keeping the most recently filed value.

    companyfacts lists every filing of every fact. The same fiscal quarter shows
    up repeatedly as later filings revise it. Keyed on the period itself, so a
    revision replaces rather than duplicates.
    """
    best: dict[tuple, dict] = {}
    for f in facts:
        key = (f.get("start"), f.get("end"))
        prev = best.get(key)
        if prev is None or str(f.get("filed", "")) > str(prev.get("filed", "")):
            best[key] = f
    return sorted(best.values(), key=lambda f: str(f.get("end", "")))


def extract_metric(facts_blob: dict, metric: str, period: str
                   ) -> tuple[list[dict], str | None]:
    """Return (rows, concept_used). Walks the fallback chain until data appears."""
    spec = METRICS[metric]
    taxonomies = facts_blob.get("facts") or {}
    want_instant = spec["kind"] == "instant"

    for taxonomy, concept in spec["concepts"]:
        cdata = (taxonomies.get(taxonomy) or {}).get(concept)
        if not cdata:
            continue
        units: dict[str, list[dict]] = cdata.get("units") or {}
        if not units:
            continue
        # Prefer USD, then per-share, then whatever single unit exists.
        unit = next((u for u in ("USD", "USD/shares", "shares") if u in units),
                    next(iter(units)))
        candidates = units[unit]

        if want_instant:
            picked = [f for f in candidates if not f.get("start")]
        else:
            picked = [f for f in candidates
                      if _period_kind(f) == period]
        if not picked:
            continue

        rows = []
        for f in _dedupe_restatements(picked):
            rows.append({
                "metric": metric,
                "concept": f"{taxonomy}:{concept}",
                "unit": unit,
                "period_start": f.get("start"),
                "period_end": f.get("end"),
                "fiscal_year": f.get("fy"),
                "fiscal_period": f.get("fp"),
                "form": f.get("form"),
                "filed": f.get("filed"),
                "accession": f.get("accn"),
                "frame": f.get("frame"),
                "value": f.get("val"),
            })
        if rows:
            return rows, f"{taxonomy}:{concept}"
    return [], None


def company_rows(facts_blob: dict, metrics: Iterable[str], period: str,
                 limit_periods: int | None) -> tuple[list[dict], dict]:
    """Normalised rows for one company, newest period first, plus a report."""
    entity = facts_blob.get("entityName") or ""
    cik = pad_cik(facts_blob.get("cik", 0)) if facts_blob.get("cik") else ""

    out: list[dict] = []
    resolved: dict[str, str | None] = {}
    for metric in metrics:
        if metric not in METRICS:
            resolved[metric] = None
            continue
        effective = "instant" if METRICS[metric]["kind"] == "instant" else period
        rows, concept = extract_metric(facts_blob, metric, effective)
        resolved[metric] = concept
        rows.reverse()                       # newest first
        if limit_periods:
            rows = rows[:limit_periods]
        for r in rows:
            r["company"] = entity
            r["cik"] = cik
            out.append(r)

    found = sum(1 for v in resolved.values() if v)
    report = {
        "company": entity,
        "cik": cik,
        "metrics_requested": len(resolved),
        "metrics_resolved": found,
        "concepts_used": resolved,
        "rows": len(out),
    }
    return out, report
