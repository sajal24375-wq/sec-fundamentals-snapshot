"""SEC-filed fundamentals snapshot for one US ticker.

Pulls data.sec.gov companyfacts, resolves each metric through an ordered XBRL
concept chain, keeps only true quarterly (80-100 day) or annual (330-400 day)
spans, collapses restatements to the latest-filed value per period, computes
year-over-year change against the period ending ~1 year earlier, and cites the
exact accession + EDGAR folder for every number.

Usage:
  python snapshot.py AAPL quarterly 4 revenue,net_income,eps_diluted [--out out.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract import INPUT_SCHEMA, OUTPUT_SCHEMA, validate  # noqa: E402
from sec import (FACTS_URL, METRICS, Fetcher, SecError, _dedupe_restatements,  # noqa: E402
                 _period_kind, load_ticker_map, pad_cik, resolve)

# SEC fair-access policy: declare a contact email. data.sec.gov answers 403 to
# User-Agents that contain a URL or no email, so set SEC_CONTACT=<your email>.
UA = f"secsnap/1.0 {os.environ.get('SEC_CONTACT', '').strip()}".strip()


def _date(s: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _filing_url(cik: str, accn: str | None) -> str:
    if not accn:
        return ""
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"


# Only periodic reports. Proxy statements (DEF 14A pay-versus-performance
# tables) also tag NetIncomeLoss, rounded, and are often the latest filing.
PERIODIC = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A",
            "20-F", "20-F/A", "40-F", "40-F/A"}
# Per-share figures cannot be derived by subtraction (share counts move).
NO_DERIVE = {"eps_basic", "eps_diluted"}


def _derive_q4(cand: list[dict], quarters: list[dict]) -> list[dict]:
    """Fiscal Q4 is rarely filed on its own: it sits inside the 10-K annual
    figure. When an annual span holds exactly three filed quarters and the last
    one is missing, Q4 = annual - (Q1+Q2+Q3), cited to the 10-K."""
    annual = _dedupe_restatements([f for f in cand if _period_kind(f) == "annual"])
    have = {f.get("end") for f in quarters}
    out = []
    for a in annual:
        s, e = _date(a.get("start")), _date(a.get("end"))
        if not s or not e or a.get("end") in have:
            continue
        inside = [q for q in quarters
                  if _date(q.get("start")) and _date(q.get("end"))
                  and (_date(q["start"]) - s).days >= -3 and (e - _date(q["end"])).days >= 0]
        inside.sort(key=lambda q: q["end"])
        if len(inside) != 3 or abs((_date(inside[0]["start"]) - s).days) > 3:
            continue
        # Contiguous Q1-Q3 and a plausible 80-100 day remainder.
        if any(abs((_date(inside[i + 1]["start"]) - _date(inside[i]["end"])).days - 1) > 3 for i in range(2)):
            continue
        q4s = _date(inside[-1]["end"]) + dt.timedelta(days=1)
        if not 80 <= (e - q4s).days <= 100:
            continue
        vals = [a.get("val")] + [q.get("val") for q in inside]
        if not all(isinstance(v, (int, float)) for v in vals):
            continue
        out.append(dict(a, start=q4s.isoformat(), val=vals[0] - sum(vals[1:]), form="10-K derived"))
    return out


def _series(blob: dict, metric: str, period: str) -> tuple[list[dict], str | None, dict]:
    """Deduplicated oldest-first series for one metric, plus counters."""
    spec = METRICS[metric]
    facts = blob.get("facts") or {}
    instant = spec["kind"] == "instant"
    stats = {"raw": 0, "excluded_spans": 0, "kept": 0, "derived_q4": 0}
    for taxonomy, concept in spec["concepts"]:
        units = ((facts.get(taxonomy) or {}).get(concept) or {}).get("units") or {}
        if not units:
            continue
        unit = next((u for u in ("USD", "USD/shares", "shares") if u in units), next(iter(units)))
        cand = [f for f in units[unit] if str(f.get("form", "")) in PERIODIC]
        if instant:
            picked = [f for f in cand if not f.get("start") and f.get("end")]
            if period == "annual":
                # Year-end balances are the instants a 10-K reports. Later 10-Qs
                # repeat them as comparatives, so filter on end date, not form.
                ends = {f["end"] for f in picked if str(f.get("form", "")).startswith("10-K")}
                picked = [f for f in picked if f["end"] in ends]
        else:
            picked = [f for f in cand if _period_kind(f) == period]
            stats["excluded_spans"] += sum(1 for f in cand if f.get("start") and _period_kind(f) is None)
        if not picked:
            continue
        stats["raw"] += len(picked)
        rows = _dedupe_restatements(picked)
        stats["kept"] += len(rows)
        if period == "quarterly" and not instant and metric not in NO_DERIVE:
            q4 = _derive_q4(cand, rows)
            stats["derived_q4"] += len(q4)
            rows = sorted(rows + q4, key=lambda f: str(f.get("end", "")))
        return [dict(f, _unit=unit) for f in rows], f"{taxonomy}:{concept}", stats
    return [], None, stats


def _yoy(rows: list[dict], i: int) -> str:
    cur = rows[i]
    end = _date(cur.get("end"))
    if end is None:
        return "n/a"
    for prev in reversed(rows[:i]):
        pe = _date(prev.get("end"))
        if pe is None:
            continue
        gap = (end - pe).days
        if 345 <= gap <= 385:
            pv, cv = prev.get("val"), cur.get("val")
            if isinstance(pv, (int, float)) and isinstance(cv, (int, float)) and pv != 0:
                return f"{(cv - pv) / abs(pv) * 100:+.2f}"
            return "n/a"
        if gap > 385:
            break
    return "n/a"


def snapshot(ticker: str, period: str, periods: int, metrics: list[str],
             fetcher: Fetcher | None = None) -> dict:
    req = {"ticker": ticker, "period": period, "periods": periods, "metrics": metrics}
    bad = validate(req, INPUT_SCHEMA)
    if bad:
        raise ValueError("input rejected: " + "; ".join(bad))
    fetcher = fetcher or Fetcher(user_agent=UA)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lookup = "https://www.sec.gov/files/company_tickers.json"
    try:
        cik = resolve(ticker, load_ticker_map(fetcher))
        blob = fetcher.get_json(FACTS_URL.format(cik=cik))
    except SecError as exc:
        return {"checked_at_utc": now, "company": "", "cik": "", "period": period, "rows": [],
                "unresolved_metrics": list(dict.fromkeys(metrics)),
                "data_notes": f"No data returned: {exc}. Nothing was estimated or guessed."[:2000],
                "source_links": [lookup]}
    cik = pad_cik(blob.get("cik") or cik)
    out_rows, unresolved, notes, filings = [], [], [], []
    tot = {"raw": 0, "excluded_spans": 0, "kept": 0, "derived_q4": 0}
    for m in dict.fromkeys(metrics):
        series, concept, st = _series(blob, m, period)
        for k in tot:
            tot[k] += st[k]
        if not concept:
            unresolved.append(m)
            continue
        notes.append(f"{m}={concept}")
        newest = list(range(len(series)))[::-1][:periods]
        for i in newest:
            f = series[i]
            url = _filing_url(cik, f.get("accn"))
            if url and url not in filings:
                filings.append(url)
            out_rows.append({
                "metric": m,
                "period_start": str(f.get("start") or ""),
                "period_end": str(f.get("end") or ""),
                "value": f.get("val"),
                "unit": str(f["_unit"])[:20],
                "yoy_change_pct": _yoy(series, i),
                "concept": concept[:160],
                "form": str(f.get("form") or "")[:12],
                "filed": str(f.get("filed") or "")[:10],
                "accession": str(f.get("accn") or "")[:25],
                "filing_url": url,
            })
    data_notes = (
        f"{tot['raw']} matching datapoints collapsed to {tot['kept']} distinct periods "
        f"(latest-filed value kept per period, so restated figures win); "
        f"{tot['excluded_spans']} year-to-date/half-year spans excluded by actual start-end length. "
        "Only periodic reports (10-K/10-Q/20-F/40-F and amendments) are used; proxy-statement tags are ignored. "
        + (f"{tot['derived_q4']} fiscal Q4 values are not filed separately and were derived as "
           "10-K annual minus the three filed quarters (form '10-K derived', cited to that 10-K; never for EPS). "
           if tot["derived_q4"] else "") +
        f"Concepts used: {'; '.join(notes) or 'none'}. "
        "companyfacts fy/fp fields describe the filing, not the fact, so periods are identified by dates. "
        "yoy_change_pct compares with the same metric's period ending 345-385 days earlier "
        "(n/a when absent or zero). Values are as filed in the stated unit: no FX, inflation adjustment or ratios."
        + (f" Not reported under known concepts: {', '.join(unresolved)}." if unresolved else "")
    )
    links = [FACTS_URL.format(cik=cik),
             f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-&dateb=&owner=include&count=40"]
    links += filings[:10]
    result = {"checked_at_utc": now, "company": str(blob.get("entityName") or "")[:200], "cik": cik,
              "period": period, "rows": out_rows, "unresolved_metrics": unresolved,
              "data_notes": data_notes[:2000], "source_links": links}
    bad = validate(result, OUTPUT_SCHEMA)
    if bad:
        raise RuntimeError("output failed contract: " + "; ".join(bad[:10]))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("period", choices=["quarterly", "annual"])
    ap.add_argument("periods", type=int)
    ap.add_argument("metrics", help="comma-separated")
    ap.add_argument("--out")
    a = ap.parse_args()
    res = snapshot(a.ticker, a.period, a.periods, [m.strip() for m in a.metrics.split(",") if m.strip()])
    txt = json.dumps(res, indent=1)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8")
        print(f"wrote {a.out}: {res['company']} {len(res['rows'])} rows, unresolved={res['unresolved_metrics']}")
    else:
        print(txt)


if __name__ == "__main__":
    main()
