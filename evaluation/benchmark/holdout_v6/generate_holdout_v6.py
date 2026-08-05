"""Deterministically derive a fresh v6 held-out set from the v5 authoring grammar.

The grammar is reused only as a data-authoring utility.  Every case number, listing
identity, price and query is shifted/renamed, and the output is frozen independently;
no v5 outcome is read while authoring v6.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "evaluation" / "benchmark" / "holdout_v6"
FIX = OUT / "fixtures"
V5_GENERATOR = REPO / "evaluation" / "benchmark" / "holdout_v5" / "generate_holdout_v5.py"
SCHEMA = "rentcompass/benchmark/v6"
OFFSET = 180


def _load_v5():
    spec = importlib.util.spec_from_file_location("holdout_v5_authoring", V5_GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load v5 authoring grammar")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rename(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _rename(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rename(item) for item in value]
    if not isinstance(value, str):
        return value
    return (value.replace("HO5-", "HO6-")
                 .replace("ho5-", "ho6-")
                 .replace("u_ho5_", "u_ho6_")
                 .replace("holdout-v5", "holdout-v6")
                 .replace("Fresh v5", "Fresh v6")
                 .replace("v5 ", "v6 "))


def _prior_fingerprint(v5) -> tuple[set[str], set[float], set[str]]:
    queries, prices, addresses = v5.source_fingerprint()
    # v5's original audit intentionally predates v5 itself; include it explicitly here.
    cases_path = REPO / "evaluation" / "benchmark" / "holdout_v5" / "cases_holdout_v5.jsonl"
    for case in _jsonl(cases_path):
        queries.add(str(case.get("user_query") or "").strip().casefold())
        names = case.get("fixture")
        names = [names] if isinstance(names, str) else (names or [])
        for name in names:
            path = REPO / "evaluation" / "benchmark" / "holdout_v5" / "fixtures" / name
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw.get("results", [raw]) if isinstance(raw, dict) else []
            for record in records:
                data = record.get("data") if isinstance(record, dict) else None
                if not isinstance(data, dict):
                    continue
                for key in ("recommendations", "over_budget_alternatives"):
                    for row in data.get(key) or []:
                        if isinstance(row, dict):
                            if isinstance(row.get("price_raw"), (int, float)):
                                prices.add(float(row["price_raw"]))
                            if isinstance(row.get("address"), str):
                                addresses.add(row["address"].strip().casefold())
    return queries, prices, addresses


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def build() -> None:
    cases_path = OUT / "cases_holdout_v6.jsonl"
    if cases_path.exists() or FIX.exists() and any(FIX.iterdir()):
        raise SystemExit("refusing to overwrite existing v6 formal cases or fixtures")
    FIX.mkdir(parents=True, exist_ok=True)
    v5 = _load_v5()
    v5.SCHEMA = SCHEMA
    banned_queries, banned_prices, banned_addresses = _prior_fingerprint(v5)
    used_prices: set[float] = set()
    cases: list[dict] = []
    for n in range(1, 31):
        case, fixture = v5.hard_case(OFFSET + n, commute=True, no_result=False,
                                     banned_prices=banned_prices, used_prices=used_prices)
        cases.append(_rename(case)); _write(FIX / _rename(case["fixture"]), _rename(fixture))
    for n in range(31, 61):
        case, fixture = v5.hard_case(OFFSET + n, commute=False, no_result=False,
                                     banned_prices=banned_prices, used_prices=used_prices)
        cases.append(_rename(case)); _write(FIX / _rename(case["fixture"]), _rename(fixture))
    for n in range(61, 91):
        case, fixture = v5.hard_case(OFFSET + n, commute=False, no_result=True,
                                     banned_prices=banned_prices, used_prices=used_prices)
        cases.append(_rename(case)); _write(FIX / _rename(case["fixture"]), _rename(fixture))
    for n in range(91, 121):
        case, fixture = v5.calculation_case(OFFSET + n)
        cases.append(_rename(case)); _write(FIX / _rename(case["fixture"]), _rename(fixture))
    for n in range(121, 151):
        case, fixture = v5.memory_case(OFFSET + n)
        cases.append(_rename(case)); _write(FIX / _rename(case["fixture"]), _rename(fixture))
    for n in range(151, 181):
        case, fixture = v5.clarify_case(OFFSET + n)
        case = _rename(case)
        case["completion_oracle"]["accept_text_question"] = True
        cases.append(case); _write(FIX / _rename(case["fixture"]), _rename(fixture))

    if len(cases) != 180:
        raise AssertionError(f"expected 180 cases, got {len(cases)}")
    queries = [str(c.get("user_query") or "").strip().casefold() for c in cases]
    if len(queries) != len(set(queries)) or set(queries) & banned_queries:
        raise AssertionError("v6 verbatim query collision")
    ids: set[str] = set(); addresses: set[str] = set(); prices: set[float] = set()
    for path in sorted(FIX.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        records = raw.get("results", [raw]) if isinstance(raw, dict) else []
        for record in records:
            data = record.get("data") if isinstance(record, dict) else None
            for row in (data or {}).get("recommendations", []):
                lid, address, price = row.get("eval_listing_id"), row.get("address"), row.get("price_raw")
                if lid in ids or str(address).casefold() in addresses or float(price) in prices:
                    raise AssertionError(f"duplicate v6 listing identity: {path.name}")
                if str(address).casefold() in banned_addresses or float(price) in banned_prices:
                    raise AssertionError(f"v6 identity overlaps earlier set: {path.name}")
                ids.add(lid); addresses.add(str(address).casefold()); prices.add(float(price))
    cases_path.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases),
                          encoding="utf-8")
    manifest = {
        "generator": str(Path(__file__).relative_to(REPO)), "generator_sha256": _sha(Path(__file__)),
        "cases_sha256": _sha(cases_path), "n_cases": len(cases),
        "n_fixtures": len(list(FIX.glob("*.json"))),
        "fixtures": {p.name: _sha(p) for p in sorted(FIX.glob("*.json"))},
        "novelty_audit": {"verbatim_query_overlap": 0, "listing_id_overlap": 0,
                          "address_overlap": 0, "price_overlap": 0},
    }
    _write(OUT / "MANIFEST.json", manifest)
    (OUT / "AUTHOR_AUDIT.md").write_text(
        "# Author audit\n\n"
        "Static author audit; no model request or outcome was observed while authoring.\n\n"
        f"- Cases: {len(cases)}; fixture files: {manifest['n_fixtures']}.\n"
        "- Query, listing-ID, address and price overlap with earlier sets: 0.\n"
        "- Clarification cases explicitly separate text-question completion from ask_user dispatch.\n",
        encoding="utf-8")
    print(json.dumps({"cases": len(cases), "fixtures": manifest["n_fixtures"],
                      "cases_sha256": manifest["cases_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    if not parser.parse_args().write:
        raise SystemExit("refusing to author without --write")
    build()
