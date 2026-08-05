"""Deterministic tests for hard-constraint schema v2.

Run: python evaluation/results/_harness/test_constraint_schema_v2.py
Exit 0 = all pass. The held-out authoring step is BLOCKED until this exits 0
(task brief §一: "测试未通过不得继续出题").

No pytest dependency on purpose — this has to run inside the eval container.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import constraint_schema_v2 as v2  # noqa: E402

FAILURES = []
CHECKS = [0]


def check(name, got, want):
    CHECKS[0] += 1
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def L(**kw):
    """A frozen-fixture listing with sensible v2 defaults."""
    base = {"uid_token": "Fernbrook Row", "address": "12 Fernbrook Row, London N1 4QQ",
            "price_raw": 1400, "bedrooms": 1, "room_type_normalized": "flat",
            "area_normalized": "Islington", "borough": "Islington", "city": "London",
            "postcode_district": "N1", "postcode_sector": "N1 4",
            "available_from_normalized": "2026-09-01", "features": ["furnished"]}
    base.update(kw)
    return base


# ========================================================================== #
# 1. user-text normalisation                                                  #
# ========================================================================== #
def test_normalisation():
    check("budget pcm", v2.normalise_budget("under £1,500 a month"), 1500.0)
    check("budget bare", v2.normalise_budget("my budget is 1200"), 1200.0)
    check("budget pw", v2.normalise_budget("£350 pw"), round(350 * 52 / 12, 2))
    check("budget per week", v2.normalise_budget("£300 per week max"), round(300 * 52 / 12, 2))
    check("budget none", v2.normalise_budget("something cheap"), None)

    check("bed exact", v2.normalise_bedroom_count("a 2-bed flat"), {"op": "==", "value": 2})
    check("bed word", v2.normalise_bedroom_count("two bedroom place"), {"op": "==", "value": 2})
    check("bed atleast", v2.normalise_bedroom_count("at least 2 bedrooms"),
          {"op": ">=", "value": 2})
    check("bed plus", v2.normalise_bedroom_count("3+ bedrooms"), {"op": ">=", "value": 3})
    check("bed atmost", v2.normalise_bedroom_count("no more than 3 bedrooms"),
          {"op": "<=", "value": 3})
    check("bed range", v2.normalise_bedroom_count("2 to 3 bedrooms"),
          {"op": "between", "value": [2, 3]})
    check("bed range dash", v2.normalise_bedroom_count("2-3 bed"),
          {"op": "between", "value": [2, 3]})
    check("bed studio-not-count", v2.normalise_bedroom_count("a studio"), None)

    check("rt studio", v2.normalise_room_type("a studio please"), "studio")
    check("rt flat", v2.normalise_room_type("one bed apartment"), "flat")
    check("rt share", v2.normalise_room_type("a room in a house share"), "room_in_shared")
    check("rt house", v2.normalise_room_type("a small house"), "house")
    check("rt none", v2.normalise_room_type("something near work"), None)

    check("area district", v2.normalise_area("N1"),
          {"granularity": "postcode_district", "value": "N1"})
    check("area district long", v2.normalise_area("SE15"),
          {"granularity": "postcode_district", "value": "SE15"})
    check("area sector", v2.normalise_area("N1 9"),
          {"granularity": "postcode_sector", "value": "N1 9"})
    check("area borough", v2.normalise_area("Camden"),
          {"granularity": "borough", "value": "Camden"})
    check("area adjacent", v2.normalise_area("near King's Cross")["granularity"], "adjacent")

    check("date iso", v2.normalise_move_in_date("2026-09-01"), "2026-09-01")
    check("date dm", v2.normalise_move_in_date("1 September"), "2026-09-01")
    check("date md", v2.normalise_move_in_date("September 15th"), "2026-09-15")
    check("date bare month", v2.normalise_move_in_date("in September"), "2026-09-30")

    check("feat furnished", v2.normalise_feature("must be furnished"), "furnished")
    check("feat unfurnished", v2.normalise_feature("unfurnished only"), "unfurnished")
    check("feat pet", v2.normalise_feature("pet-friendly"), "pet_friendly")
    check("feat garden", v2.normalise_feature("with a garden"), "garden")
    check("feat none", v2.normalise_feature("with a helipad"), None)

    check("commute mins", v2.normalise_commute_minutes("within 35 minutes"), 35)
    check("commute half hour", v2.normalise_commute_minutes("within half an hour"), 30)
    check("commute hour", v2.normalise_commute_minutes("under 1 hour"), 60)


# ========================================================================== #
# 2. deterministic predicates, one record at a time                           #
# ========================================================================== #
def test_predicates():
    P, F, U = v2.PASS, v2.FAIL, v2.UNKNOWN
    T = v2.TYPES

    b = {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=", "value": 1500}
    check("budget pass", T["all_results_satisfy"]["predicate"](L(price_raw=1400), b), P)
    check("budget edge", T["all_results_satisfy"]["predicate"](L(price_raw=1500), b), P)
    check("budget fail", T["all_results_satisfy"]["predicate"](L(price_raw=1501), b), F)
    check("budget alias", T["all_results_satisfy"]["predicate"](
        {"monthly_rent": 1400}, b), P)
    check("budget unknown", T["all_results_satisfy"]["predicate"]({"address": "x"}, b), U)

    for op, val, price, want in [("==", 2, 2, P), ("==", 2, 1, F), (">=", 2, 3, P),
                                 (">=", 2, 1, F), ("<=", 3, 3, P), ("<=", 3, 4, F),
                                 ("between", [2, 3], 2, P), ("between", [2, 3], 3, P),
                                 ("between", [2, 3], 4, F), ("between", [2, 3], 1, F)]:
        c = {"type": "bedroom_count_match", "op": op, "value": val}
        check(f"bed {op}{val} vs {price}",
              T["bedroom_count_match"]["predicate"](L(bedrooms=price), c), want)
    check("bed unknown", T["bedroom_count_match"]["predicate"](
        {"address": "x"}, {"type": "bedroom_count_match", "op": "==", "value": 2}), U)

    rt = {"type": "room_type_match", "value": "studio"}
    check("rt pass", T["room_type_match"]["predicate"](L(room_type_normalized="studio"), rt), P)
    check("rt fail", T["room_type_match"]["predicate"](L(room_type_normalized="flat"), rt), F)
    # v2 must NOT fall back to parsing property_type free text (v1's heuristic branch)
    check("rt no free-text fallback",
          T["room_type_match"]["predicate"]({"property_type": "studio apartment"}, rt), U)
    check("rt out-of-vocab is unknown",
          T["room_type_match"]["predicate"](L(room_type_normalized="penthouse"), rt), U)

    a_d = {"type": "area_match", "granularity": "postcode_district", "value": "N1"}
    check("area N1 pass", T["area_match"]["predicate"](L(postcode_district="N1"), a_d), P)
    check("area N1C is NOT N1", T["area_match"]["predicate"](L(postcode_district="N1C"), a_d), F)
    check("area district case", T["area_match"]["predicate"](L(postcode_district="n1"), a_d), P)
    a_s = {"type": "area_match", "granularity": "postcode_sector", "value": "N1 4"}
    check("area sector pass", T["area_match"]["predicate"](L(postcode_sector="N1 4"), a_s), P)
    check("area sector fail", T["area_match"]["predicate"](L(postcode_sector="N1 9"), a_s), F)
    a_b = {"type": "area_match", "granularity": "borough", "value": "Camden"}
    check("area borough fail", T["area_match"]["predicate"](L(), a_b), F)
    check("area borough pass",
          T["area_match"]["predicate"](L(area_normalized="Camden", borough="Camden"), a_b), P)
    a_c = {"type": "area_match", "granularity": "city", "value": "London"}
    check("area city pass (Camden in London via listing.city)",
          T["area_match"]["predicate"](L(area_normalized="Camden", city="London"), a_c), P)
    check("area city fail", T["area_match"]["predicate"](L(city="Manchester"), a_c), F)
    a_adj = {"type": "area_match", "granularity": "adjacent", "value": "near King's Cross",
             "accept": ["Islington", "Camden", "St Pancras"]}
    check("area adjacent pass", T["area_match"]["predicate"](L(area_normalized="Camden"), a_adj), P)
    check("area adjacent fail", T["area_match"]["predicate"](L(area_normalized="Croydon"), a_adj), F)
    check("area adjacent without frozen accept list is UNJUDGEABLE",
          T["area_match"]["predicate"](L(), {"type": "area_match", "granularity": "adjacent",
                                             "value": "near King's Cross"}), U)

    d = {"type": "move_in_date_satisfied", "op": "<=", "value": "2026-09-01"}
    check("date pass", T["move_in_date_satisfied"]["predicate"](
        L(available_from_normalized="2026-08-15"), d), P)
    check("date edge", T["move_in_date_satisfied"]["predicate"](
        L(available_from_normalized="2026-09-01"), d), P)
    check("date fail", T["move_in_date_satisfied"]["predicate"](
        L(available_from_normalized="2026-10-01"), d), F)
    check("date immediate", T["move_in_date_satisfied"]["predicate"](
        L(available_from_normalized="now"), d), P)
    for marker in ("Contact agent", "On application", "TBC", "", "unknown"):
        check(f"date '{marker}' -> unknown", T["move_in_date_satisfied"]["predicate"](
            L(available_from_normalized=marker), d), U)
    check("date missing field -> unknown",
          T["move_in_date_satisfied"]["predicate"]({"address": "x"}, d), U)

    f = {"type": "property_feature_present", "value": "pet_friendly"}
    check("feat pass", T["property_feature_present"]["predicate"](
        L(features=["furnished", "pet_friendly"]), f), P)
    check("feat fail", T["property_feature_present"]["predicate"](L(features=["garden"]), f), F)
    check("feat no structured list -> unknown", T["property_feature_present"]["predicate"](
        {"explanation": "Pets are welcome in this lovely flat"}, f), U)
    check("feat FORBIDDEN free-text inference", T["property_feature_present"]["predicate"](
        {"features": [], "description": "pet friendly"}, f), v2.FAIL)

    cm = {"type": "commute_leq_minutes", "dest": "Bank", "value": 35}
    check("commute pass", T["commute_leq_minutes"]["predicate"]({"duration_minutes": 30}, cm), P)
    check("commute edge", T["commute_leq_minutes"]["predicate"]({"duration_minutes": 35}, cm), P)
    check("commute fail", T["commute_leq_minutes"]["predicate"]({"duration_minutes": 41}, cm), F)
    check("commute unknown", T["commute_leq_minutes"]["predicate"]({"status": "error"}, cm), U)


# ========================================================================== #
# 3. surfaced-set semantics + end-to-end evaluate_constraint                   #
# ========================================================================== #
def _write_fixture(tmp: Path, name: str, recs, status="found"):
    payload = {"tool_name": "search_properties", "success": True, "error": None,
               "data": {"success": True, "status": status,
                        "total_found": len(recs), "data_source": "onthemarket_live",
                        "recommendations": recs}}
    (tmp / name).write_text(json.dumps(payload), encoding="utf-8")


def test_surfaced_and_evaluate():
    tmp = Path(tempfile.mkdtemp(prefix="v2fx_"))
    good = L(uid_token="Fernbrook Row", price_raw=1400)
    bad = L(uid_token="Halstow Mews", price_raw=1750,
            address="4 Halstow Mews, London N1 4RR")
    _write_fixture(tmp, "trap.json", [good, bad])
    case = {"case_id": "T1", "fixture": "trap.json"}
    con = {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=", "value": 1500}

    check("surfaced by uid token",
          [l["uid_token"] for l in v2.surfaced_listings(
              "I found 12 Fernbrook Row for you.", [good, bad])], ["Fernbrook Row"])
    check("surfaced by exact price",
          [l["uid_token"] for l in v2.surfaced_listings(
              "One option is £1,750/month.", [good, bad])], ["Halstow Mews"])
    check("surfaced none", v2.surfaced_listings("No matches at all.", [good, bad]), [])

    r = v2.evaluate_constraint(case, con, "Best fit: 12 Fernbrook Row at £1,400/month.", tmp)
    check("evaluate PASS", r["verdict"], v2.PASS)
    r = v2.evaluate_constraint(case, con, "Try 4 Halstow Mews, a great spot.", tmp)
    check("evaluate FAIL", r["verdict"], v2.FAIL)
    check("evaluate FAIL offender", r["offenders"], ["Halstow Mews"])
    r = v2.evaluate_constraint(case, con, "Nothing matched your budget.", tmp)
    check("evaluate NOT_SURFACED", r["verdict"], v2.NOT_SURFACED)

    _write_fixture(tmp, "empty.json", [], status="no_results")
    r = v2.evaluate_constraint({"case_id": "T2", "fixture": "empty.json"}, con, "None found.", tmp)
    check("evaluate NO_RESULT", r["verdict"], v2.NO_RESULT)

    # token fallback: no uid_token -> street parsed off the address
    anon = {"address": "9 Wraysbury Terrace, London SE15 3AA", "price_raw": 1250}
    check("token falls back to the street in the address",
          [l["address"] for l in v2.surfaced_listings(
              "9 Wraysbury Terrace is the pick.", [anon])], [anon["address"]])

    # commute is tool-result scoped and filtered by origin_uid, exactly like a listing
    cm = {"type": "commute_leq_minutes", "dest": "Bank", "value": 35}
    ok_leg = {"origin_uid": "Fernbrook Row", "duration_minutes": 28, "destination": "Bank"}
    bad_leg = {"origin_uid": "Halstow Mews", "duration_minutes": 47, "destination": "Bank"}
    (tmp / "commute.json").write_text(json.dumps({"results": [
        {"tool_name": "search_properties", "data": {"status": "found",
                                                    "recommendations": [good, bad]}},
        {"tool_name": "calculate_commute", "data": ok_leg},
        {"tool_name": "calculate_commute", "data": bad_leg}]}), encoding="utf-8")
    ccase = {"case_id": "T6", "fixture": "commute.json"}
    check("commute ignores the search payload (no duration field)",
          len(v2._records_for(cm, ccase, tmp)), 2)
    check("commute PASS on the compliant leg",
          v2.evaluate_constraint(ccase, cm, "12 Fernbrook Row: 28 min to Bank.", tmp)["verdict"],
          v2.PASS)
    check("commute FAIL when the answer offers the slow leg",
          v2.evaluate_constraint(ccase, cm, "Try 4 Halstow Mews.", tmp)["verdict"], v2.FAIL)
    check("commute verifiable (has both a PASS and a FAIL leg)",
          v2.constraint_is_satisfaction_verifiable(ccase, cm, tmp), True)

    # unknown branch: every surfaced listing is unknown on the field
    unk = L(uid_token="Quill Lane", available_from_normalized="Contact agent")
    _write_fixture(tmp, "unk.json", [unk])
    dcon = {"type": "move_in_date_satisfied", "op": "<=", "value": "2026-09-01"}
    r = v2.evaluate_constraint({"case_id": "T3", "fixture": "unk.json"}, dcon,
                               "Quill Lane looks good.", tmp)
    check("evaluate UNKNOWN", r["verdict"], v2.UNKNOWN)

    # satisfaction verifiability needs a violation trap
    check("verifiable with trap",
          v2.constraint_is_satisfaction_verifiable(case, con, tmp), True)
    _write_fixture(tmp, "allgood.json", [good, L(uid_token="Ashlin Walk", price_raw=1300)])
    check("NOT verifiable without a violating record",
          v2.constraint_is_satisfaction_verifiable(
              {"case_id": "T4", "fixture": "allgood.json"}, con, tmp), False)
    check("NOT verifiable with no fixture",
          v2.constraint_is_satisfaction_verifiable({"case_id": "T5"}, con, tmp), False)
    check("NOT verifiable when the field is missing",
          v2.constraint_is_satisfaction_verifiable(
              {"case_id": "T3", "fixture": "unk.json"}, dcon, tmp), False)

    # ONLY pass/fail enter the satisfaction denominator
    for verdict in (v2.UNKNOWN, v2.NO_RESULT, v2.NOT_SURFACED):
        check(f"{verdict} is behaviour not satisfaction",
              verdict in v2.SATISFACTION_VERDICTS, False)


# ========================================================================== #
# 4. registry invariants the gate depends on                                  #
# ========================================================================== #
def test_registry():
    check("all seven slots have a type", sorted(v2.SLOT_TYPE), sorted(v2.SLOT_MIN_COVERAGE))
    check("no slot is type-less", v2.MISSING_TYPES, ())
    check("room_type and bedroom_count are now SEPARATE types",
          v2.SLOT_TYPE["room_type"] != v2.SLOT_TYPE["bedroom_count"], True)
    check("bedroom_count_match exists", "bedroom_count_match" in v2.TYPES, True)
    for name, spec in v2.TYPES.items():
        for key in ("user_text_normalisation", "evidence", "predicate",
                    "completion_policy", "judge_evidence"):
            check(f"{name} freezes {key}", key in spec, True)
        for br in ("no_result", "unknown", "partial"):
            check(f"{name} completion_policy.{br}", bool(spec["completion_policy"].get(br)), True)
        check(f"{name} evidence field matches SLOT_EVIDENCE_FIELD",
              tuple(spec["evidence"]), v2.SLOT_EVIDENCE_FIELD[spec["slot"]])

    # every type in the v1 vocabulary must be classified — INCLUDED or EXCLUDED, never both
    v1_types = set(json.loads(
        (Path(__file__).resolve().parents[3] / "evaluation/benchmark/schema.json")
        .read_text(encoding="utf-8"))["properties"]["expected_constraints"]["items"]
        ["properties"]["type"]["enum"])
    audit = v2.audit_vocabulary(v1_types | set(v2.TYPES))
    check("no UNCLASSIFIED type", audit["UNCLASSIFIED"], [])
    check("INCLUDED and EXCLUDED are disjoint",
          v2.INCLUDED_TYPES & v2.EXCLUDED_INSTRUMENT_TYPES, frozenset())
    check("an unheard-of type IS flagged",
          v2.audit_vocabulary({"totally_new_type"})["UNCLASSIFIED"], ["totally_new_type"])

    for name, spec in v2.TYPES.items():
        check(f"{name} slot_of round-trips", v2.slot_of({"type": name}), spec["slot"])
    check("slot_of on an instrument type is None",
          v2.slot_of({"type": "must_call_tool"}), None)
    check("freeze_digest covers every type", sorted(v2.freeze_digest()["types"]),
          sorted(v2.TYPES))


# ========================================================================== #
# 5. explicitness (H6) and contradiction (H4) machinery                       #
# ========================================================================== #
def test_explicitness_and_contradictions():
    case = {"case_id": "X1",
            "user_query": "Looking for a 2-bed flat in N1, under £1,500 a month, "
                          "furnished, ready by 1 September, within 35 minutes of Bank.",
            "conversation_history": [],
            "expected_constraints": [
                {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=",
                 "value": 1500, "user_text": "under £1,500 a month"},
                {"type": "bedroom_count_match", "op": "==", "value": 2,
                 "user_text": "2-bed"},
                {"type": "room_type_match", "value": "flat", "user_text": "flat"},
                {"type": "area_match", "granularity": "postcode_district", "value": "N1",
                 "user_text": "N1"},
                {"type": "property_feature_present", "value": "furnished",
                 "user_text": "furnished"},
                {"type": "move_in_date_satisfied", "op": "<=", "value": "2026-09-01",
                 "user_text": "1 September"},
                {"type": "commute_leq_minutes", "dest": "Bank", "value": 35,
                 "user_text": "within 35 minutes"},
                {"type": "must_call_tool", "tool": "search_properties"},
            ]}
    check("H6 clean case", v2.explicitness_problems(case), [])

    bad = json.loads(json.dumps(case))
    bad["expected_constraints"][0]["value"] = 1200          # value no longer re-derivable
    check("H6 catches a value that the user text does not state",
          len(v2.explicitness_problems(bad)), 1)
    bad2 = json.loads(json.dumps(case))
    bad2["expected_constraints"][1].pop("user_text")
    check("H6 catches a missing user_text", len(v2.explicitness_problems(bad2)), 1)
    bad3 = json.loads(json.dumps(case))
    bad3["expected_constraints"][2]["user_text"] = "penthouse"   # not in the request text
    check("H6 catches a user_text that is not a verbatim span",
          len(v2.explicitness_problems(bad3)), 1)

    check("H4 clean", v2.contradictions(case["expected_constraints"]), [])
    check("H4 budget bounds", len(v2.contradictions([
        {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=", "value": 1200},
        {"type": "all_results_satisfy", "field": "monthly_rent", "op": ">=", "value": 1800}])), 1)
    check("H4 bedroom empty interval", len(v2.contradictions([
        {"type": "bedroom_count_match", "op": ">=", "value": 3},
        {"type": "bedroom_count_match", "op": "<=", "value": 1}])), 1)
    check("H4 bedroom compatible", v2.contradictions([
        {"type": "bedroom_count_match", "op": ">=", "value": 2},
        {"type": "bedroom_count_match", "op": "<=", "value": 3}]), [])
    check("H4 two room types", len(v2.contradictions([
        {"type": "room_type_match", "value": "flat"},
        {"type": "room_type_match", "value": "house"}])), 1)
    check("H4 furnished + unfurnished", len(v2.contradictions([
        {"type": "property_feature_present", "value": "furnished"},
        {"type": "property_feature_present", "value": "unfurnished"}])), 1)

    check("H2b rejects a numeric room_type (the v1 overload)",
          len(v2.arg_domain_problems({"type": "room_type_match", "value": "2-bed"})), 2)
    check("H2b rejects an out-of-vocab feature",
          len(v2.arg_domain_problems({"type": "property_feature_present", "value": "helipad"})), 1)
    check("H2b accepts a vocab feature",
          v2.arg_domain_problems({"type": "property_feature_present", "value": "garden"}), [])
    check("H2b rejects an unknown area granularity",
          len(v2.arg_domain_problems({"type": "area_match", "granularity": "county",
                                      "value": "Kent"})), 1)


def main() -> int:
    for fn in (test_normalisation, test_predicates, test_surfaced_and_evaluate, test_registry,
               test_explicitness_and_contradictions):
        fn()
    print(f"schema_version = {v2.SCHEMA_VERSION}")
    print(f"checks run     = {CHECKS[0]}")
    print(f"failures       = {len(FAILURES)}")
    for f in FAILURES:
        print("  FAIL " + f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
