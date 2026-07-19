"""Validate the RentCompass Phase 2 benchmark.

Run from the repo root as either:

    python -m evaluation.benchmark.validate
    python evaluation/benchmark/validate.py

Checks performed:
  1. Every row in cases.jsonl is valid JSON and validates against schema.json
     (uses `jsonschema` if importable; otherwise a minimal structural fallback,
     see MINIMAL_CHECK note below).
  2. `case_id` values are unique.
  3. Every `expected_tools` / `forbidden_tools` / `expected_route` entry is a REAL
     registry tool or a documented pseudo-route (no invented tools).
  4. Every referenced `fixture` file exists under fixtures/.
  5. Every `smoke` case is a bool; at least one smoke case per represented rule set.
  6. Prints per-category counts and the smoke count.

Exits non-zero on ANY violation.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.json"
CASES_PATH = HERE / "cases.jsonl"
FIXTURES_DIR = HERE / "fixtures"

# The 14 real registry tools (app/core/tool_system.py create_tool_registry) ...
REAL_TOOLS = {
    "search_properties",
    "calculate_commute",
    "calculate_commute_cost",
    "check_safety",
    "get_weather",
    "web_search",
    "search_nearby_pois",
    "get_property_details",
    "check_transport_cost",
    "get_transport_info",
    "recall_memory",
    "remember",
    "ask_user",
    "compare_or_rank_areas",
}
# ... plus the graph-internal pseudo-routes (NOT registry tools).
PSEUDO_ROUTES = {
    "market_info",
    "direct_answer",
    "multi_search",
    "reasoning_property",
    "clarification",
}
VALID_TARGETS = REAL_TOOLS | PSEUDO_ROUTES

VALID_CATEGORIES = {
    "A_retrieval", "B_money", "C_commute", "D_crime_poi",
    "E_multi_constraint", "F_grounding", "G_memory",
}
# Categories that are LEGAL but not required for coverage: the guard-regression shard
# (cases_guard_regression.jsonl) lives outside the base suite, so its category must
# validate per-case without the base cases.jsonl being flagged as "missing" it.
EXTRA_CATEGORIES = {"H_guard_regression"}


def _load_cases() -> list[dict]:
    rows = []
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((lineno, json.loads(line)))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"[FATAL] cases.jsonl line {lineno}: invalid JSON: {exc}")
    return rows


def _schema_validator():
    """Return (validate_fn, mode_str). Prefers jsonschema; falls back to structural."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        import jsonschema  # type: ignore
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        def _v(obj):
            return [f"{'/'.join(str(p) for p in e.path)}: {e.message}"
                    for e in validator.iter_errors(obj)]

        return _v, "jsonschema"
    except Exception:
        # MINIMAL_CHECK fallback (dependency: `pip install jsonschema` for full checks).
        required = schema["required"]

        def _v(obj):
            errs = []
            for key in required:
                if key not in obj:
                    errs.append(f"missing required field: {key}")
            if obj.get("category") not in VALID_CATEGORIES | EXTRA_CATEGORIES:
                errs.append(f"bad category: {obj.get('category')}")
            if not isinstance(obj.get("conversation_history"), list):
                errs.append("conversation_history must be a list")
            if not isinstance(obj.get("expected_constraints"), list):
                errs.append("expected_constraints must be a list")
            if not isinstance(obj.get("failure_conditions"), list) or not obj.get("failure_conditions"):
                errs.append("failure_conditions must be a non-empty list")
            return errs

        return _v, "structural-fallback"


def main() -> int:
    problems: list[str] = []

    if not SCHEMA_PATH.exists():
        raise SystemExit(f"[FATAL] schema not found: {SCHEMA_PATH}")
    if not CASES_PATH.exists():
        raise SystemExit(f"[FATAL] cases not found: {CASES_PATH}")

    validate_fn, mode = _schema_validator()
    rows = _load_cases()

    seen_ids: set[str] = set()
    categories: Counter[str] = Counter()
    smoke_count = 0
    smoke_categories: set[str] = set()

    for lineno, case in rows:
        cid = case.get("case_id", f"<line {lineno}>")

        for err in validate_fn(case):
            problems.append(f"{cid}: schema: {err}")

        if cid in seen_ids:
            problems.append(f"{cid}: duplicate case_id")
        seen_ids.add(cid)

        categories[case.get("category", "?")] += 1

        # tool / route reality checks
        for field in ("expected_tools", "forbidden_tools"):
            for tool in case.get(field, []):
                if tool not in VALID_TARGETS:
                    problems.append(f"{cid}: {field} references unknown tool/route '{tool}'")
        route = case.get("expected_route")
        if route is not None and route not in VALID_TARGETS:
            problems.append(f"{cid}: expected_route '{route}' is not a real tool/route")

        # category prefix consistency
        if isinstance(cid, str) and cid[:1] not in {"A", "B", "C", "D", "E", "F", "G", "H"}:
            problems.append(f"{cid}: case_id prefix is not a category letter")

        # fixtures exist
        fx = case.get("fixture")
        if fx is not None:
            names = [fx] if isinstance(fx, str) else list(fx)
            for name in names:
                if not (FIXTURES_DIR / name).exists():
                    problems.append(f"{cid}: fixture '{name}' not found under fixtures/")

        # smoke bookkeeping
        if case.get("smoke") is True:
            smoke_count += 1
            smoke_categories.add(case.get("category"))
        elif "smoke" in case and not isinstance(case["smoke"], bool):
            problems.append(f"{cid}: smoke must be a boolean")

    # coverage assertions
    missing_cats = VALID_CATEGORIES - set(categories)
    if missing_cats:
        problems.append(f"missing categories entirely: {sorted(missing_cats)}")
    if smoke_count < 1:
        problems.append("no smoke cases marked")
    smoke_missing = VALID_CATEGORIES - smoke_categories
    if smoke_missing:
        problems.append(f"categories with no smoke case: {sorted(smoke_missing)}")

    # ---- report ----
    print(f"Schema validation mode: {mode}")
    print(f"Total cases: {len(rows)}")
    print("Per-category counts:")
    for cat in sorted(VALID_CATEGORIES):
        print(f"  {cat:20s} {categories.get(cat, 0)}")
    print(f"Smoke cases: {smoke_count}")
    print(f"Fixtures on disk: {len(list(FIXTURES_DIR.glob('*.json'))) if FIXTURES_DIR.exists() else 0}")

    if problems:
        print(f"\nFAILED with {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("\nOK: all cases valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
