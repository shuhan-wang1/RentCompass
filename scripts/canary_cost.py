#!/usr/bin/env python3
"""Offline cost attribution for canary.turn records.

Cost is deliberately NOT computed in the request path: the hot path records raw
token usage only, and money is applied here from a VERSIONED price table. A price
change therefore never rewrites the cost of a historical run — you re-run this
tool with a different --prices and get a second, separately-versioned answer.

Fail-closed: a price table marked "unverified" (or missing a rate for a model that
actually appears in the data) refuses to produce a number, because a plausible-looking
wrong cost is worse than no cost.

    python3 scripts/canary_cost.py .runtime/logs/canary-fc_loop.jsonl \
        --prices scripts/pricing/deepseek_prices_v1.json
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from canary_report import load_records, resolve_inputs  # noqa: E402


def load_prices(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def sum_usage(records: List[dict]) -> Dict[str, Dict[str, int]]:
    """Total tokens per model across records. Records with no llm_usage are counted
    as UNMEASURED (reported separately) rather than as zero spend."""
    per_model: Dict[str, Dict[str, int]] = {}
    unmeasured = 0
    for r in records:
        u = r.get("llm_usage")
        if not isinstance(u, dict) or not u.get("models"):
            unmeasured += 1
            continue
        for model, m in u["models"].items():
            slot = per_model.setdefault(
                model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0})
            for k in ("calls", "input_tokens", "output_tokens", "cache_read_tokens"):
                slot[k] += int(m.get(k) or 0)
    per_model["_unmeasured_turns"] = {"count": unmeasured}  # type: ignore[assignment]
    return per_model


def compute_cost(per_model: dict, prices: dict, allow_unverified: bool) -> dict:
    if prices.get("unverified") and not allow_unverified:
        return {"ok": False,
                "error": f"price table v{prices.get('price_table_version')} is marked "
                         f"unverified — refusing to compute cost. Fill in the real rates "
                         f"and set unverified=false (or pass --allow-unverified to estimate)."}
    rates = prices.get("models") or {}
    total = 0.0
    lines = []
    missing = []
    for model, m in per_model.items():
        if model.startswith("_"):
            continue
        r = rates.get(model)
        if not r or any(r.get(k) is None for k in ("input", "output", "cache_read")):
            missing.append(model)
            continue
        cost = (m["input_tokens"] * r["input"]
                + m["output_tokens"] * r["output"]
                + m["cache_read_tokens"] * r["cache_read"]) / 1_000_000.0
        total += cost
        lines.append({"model": model, **m, "cost": round(cost, 6)})
    if missing:
        return {"ok": False,
                "error": f"no rate for model(s) {missing} in price table "
                         f"v{prices.get('price_table_version')} — refusing to under-report cost."}
    return {"ok": True, "currency": prices.get("currency"),
            "price_table_version": prices.get("price_table_version"),
            "total_cost": round(total, 6), "by_model": lines}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--prices", required=True)
    ap.add_argument("--allow-unverified", action="store_true")
    a = ap.parse_args()

    records, _ = load_records(resolve_inputs(a.inputs))
    per_model = sum_usage(records)
    prices = load_prices(a.prices)
    result = compute_cost(per_model, prices, a.allow_unverified)
    result["usage"] = per_model
    result["records"] = len(records)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
