"""Validate the RentCompass Phase 2 benchmark.

Run from the repo root as either:

    python -m evaluation.benchmark.validate
    python evaluation/benchmark/validate.py

Checks performed:
  1. Every row in cases.jsonl is valid JSON and validates against schema.json
     (uses `jsonschema` if importable; otherwise a minimal structural fallback,
     see MINIMAL_CHECK note below).
  2. `case_id` values are unique.
  3. Every `expected_tools` / `forbidden_tools` entry is a REAL registry tool, and every
     `expected_route` is a real registry tool OR a documented pseudo-route. The real-tool
     list is DERIVED from `create_tool_registry()` — see TOOL NAMES below.
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
REPO_ROOT = HERE.parents[1]
SCHEMA_PATH = HERE / "schema.json"
CASES_PATH = HERE / "cases.jsonl"
FIXTURES_DIR = HERE / "fixtures"

# ── TOOL NAMES ──────────────────────────────────────────────────────────────────────────
# This used to be a hand-copied `REAL_TOOLS` literal, commented "the 14 real registry tools
# (app/core/tool_system.py create_tool_registry)". A copy of a list is not the list, and this
# one had a second defect on top: `expected_tools` and `forbidden_tools` were checked against
# `REAL_TOOLS | PSEUDO_ROUTES`, so a PSEUDO-ROUTE in either field validated clean.
#
# That is exactly how F7 shipped with `expected_tools: ["market_info"]`. `market_info` is a
# graph router decision, never a registry tool, so it can never appear in an executed tool
# trace — and `graders.route_matches` scores `expected_tools` as a subset of that trace. F7's
# route could therefore never match, for any run of any architecture, and it cost a route
# point in every round ever run. The case was fixed; the validator that should have caught it
# was not. Fixing it is the point of this block.
#
# Now derived from `create_tool_registry()` — the SAME registry whose `execute_tool(name, ...)`
# produces the traces being matched. Registering a new tool widens this automatically;
# retiring one narrows it. Same approach (and same rationale) as
# `tests/test_case_contract_consistency.py::_registered_tool_names`, which already derives its
# list this way; reused rather than reinvented, so there is one definition of "a real tool".


def _bootstrap_app_path() -> None:
    """Put the app packages on sys.path exactly as run_benchmark/rescore do, so the registry
    imports identically. No graph is built and no tool is ever executed."""
    for p in (REPO_ROOT / "app", REPO_ROOT / "src", REPO_ROOT):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def registered_tool_names() -> frozenset:
    """The names `registry.execute_tool` accepts, i.e. the only names that can ever show up in
    a tool trace. A hard failure if the registry cannot be imported: falling back to a literal
    is the defect this function exists to remove, and a validator that silently degrades to a
    stale list is worse than one that stops."""
    _bootstrap_app_path()
    try:
        from core.tool_system import create_tool_registry  # type: ignore
    except Exception as exc:  # pragma: no cover - environment problem, not a data problem
        raise SystemExit(
            f"[FATAL] cannot import the tool registry to validate tool names: "
            f"{type(exc).__name__}: {exc}\n"
            "        Tool names MUST be derived from create_tool_registry() — a hand-copied "
            "list is what let F7's `expected_tools: [\"market_info\"]` through."
        )
    return frozenset(create_tool_registry().tools)


# Graph-internal router decisions (NOT registry tools). Legal in `expected_route`; NEVER legal
# in a trace-matched tool field. Mirrors tests/test_case_contract_consistency.PSEUDO_ROUTES.
PSEUDO_ROUTES = {
    "market_info",
    "direct_answer",
    "multi_search",
    "reasoning_property",
    "clarification",
}

# Fields the graders match against the EXECUTED tool trace, so they may only name real tools.
TRACE_MATCHED_TOOL_FIELDS = ("expected_tools", "forbidden_tools")


def tool_field_problems(cid, case: dict, real_tools) -> list:
    """Every tool/route naming problem in one case. Split out of main() so it can be tested
    on its own: main()'s other checks are corpus-WIDE (category coverage, smoke counts) and
    would drown a single-case fixture in unrelated failures."""
    out = []
    for field in TRACE_MATCHED_TOOL_FIELDS:
        for tool in case.get(field, []) or []:
            if tool in PSEUDO_ROUTES:
                # THE F7 defect, stated in the terms that make it a defect.
                out.append(
                    f"{cid}: {field} names the pseudo-route '{tool}', which is a graph "
                    "router decision and can never appear in an executed tool trace — "
                    "the case is unsatisfiable by construction")
            elif tool not in real_tools:
                out.append(f"{cid}: {field} references unknown tool '{tool}' "
                           "(not in create_tool_registry())")
    route = case.get("expected_route")
    if route is not None and route not in (set(real_tools) | PSEUDO_ROUTES):
        out.append(f"{cid}: expected_route '{route}' is not a real tool/route")
    return out


VALID_CATEGORIES = {
    "A_retrieval", "B_money", "C_commute", "D_crime_poi",
    "E_multi_constraint", "F_grounding", "G_memory",
}
# Categories that are LEGAL but not required for coverage: the guard-regression shard
# (cases_guard_regression.jsonl) lives outside the base suite, so its category must
# validate per-case without the base cases.jsonl being flagged as "missing" it.
EXTRA_CATEGORIES = {"H_guard_regression", "cold_resilience"}


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
    real_tools = registered_tool_names()
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

        # tool / route reality checks (derived from the registry — see TOOL NAMES above)
        problems.extend(tool_field_problems(cid, case, real_tools))

        # category prefix consistency
        if isinstance(cid, str) and not (
                cid.startswith("CR") or cid[:1] in {"A", "B", "C", "D", "E", "F", "G", "H"}):
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
    print(f"Registry tools (from create_tool_registry()): {len(real_tools)}")
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
