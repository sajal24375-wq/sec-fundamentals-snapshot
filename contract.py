"""Service contract (input/output JSON Schemas) and a strict validator for the
bounded schema subset Speedbot accepts. The validator is used before every
delivery so a result can never be rejected for shape.
"""
from __future__ import annotations

from typing import Any

from sec import ALL_METRICS

PERIODS = ["quarterly", "annual"]

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "minLength": 1, "maxLength": 12,
                   "description": "US-listed ticker (e.g. AAPL) or SEC CIK digits"},
        "period": {"type": "string", "enum": PERIODS, "maxLength": 10,
                   "description": "quarterly = 80-100 day spans, annual = 330-400 day spans"},
        "periods": {"type": "integer", "minimum": 1, "maximum": 12,
                    "description": "Number of most recent periods per metric"},
        # 8 metrics x 12 periods = 96 rows, inside the 100-row output cap.
        "metrics": {"type": "array", "minItems": 1, "maxItems": 8,
                    "items": {"type": "string", "enum": list(ALL_METRICS), "maxLength": 40},
                    "description": "Metrics to extract"},
    },
    "required": ["ticker", "period", "periods", "metrics"],
    "additionalProperties": False,
}

ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "maxLength": 40},
        "period_start": {"type": "string", "maxLength": 10},
        "period_end": {"type": "string", "maxLength": 10},
        "value": {"type": "number"},
        "unit": {"type": "string", "maxLength": 20},
        "yoy_change_pct": {"type": "string", "maxLength": 16},
        "concept": {"type": "string", "maxLength": 160},
        "form": {"type": "string", "maxLength": 12},
        "filed": {"type": "string", "maxLength": 10},
        "accession": {"type": "string", "maxLength": 25},
        "filing_url": {"type": "string", "maxLength": 300},
    },
    "required": ["metric", "period_start", "period_end", "value", "unit", "yoy_change_pct",
                 "concept", "form", "filed", "accession", "filing_url"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "checked_at_utc": {"type": "string", "maxLength": 40},
        "company": {"type": "string", "maxLength": 200},
        "cik": {"type": "string", "maxLength": 10},
        "period": {"type": "string", "enum": PERIODS, "maxLength": 10},
        "rows": {"type": "array", "maxItems": 100, "items": ROW_SCHEMA},
        "unresolved_metrics": {"type": "array", "maxItems": len(ALL_METRICS),
                               "items": {"type": "string", "maxLength": 40}},
        "data_notes": {"type": "string", "maxLength": 2000},
        "source_links": {"type": "array", "minItems": 1, "maxItems": 12,
                         "items": {"type": "string", "maxLength": 300}},
    },
    "required": ["checked_at_utc", "company", "cik", "period", "rows",
                 "unresolved_metrics", "data_notes", "source_links"],
    "additionalProperties": False,
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of violations (empty = valid) for the supported subset."""
    errs: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}.{k}: not allowed")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if len(value) > schema.get("maxItems", 10**9):
            errs.append(f"{path}: too many items")
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{path}: too few items")
        for i, v in enumerate(value):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if len(value) > schema["maxLength"] or len(value) < schema.get("minLength", 0):
            errs.append(f"{path}: length {len(value)} out of range")
    elif t in ("number", "integer"):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if t == "integer":
            ok = ok and float(value).is_integer()
        if not ok:
            return [f"{path}: expected {t}"]
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum")
    elif t == "boolean":
        if not isinstance(value, bool):
            errs.append(f"{path}: expected boolean")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    return errs
