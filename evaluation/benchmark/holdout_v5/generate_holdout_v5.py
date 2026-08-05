"""Deterministically author the fresh held-out v5 contract and fixtures.

The generator is deliberately data-only: it has no RNG, model calls or network access.
It refuses to overwrite a formal set.  Novelty is mechanically checked against the base
98 and v2 held-out case/fixture identifiers, addresses, prices and verbatim queries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "evaluation" / "benchmark" / "holdout_v5"
FIX = OUT / "fixtures"
BASE_CASES = REPO / "evaluation" / "benchmark" / "cases.jsonl"
V2_CASES = REPO / "evaluation" / "benchmark" / "holdout_v2" / "cases_holdout_v2.jsonl"
BASE_FIX = REPO / "evaluation" / "benchmark" / "fixtures"
V2_FIX = REPO / "evaluation" / "benchmark" / "holdout_v2" / "fixtures"
V3_CASES = REPO / "evaluation" / "benchmark" / "holdout_v3" / "cases_holdout_v3.jsonl"
V3_FIX = REPO / "evaluation" / "benchmark" / "holdout_v3" / "fixtures"
V4_CASES = REPO / "evaluation" / "benchmark" / "holdout_v4" / "cases_holdout_v4.jsonl"
V4_FIX = REPO / "evaluation" / "benchmark" / "holdout_v4" / "fixtures"
SCHEMA = "rentcompass/benchmark/v5"

AREAS = (
    "Alabaster Reach", "Blackthorn Grove", "Coral Wharf", "Dewberry Vale", "Evergreen Park",
    "Frostmere", "Goldfinch Rise", "Heather Quay", "Ivory Green", "Jet Hollow",
    "Kingfisher Fields", "Linden Cross", "Marigold Yard", "Northstar Heath", "Olive Terrace",
    "Poppywick", "Quill End", "Redwood Bay", "Silver Mews", "Tansy Gardens",
    "Umber Hill", "Verona Lock", "Wildrose Ford", "Xenia Court", "Yewtree Row",
    "Zenith Place", "Amber Brook", "Bramble Point", "Cloverlea", "Dawn Common",
)
DESTS = ("Waterloo", "Moorgate", "Liverpool Street", "King's Cross", "Victoria",
         "London Bridge", "Paddington", "Canary Wharf", "Farringdon", "Euston")
STREETS = ("Acorn", "Beacon", "Crescent", "Dovetail", "Estuary", "Fountain",
           "Gossamer", "Hearth", "Island", "Juniper", "Kite", "Lantern")
RETRIEVAL_TEMPLATES = (
    "Create an exact rental shortlist: a furnished 1-bedroom flat in {area}, at £{budget} a month or below, available by {move}.",
    "Only show homes passing all of these filters in {area}: furnished 1-bedroom flat, no more than £{budget} a month, move in by {move}.",
    "My requirement is a furnished 1-bedroom flat around {area} with a £{budget} monthly cap and availability by {move}; remove failures.",
    "Find a qualifying furnished 1-bedroom flat in {area}. The price must stay within £{budget} a month and the date must be no later than {move}.",
    "For a strict search in {area}, return furnished 1-bedroom flats only, with rent up to £{budget} a month and move-in by {move}.",
)
COMMUTE_SUFFIXES = (
    " Independently confirm each listing can reach {dest} in {limit} minutes or less.",
    " A validated journey cap of {limit} minutes to {dest} applies to every proposed property.",
    " For each candidate, require a separately evidenced commute of at most {limit} minutes to {dest}.",
)
NO_RESULT_TEMPLATES = (
    "Seek an exact furnished 1-bedroom flat in {area}, within £{budget} a month and ready by {move}; state the empty result if none exist.",
    "I need a furnished 1-bedroom flat in {area}, maximum £{budget} a month and available by {move}. If there is no exact option, be direct.",
    "Check for a furnished 1-bedroom flat in {area} below £{budget} a month with a move date by {move}; never invent a fallback if the set is empty.",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def source_fingerprint() -> tuple[set[str], set[float], set[str]]:
    queries, prices, addresses = set(), set(), set()
    for cases, root in ((BASE_CASES, BASE_FIX), (V2_CASES, V2_FIX), (V3_CASES, V3_FIX), (V4_CASES, V4_FIX)):
        for case in jsonl(cases):
            queries.add(str(case.get("user_query") or "").strip().casefold())
            names = case.get("fixture")
            names = [names] if isinstance(names, str) else (names or [])
            for name in names:
                path = root / name
                if not path.is_file():
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
                records = raw.get("results", [raw]) if isinstance(raw, dict) else []
                for rec in records:
                    data = rec.get("data") if isinstance(rec, dict) else None
                    if not isinstance(data, dict):
                        continue
                    for key in ("recommendations", "over_budget_alternatives"):
                        for row in data.get(key) or []:
                            if not isinstance(row, dict):
                                continue
                            if isinstance(row.get("price_raw"), (int, float)):
                                prices.add(float(row["price_raw"]))
                            if isinstance(row.get("address"), str):
                                addresses.add(row["address"].strip().casefold())
    return queries, prices, addresses


def choose_price(seed: int, banned: set[float], used: set[float]) -> int:
    value = 2039 + seed * 29
    while float(value) in banned or float(value) in used:
        value += 19
    used.add(float(value))
    return value


def listing(case_no: int, tag: str, *, area: str, price: int, bedrooms: int = 1,
            room_type: str = "flat", available: str = "2026-10-15",
            features: list[str] | None = None) -> dict:
    street = STREETS[(case_no + len(tag)) % len(STREETS)]
    number = 9000 + case_no * 17 + ord(tag[0]) % 10
    lid = f"ho5-{case_no:03d}-{tag.lower()}"
    return {
        "eval_listing_id": lid,
        "uid_token": lid,
        "rank": len(tag),
        "address": f"{number} {street} Quay, {area}, London V{case_no % 9 + 1} {case_no % 8 + 1}AB",
        "url": f"https://fixtures.rentcompass.invalid/holdout-v5/{lid}",
        "price": f"£{price:,}/month",
        "price_raw": price,
        "bedrooms": bedrooms,
        "room_type_normalized": room_type,
        "property_type": f"{room_type.title()} · {bedrooms} bed",
        "area_normalized": area,
        "borough": area,
        "city": "London",
        "postcode_district": f"V{case_no % 9 + 1}",
        "postcode_sector": f"V{case_no % 9 + 1} {case_no % 8 + 1}",
        "available_from": available,
        "available_from_normalized": available,
        "features": list(features or ["furnished"]),
        "verified_features": list(features or ["furnished"]),
        "source": "frozen_holdout_v5",
    }


def hard_constraints(area: str, budget: int, move: str, *, move_text: str | None = None,
                     commute: int | None = None, dest: str | None = None) -> list[dict]:
    items = [
        {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=", "value": budget,
         "user_text": f"£{budget}"},
        {"type": "bedroom_count_match", "op": "==", "value": 1, "user_text": "1-bedroom"},
        {"type": "room_type_match", "value": "flat", "user_text": "flat"},
        {"type": "area_match", "granularity": "borough", "value": area, "user_text": area},
        {"type": "move_in_date_satisfied", "value": move, "user_text": move_text or move},
        {"type": "property_feature_present", "value": "furnished", "user_text": "furnished"},
    ]
    if commute is not None:
        items.append({"type": "commute_leq_minutes", "value": commute,
                      "dest": dest, "user_text": f"{commute} minutes"})
    return items


def hard_case(case_no: int, *, commute: bool, no_result: bool, banned_prices: set[float], used_prices: set[float]) -> tuple[dict, dict]:
    area = AREAS[(case_no - 1) % len(AREAS)]
    budget = choose_price(100 + case_no, banned_prices, used_prices)
    good_price = choose_price(500 + case_no, banned_prices, used_prices)
    if good_price >= budget:
        good_price = max(900, budget - 37)
        while float(good_price) in banned_prices or float(good_price) in used_prices:
            good_price -= 11
        used_prices.add(float(good_price))
    move_date = (date(2026, 9, 1) + timedelta(days=(case_no * 3) % 80)).isoformat()
    move_words = date.fromisoformat(move_date).strftime("%-d %B %Y")
    limit = 25 + case_no % 16
    dest = DESTS[(case_no - 1) % len(DESTS)]
    slots = ["budget", "bedroom_count", "room_type", "area", "move_in_date", "property_feature"]
    template = RETRIEVAL_TEMPLATES[(case_no - 1) % len(RETRIEVAL_TEMPLATES)]
    query = template.format(area=area, budget=budget, move=move_words)
    if commute:
        query += COMMUTE_SUFFIXES[(case_no - 1) % len(COMMUTE_SUFFIXES)].format(limit=limit, dest=dest)
        slots.append("commute")
    constraints = hard_constraints(area, budget, move_date, move_text=move_words,
                                   commute=limit if commute else None, dest=dest if commute else None)
    if no_result:
        query = NO_RESULT_TEMPLATES[(case_no - 1) % len(NO_RESULT_TEMPLATES)].format(area=area, budget=budget, move=move_words)
        case = {
            "case_id": f"HO5-{case_no:03d}", "schema_version": SCHEMA, "task_category": "retrieval_hard",
            "category": "E_multi_constraint", "authored_on": "2026-08-05", "user_id": f"u_ho5_{case_no:03d}",
            "user_query": query, "conversation_history": [], "expected_tools": ["search_properties"],
            "forbidden_tools": [], "expected_route": "search_properties", "expected_constraints": constraints,
            "hard_constraint_slots": slots, "correct_completion": "State plainly that no frozen listing meets every stated condition; do not invent a rent or a substitute listing.",
            "completion_oracle": {"kind": "retrieval_exact_set"}, "metric_eligibility": ["recommendation_precision", "unsupported_numeric_control", "task_completion"],
            "failure_conditions": ["Claims an exact match exists.", "Provides an unsupported market rent or a made-up listing."],
            "allowed_evidence_sources": ["frozen search_properties fixture", "user request"], "expected_grounding_sources": ["frozen fixture"],
            "reference_calculations": None, "novelty_note": f"Fresh v5 no-result request {case_no}; unique synthetic area, wording and price.",
            "notes": "No-result branch. It is hard because the user explicitly stated housing conditions.", "fixture": f"ho5_{case_no:03d}_search.json",
        }
        fixture = {"tool_name": "search_properties", "success": True, "data": {
            "success": True, "status": "no_results", "recommendations": [], "total_found": 0,
            "summary": f"No exact listings in {area}.", "search_criteria": {"area": area, "max_budget": budget,
            "bedrooms": 1, "room_type": "flat", "move_in_date": move_date, "property_features": ["furnished"]}}}
        return case, fixture
    rows = [
        listing(case_no, "A", area=area, price=good_price, available=move_date),
        listing(case_no, "B", area=area, price=choose_price(700 + case_no, banned_prices, used_prices), available=move_date),
        listing(case_no, "C", area=area, price=choose_price(800 + case_no, banned_prices, used_prices), bedrooms=2, available=move_date),
        listing(case_no, "D", area=area, price=choose_price(900 + case_no, banned_prices, used_prices), room_type="house", available=move_date),
        listing(case_no, "E", area=AREAS[(case_no + 7) % len(AREAS)], price=choose_price(1000 + case_no, banned_prices, used_prices), available=move_date),
        listing(case_no, "F", area=area, price=choose_price(1100 + case_no, banned_prices, used_prices), available=(date.fromisoformat(move_date) + timedelta(days=28)).isoformat()),
        listing(case_no, "G", area=area, price=choose_price(1200 + case_no, banned_prices, used_prices), available=move_date, features=["garden"]),
    ]
    over_budget = budget + 31
    while float(over_budget) in banned_prices or float(over_budget) in used_prices:
        over_budget += 17
    used_prices.add(float(over_budget))
    rows[1]["price_raw"] = over_budget
    rows[1]["price"] = f"£{over_budget:,}/month"
    case = {
        "case_id": f"HO5-{case_no:03d}", "schema_version": SCHEMA, "task_category": "retrieval_hard",
        "category": "E_multi_constraint", "authored_on": "2026-08-05", "user_id": f"u_ho5_{case_no:03d}",
        "user_query": query, "conversation_history": [], "expected_tools": ["search_properties"] + (["calculate_commute"] if commute else []),
        "forbidden_tools": [], "expected_route": "search_properties", "expected_constraints": constraints,
        "hard_constraint_slots": slots,
        "correct_completion": "Return exactly the frozen eligible listing IDs. Excluded and unknown candidates must not appear in the eligible collection.",
        "completion_oracle": {"kind": "retrieval_exact_set"},
        "metric_eligibility": ["eligible_recall", "recommendation_precision", "complete_constraint_satisfaction"] + (["required_tool_completion"] if commute else []) + ["unsupported_numeric_control", "task_completion"],
        "required_tool_contract": {"kind": "commute_per_search_candidate"} if commute else {},
        "failure_conditions": ["Places a false positive in eligible_recommendations.", "Omits the eligible listing.", "States any unsupported price or journey time."],
        "allowed_evidence_sources": ["frozen structured listings", "frozen structured commute records", "user request"],
        "expected_grounding_sources": ["frozen fixture"], "reference_calculations": None,
        "novelty_note": f"Fresh v5 retrieval request {case_no}; unique IDs, addresses, prices, dates and wording absent from source sets.",
        "notes": "Every declared slot has a pass/fail trap. Listing IDs are the deterministic measurement unit.",
        "fixture": f"ho5_{case_no:03d}_search.json",
    }
    records = [{"tool_name": "search_properties", "success": True, "data": {
        "success": True, "status": "found", "recommendations": rows, "total_found": len(rows),
        "summary": f"Frozen candidates for {area}.",
        "search_criteria": {"area": area, "max_budget": budget, "bedrooms": 1, "room_type": "flat",
                            "move_in_date": move_date, "property_features": ["furnished"],
                            **({"max_commute_time": limit, "commute_destination": dest} if commute else {})}}}]
    if commute:
        for row in rows:
            duration = limit - 2 if row["eval_listing_id"].endswith("-a") else limit + 9
            records.append({"tool_name": "calculate_commute", "success": True, "data": {
                "origin_eval_listing_id": row["eval_listing_id"], "candidate_key": f"url:{row['url'].casefold()}",
                "from_address": row["address"], "to_address": dest, "duration_minutes": duration}})
    return case, {"results": records}


def calculation_case(case_no: int) -> tuple[dict, dict]:
    weekly = 527 + case_no * 13
    result = round(weekly * 52 / 12, 2)
    query = f"Using the specified conversion weekly × 52 ÷ 12, calculate the monthly GBP equivalent of £{weekly} per week."
    case = {
        "case_id": f"HO5-{case_no:03d}", "schema_version": SCHEMA, "task_category": "calculation",
        "category": "B_money", "authored_on": "2026-08-05", "user_id": f"u_ho5_{case_no:03d}",
        "user_query": query, "conversation_history": [], "expected_tools": [], "forbidden_tools": ["search_properties"],
        "expected_constraints": [], "hard_constraint_slots": [], "correct_completion": f"Return £{result:,.2f} using the frozen formula.",
        "completion_oracle": {"kind": "calculation", "result": result},
        "metric_eligibility": ["unsupported_numeric_control", "task_completion"],
        "failure_conditions": ["Uses ÷4.33 or another non-frozen conversion.", "Invents market evidence."],
        "allowed_evidence_sources": ["user weekly amount", "weekly × 52 ÷ 12 formula"],
        "expected_grounding_sources": ["user request", "frozen formula"],
        "reference_calculations": {"monthly_rent": {"formula": "weekly * 52 / 12", "result": result, "unit": "GBP/month"}},
        "novelty_note": f"Fresh v5 calculation amount £{weekly}/week.", "notes": "Deterministic arithmetic case.",
        "fixture": f"ho5_{case_no:03d}_calc.json",
    }
    return case, {"tool_name": "calculation_reference", "success": True, "data": {"weekly_rent": weekly, "monthly_rent": result}}


def memory_case(case_no: int) -> tuple[dict, dict]:
    preference = f"my V5 preference marker {case_no}: quiet top-floor flat"
    query = f"Please remember {preference} for future searches."
    case = {
        "case_id": f"HO5-{case_no:03d}", "schema_version": SCHEMA, "task_category": "memory",
        "category": "G_memory", "authored_on": "2026-08-05", "user_id": f"u_ho5_{case_no:03d}",
        "user_query": query, "conversation_history": [], "expected_tools": ["remember"], "forbidden_tools": ["search_properties"],
        "expected_constraints": [], "hard_constraint_slots": [], "correct_completion": "Execute remember successfully, then acknowledge the saved preference.",
        "completion_oracle": {"kind": "memory_write", "ack_markers_any": ["saved", "remember", "记住"]},
        "metric_eligibility": ["required_tool_completion", "unsupported_numeric_control", "task_completion"],
        "required_tool_contract": {"kind": "remember_write"},
        "failure_conditions": ["Claims saved without a successful remember call.", "Searches instead of persisting the explicit request."],
        "allowed_evidence_sources": ["user request", "remember tool result"], "expected_grounding_sources": ["remember"],
        "reference_calculations": None, "novelty_note": f"Fresh v5 memory payload {case_no}.", "notes": "Explicit write-side-effect contract.",
        "fixture": f"ho5_{case_no:03d}_memory.json",
    }
    return case, {"tool_name": "remember", "success": True, "data": {"saved": True, "kind": "semantic"}}


def clarify_case(case_no: int) -> tuple[dict, dict]:
    query = f"Before searching for a rental, I have not selected either location or budget. Ask me one essential question first (v5 clarification {case_no})."
    case = {
        "case_id": f"HO5-{case_no:03d}", "schema_version": SCHEMA, "task_category": "clarify",
        "category": "A_retrieval", "authored_on": "2026-08-05", "user_id": f"u_ho5_{case_no:03d}",
        "user_query": query, "conversation_history": [], "expected_tools": ["ask_user"], "forbidden_tools": ["search_properties"],
        "expected_route": "clarification", "expected_constraints": [], "hard_constraint_slots": [],
        "correct_completion": "Ask a focused clarifying question and do not pretend that a search occurred.",
        "completion_oracle": {"kind": "clarification", "markers_any": ["area", "budget", "where", "location"]},
        "metric_eligibility": ["unsupported_numeric_control", "task_completion"],
        "failure_conditions": ["Runs a property search before the missing essentials are supplied.", "Invents listings."],
        "allowed_evidence_sources": ["user request"], "expected_grounding_sources": ["user request"],
        "reference_calculations": None, "novelty_note": f"Fresh v5 clarification wording {case_no}.", "notes": "No invented search result.",
        "fixture": f"ho5_{case_no:03d}_clarify.json",
    }
    return case, {"tool_name": "ask_user", "success": True, "data": {"question": "Which area should I search first?", "clarification_kind": "missing_area", "missing_fields": ["area"]}}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def build() -> None:
    cases_path = OUT / "cases_holdout_v5.jsonl"
    if cases_path.exists() or FIX.exists():
        raise SystemExit("refusing to overwrite existing v5 formal cases or fixtures")
    FIX.mkdir(parents=True)
    banned_queries, banned_prices, banned_addresses = source_fingerprint()
    used_prices: set[float] = set()
    cases: list[dict] = []
    for n in range(1, 31):
        case, fixture = hard_case(n, commute=True, no_result=False, banned_prices=banned_prices, used_prices=used_prices)
        cases.append(case); write_json(FIX / case["fixture"], fixture)
    for n in range(31, 61):
        case, fixture = hard_case(n, commute=False, no_result=False, banned_prices=banned_prices, used_prices=used_prices)
        cases.append(case); write_json(FIX / case["fixture"], fixture)
    for n in range(61, 91):
        case, fixture = hard_case(n, commute=False, no_result=True, banned_prices=banned_prices, used_prices=used_prices)
        cases.append(case); write_json(FIX / case["fixture"], fixture)
    for n in range(91, 121):
        case, fixture = calculation_case(n); cases.append(case); write_json(FIX / case["fixture"], fixture)
    for n in range(121, 151):
        case, fixture = memory_case(n); cases.append(case); write_json(FIX / case["fixture"], fixture)
    for n in range(151, 181):
        case, fixture = clarify_case(n); cases.append(case); write_json(FIX / case["fixture"], fixture)
    assert len(cases) == 180
    queries = [c["user_query"].strip().casefold() for c in cases]
    if len(queries) != len(set(queries)) or set(queries) & banned_queries:
        raise AssertionError("verbatim query collision")
    all_ids, all_addresses, all_prices = set(), set(), set()
    for path in sorted(FIX.glob("*.json")):
        raw = json.loads(path.read_text())
        records = raw.get("results", [raw]) if isinstance(raw, dict) else []
        for rec in records:
            data = rec.get("data") if isinstance(rec, dict) else None
            for row in (data or {}).get("recommendations", []):
                lid, address, price = row.get("eval_listing_id"), row.get("address"), row.get("price_raw")
                if lid in all_ids or str(address).casefold() in all_addresses or float(price) in all_prices:
                    raise AssertionError(f"v5 duplicate listing identity in {path.name}")
                if str(address).casefold() in banned_addresses or float(price) in banned_prices:
                    raise AssertionError(f"source listing collision in {path.name}")
                all_ids.add(lid); all_addresses.add(str(address).casefold()); all_prices.add(float(price))
    cases_path.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases), encoding="utf-8")
    manifest = {
        "generator": str(Path(__file__).relative_to(REPO)), "generator_sha256": sha(Path(__file__)),
        "cases_sha256": sha(cases_path), "n_cases": len(cases), "n_fixtures": len(list(FIX.glob("*.json"))),
        "fixtures": {p.name: sha(p) for p in sorted(FIX.glob("*.json"))},
        "novelty_audit": {"verbatim_query_overlap": 0, "listing_id_overlap": 0,
                          "address_overlap": 0, "price_overlap": 0},
    }
    write_json(OUT / "MANIFEST.json", manifest)
    (OUT / "AUTHOR_AUDIT.md").write_text(
        "# Author audit\n\n"
        "This is an author-side static audit, not human outcome review.  It ran before any formal model request.\n\n"
        f"- Cases: {len(cases)}; fixture files: {manifest['n_fixtures']}.\n"
        "- Exact-query, listing-ID, address and price overlap with base98, holdout-v2 and holdout-v3: 0.\n"
        "- Every non-empty retrieval-hard fixture has unique listing IDs and one pass/fail trap per declared slot.\n"
        "- Formal outcomes have not been observed.\n", encoding="utf-8")
    print(json.dumps({"cases": len(cases), "fixtures": manifest["n_fixtures"], "cases_sha256": manifest["cases_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="actually author the formal set")
    args = parser.parse_args()
    if not args.write:
        raise SystemExit("refusing to author without --write")
    build()
