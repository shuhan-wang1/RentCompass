"""Author the 110-case held-out benchmark (schema v2) and its frozen fixtures.

Run:  python evaluation/results/_harness/build_holdout_v2.py

Outputs (deterministic — same inputs, byte-identical outputs, no RNG anywhere):
  evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl      110 cases
  evaluation/benchmark/fixtures/ho2_*.json                    frozen tool evidence
  evaluation/benchmark/holdout_v2/MANIFEST.json               sha256 of every artefact

DESIGN CONSTRAINTS THIS FILE ENFORCES AT BUILD TIME (it raises rather than emitting a
set that would only fail later in the preflight):

  * stratum quota            retrieval_hard 35 / retrieval_soft 20 / calculation 20 /
                             memory 20 / clarify 15
  * per-slot SATISFACTION floors  budget 15 / bedroom_count 12 / room_type 8 /
                             commute 12 / area 12 / move_in_date 8 / property_feature 8
  * every retrieval_hard case carries a VIOLATION TRAP for each of its declared slots —
    the frozen evidence holds at least one record that satisfies the constraint and at
    least one that breaks it. Without that the case cannot tell an assistant that filters
    from one that does not, which is the hole v1's vacuous ``all_results_satisfy`` left.
  * street tokens unique inside a case, prices unique inside a case, and no listing price
    equal to any constraint value in that case — so the deterministic "which listing did
    the answer surface" match cannot be triggered by the user's own figures.

NOVELTY. Nothing here is a rewrite of the existing 98-case benchmark. That set is built
around UCL / Canary Wharf / Camden / Islington / Shoreditch / Bloomsbury / Chessington /
Clapham / Kensington / Whitechapel / Hackney / Stratford / Manchester Piccadilly. This one
uses a disjoint geography (Walthamstow, Peckham, Leyton, Tooting, Acton, Crouch End,
Bermondsey, New Cross, Wood Green, Catford, Streatham, Willesden Green, Forest Gate,
Colindale, Balham, Deptford, Harringay, Hendon …), disjoint commute destinations
(Liverpool Street, London Bridge, Old Street, Farringdon, Moorgate, Waterloo, Victoria,
Holborn, Aldgate, Blackfriars, Paddington, Barbican), fresh personas, fresh price points
and fresh listing addresses. Every case records its own ``novelty_note``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import constraint_schema_v2 as v2  # noqa: E402

OUT_DIR = REPO / "evaluation" / "benchmark" / "holdout_v2"
FIX_DIR = REPO / "evaluation" / "benchmark" / "fixtures"
CASES_PATH = OUT_DIR / "cases_holdout_v2.jsonl"

SCHEMA_VERSION = v2.SCHEMA_VERSION
AUTHORED_ON = "2026-08-05"

# --------------------------------------------------------------------------- #
# Geography — disjoint from the existing 98-case benchmark
# --------------------------------------------------------------------------- #
# area -> (borough, postcode_district, postcode_sector)
AREAS = {
    "Walthamstow":      ("Waltham Forest", "E17", "E17 4"),
    "Leyton":           ("Waltham Forest", "E10", "E10 5"),
    "Forest Gate":      ("Newham",         "E7",  "E7 8"),
    "Peckham":          ("Southwark",      "SE15", "SE15 5"),
    "New Cross":        ("Lewisham",       "SE14", "SE14 6"),
    "Deptford":         ("Lewisham",       "SE8",  "SE8 3"),
    "Catford":          ("Lewisham",       "SE6",  "SE6 4"),
    "Bermondsey":       ("Southwark",      "SE16", "SE16 4"),
    "Streatham":        ("Lambeth",        "SW16", "SW16 1"),
    "Tooting":          ("Wandsworth",     "SW17", "SW17 0"),
    "Balham":           ("Wandsworth",     "SW12", "SW12 9"),
    "Acton":            ("Ealing",         "W3",   "W3 6"),
    "Willesden Green":  ("Brent",          "NW2",  "NW2 5"),
    "Colindale":        ("Barnet",         "NW9",  "NW9 5"),
    "Hendon":           ("Barnet",         "NW4",  "NW4 3"),
    "Crouch End":       ("Haringey",       "N8",   "N8 9"),
    "Harringay":        ("Haringey",       "N4",   "N4 1"),
    "Wood Green":       ("Haringey",       "N22",  "N22 6"),
}
CITY = "London"

DESTS = ["Liverpool Street", "London Bridge", "Old Street", "Farringdon", "Moorgate",
         "Waterloo", "Victoria", "Holborn", "Aldgate", "Blackfriars", "Paddington",
         "Barbican"]

_PRE = ["Fernbrook", "Halstow", "Wraysbury", "Quillon", "Ashlin", "Marchcroft", "Denbury",
        "Osterlyn", "Pentworth", "Ravensmere", "Sowerby", "Thackray", "Ulverton",
        "Vandermeer", "Wexcombe", "Yarnfield", "Zephyrn", "Brackendale", "Calthorpe",
        "Drayfield", "Ellesbury", "Foxhollow", "Garrowby", "Hazelbourne", "Inglewhite",
        "Jarrowfield", "Kelsterne", "Lambourne", "Mortlake", "Netherby", "Ockendale",
        "Pyrford", "Quenby", "Rushmoor", "Stanbrook", "Tarleton", "Uffington", "Verwood",
        "Whitmarsh", "Xanthe", "Yealand", "Zennorby", "Ambrose", "Beaumaris", "Carbery",
        "Dunsfold", "Eastnor", "Fairholme", "Glaisdale", "Hartsmere", "Ilmington",
        "Jevington", "Kirkstall", "Longmynd", "Merrivale", "Northiam", "Oakhanger",
        "Pilgrims", "Quarrendon", "Redbourne", "Sandhurst", "Thornbury", "Upwaltham",
        "Vellacott", "Wandlebury", "Yarrowby", "Ashmansworth", "Bishopstone",
        "Chalvington", "Dunmowe"]
_SUF = ["Row", "Mews", "Terrace", "Walk", "Gardens", "Rise", "Court", "Lane", "Green",
        "Wharf", "Crescent", "Yard"]
STREETS = [f"{p} {s}" for s in _SUF for p in _PRE]        # 840 unique names


class _StreetPool:
    def __init__(self):
        self.i = 0

    def take(self) -> str:
        s = STREETS[self.i]
        self.i += 1
        return s


POOL = _StreetPool()

FEATURE_BLURB = {
    "furnished": "Comes fully furnished.",
    "unfurnished": "Let unfurnished.",
    "part_furnished": "Part furnished.",
    "pet_friendly": "Landlord accepts pets.",
    "garden": "Private rear garden.",
    "parking": "Allocated parking space.",
    "balcony": "Balcony off the living room.",
    "lift": "Lift access to all floors.",
    "en_suite": "En-suite shower room.",
    "bills_included": "Bills included in the rent.",
    "washing_machine": "Washing machine installed.",
    "dishwasher": "Dishwasher installed.",
    "wheelchair_accessible": "Step-free access throughout.",
    "student_friendly": "Popular with students.",
}
RT_LABEL = {"studio": "Studio", "flat": "Flat", "house": "House",
            "room_in_shared": "Room in shared house", "maisonette": "Maisonette",
            "bungalow": "Bungalow"}


_MONTH_NAME = ["", "January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def _pcode_full(sector: str, n: int) -> str:
    return f"{sector}{'ABDEFGHJLNPQRSTUWXYZ'[n % 20]}{'ABDEFGHJLNPQRSTUWXYZ'[(n * 7) % 20]}"


def listing(rank, *, street, number, area, price, beds, rtype, avail_norm,
            features, extra_blurb=""):
    borough, pcd, pcs = AREAS[area]
    an = str(avail_norm).lower()
    if an == "now":
        avail_human = "Now"
    elif an in ("contact agent", "on application", "tbc", ""):
        avail_human = "Contact agent"
    else:
        y, m, d = (int(x) for x in an.split("-"))
        avail_human = f"{d} {_MONTH_NAME[m]} {y}"
    blurb = " ".join([FEATURE_BLURB[f] for f in features] + ([extra_blurb] if extra_blurb else []))
    return {
        "rank": rank,
        "address": f"{number} {street}, {area}, London {_pcode_full(pcs, rank + number)}",
        "price": f"£{price:,}/month",
        "price_raw": price,
        "score": 92 - 3 * rank,
        "property_type": RT_LABEL[rtype] + ("" if rtype == "studio" else f" · {beds} bed"),
        "bedrooms": beds,
        "room_type_normalized": rtype,
        "area_normalized": area,
        "borough": borough,
        "city": CITY,
        "postcode_district": pcd,
        "postcode_sector": pcs,
        "available_from": avail_human,
        "available_from_normalized": avail_norm,
        "features": list(features),
        "match_type": "candidate",
        "source": "onthemarket_live",
        "url": f"https://www.onthemarket.com/details/{street.lower().replace(' ', '-')}-{number}/",
        "explanation": blurb,
    }


def search_payload(recs, *, area, criteria, status="found", message=None):
    data = {"success": True, "status": status, "total_found": len(recs),
            "data_source": "onthemarket_live", "possibly_outdated": False,
            "recommendations": recs,
            "summary": (message or f"Found {len(recs)} options in {area}."),
            "search_criteria": criteria}
    if status == "no_results":
        data["message"] = message or (
            f"I could not find any listings in {area} matching those criteria right now.")
    return {"tool_name": "search_properties", "success": True, "error": None, "data": data}


def commute_payload(*, origin_uid, origin_address, dest, minutes):
    return {"tool_name": "calculate_commute", "success": True, "error": None,
            "data": {"origin_uid": origin_uid,
                     "from_address": origin_address,
                     "to_address": f"{dest}, London",
                     "mode": "transit",
                     "duration_minutes": minutes,
                     "duration_category": ("Short (<20 min)" if minutes < 20 else
                                           "Medium (20-45 min)" if minutes <= 45 else
                                           "Long (>45 min)"),
                     "route_summary": f"Direct service to {dest}",
                     "route_source": "tfl"}}


def write_fixture(name: str, records, description: str) -> str:
    payload = ({"description": description, "results": records} if len(records) != 1
               else dict(records[0], description=description))
    (FIX_DIR / name).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                                encoding="utf-8")
    return name


# --------------------------------------------------------------------------- #
# Case assembly
# --------------------------------------------------------------------------- #
CASES: list[dict] = []
CATEGORY_OF = {"retrieval_hard": "E_multi_constraint", "retrieval_soft": "A_retrieval",
               "calculation": "B_money", "memory": "G_memory", "clarify": "A_retrieval"}


def add_case(*, cid, task_category, user_query, constraints, correct_completion,
             failure_conditions, allowed_evidence_sources, novelty_note, notes,
             expected_tools=(), forbidden_tools=(), fixture=None,
             reference_calculations=None, expected_route=None, history=(),
             grounding_sources=()):
    slots = sorted({s for s in (v2.slot_of(c) for c in constraints) if s})
    case = {
        "case_id": cid,
        "schema_version": SCHEMA_VERSION,
        "task_category": task_category,
        "category": CATEGORY_OF[task_category],
        "authored_on": AUTHORED_ON,
        "user_id": f"u_ho2_{cid.split('-')[1]}",
        "user_query": user_query,
        "conversation_history": [dict(t) for t in history],
        "expected_tools": list(expected_tools),
        "forbidden_tools": list(forbidden_tools),
        "expected_constraints": constraints,
        "hard_constraint_slots": slots,
        "correct_completion": correct_completion,
        "failure_conditions": list(failure_conditions),
        "allowed_evidence_sources": list(allowed_evidence_sources),
        "expected_grounding_sources": list(grounding_sources) or list(allowed_evidence_sources),
        "reference_calculations": reference_calculations,
        "novelty_note": novelty_note,
        "notes": notes,
    }
    if expected_route:
        case["expected_route"] = expected_route
    if fixture:
        case["fixture"] = fixture
    CASES.append(case)
    return case


# --------------------------------------------------------------------------- #
# retrieval_hard — 35 cases, each with a violation trap per declared slot
# --------------------------------------------------------------------------- #
EXCLUSIONS = [
    "Only show me ones that actually fit — skip anything that misses any of that.",
    "Please leave out anything that does not meet every one of those.",
    "Do not include options that break any of those conditions.",
    "If an option misses any of that, leave it out of your answer.",
    "I only want the ones that tick every box — drop the rest.",
    "Skip anything that fails one of those conditions, please.",
    "Just the ones that satisfy all of it; ignore the others.",
    "Anything that does not meet all of those should not be in your list.",
]
OPENINGS = ["Hi — I'm looking for", "Can you find me", "I need", "Help me find",
            "I'm after", "Looking for", "Trying to find", "Please search for",
            "I'd like", "Could you dig out"]

def _date_phrase(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{d} {_MONTH_NAME[m]}"


def _plus_days(iso: str, days: int) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


# (slots, area, dest, budget, beds_spec, room_type, move_in, feature, unknown_date)
HARD_SPECS = [
    # 1-13: budget + bedroom_count
    (["budget", "bedroom_count"], "Walthamstow", None, 1700, ("==", 2), "flat", None, None, False),
    (["budget", "bedroom_count"], "Leyton", None, 1450, ("==", 1), "flat", None, None, False),
    (["budget", "bedroom_count"], "Peckham", None, 2100, ("==", 3), "house", None, None, False),
    (["budget", "bedroom_count"], "Tooting", None, 1850, (">=", 2), "flat", None, None, False),
    (["budget", "bedroom_count", "move_in_date"], "Catford", None, 1300, ("==", 1), "flat",
     "2026-10-01", None, True),
    (["budget", "bedroom_count"], "Acton", None, 1950, ("between", [2, 3]), "flat", None, None, False),
    (["budget", "bedroom_count"], "Crouch End", None, 2250, ("==", 2), "flat", None, None, False),
    (["budget", "bedroom_count"], "Bermondsey", None, 2400, ("==", 2), "flat", None, None, False),
    (["budget", "bedroom_count"], "Wood Green", None, 1550, ("==", 1), "flat", None, None, False),
    (["budget", "bedroom_count", "move_in_date"], "Hendon", None, 1750, ("<=", 2), "flat",
     "2026-11-01", None, True),
    (["budget", "bedroom_count"], "New Cross", None, 1650, ("==", 2), "house", None, None, False),
    (["budget", "bedroom_count"], "Streatham", None, 1500, ("==", 1), "flat", None, None, False),
    (["budget", "bedroom_count"], "Balham", None, 2050, (">=", 2), "flat", None, None, False),
    # 14-16: budget + room_type
    (["budget", "room_type"], "Deptford", None, 1350, None, "studio", None, None, False),
    (["budget", "room_type", "move_in_date"], "Forest Gate", None, 1150, None,
     "room_in_shared", "2026-09-15", None, True),
    (["budget", "room_type"], "Harringay", None, 2300, None, "house", None, None, False),
    # 17-19: room_type + commute
    (["room_type", "commute"], "Willesden Green", "Baker Street", None, None, "studio", None, None, False),
    (["room_type", "commute"], "Colindale", "Moorgate", None, None, "flat", None, None, False),
    (["room_type", "commute", "move_in_date"], "Leyton", "Liverpool Street", None, None,
     "room_in_shared", "2026-10-15", None, True),
    # 20-22: room_type + area
    (["room_type", "area"], "Walthamstow", None, None, None, "maisonette", None, None, False),
    (["room_type", "area"], "Peckham", None, None, None, "studio", None, None, False),
    (["room_type", "area", "move_in_date"], "Acton", None, None, None, "house",
     "2026-12-01", None, True),
    # 23-26: commute + area + move_in_date (+feature on 26)
    (["commute", "area", "move_in_date"], "Tooting", "Waterloo", None, None, None, "2026-09-28", None, False),
    (["commute", "area", "move_in_date"], "Bermondsey", "London Bridge", None, None, None, "2026-10-05", None, False),
    (["commute", "area", "move_in_date"], "Catford", "Blackfriars", None, None, None, "2026-11-15", None, False),
    (["commute", "area", "move_in_date", "property_feature"], "Streatham", "Victoria",
     None, None, None, "2026-10-20", "parking", False),
    # 27-29: commute + area + property_feature
    (["commute", "area", "property_feature"], "New Cross", "Old Street", None, None, None, None, "garden", False),
    (["commute", "area", "property_feature"], "Harringay", "Farringdon", None, None, None, None, "furnished", False),
    (["commute", "area", "property_feature"], "Wood Green", "Holborn", None, None, None, None, "pet_friendly", False),
    # 30-31: commute + area + move_in_date
    (["commute", "area", "move_in_date"], "Balham", "Aldgate", None, None, None, "2026-09-20", None, False),
    (["commute", "area", "move_in_date"], "Colindale", "Paddington", None, None, None, "2026-12-15", None, False),
    # 32: commute + area + property_feature
    (["commute", "area", "property_feature"], "Deptford", "Barbican", None, None, None, None, "balcony", False),
    # 33-35: commute + move_in_date + property_feature
    (["commute", "move_in_date", "property_feature"], "Hendon", "Baker Street", None, None,
     None, "2026-10-10", "lift", False),
    (["commute", "move_in_date", "property_feature"], "Forest Gate", "Liverpool Street",
     None, None, None, "2026-11-05", "bills_included", False),
    (["commute", "move_in_date", "property_feature"], "Crouch End", "Moorgate", None, None,
     None, "2026-09-25", "washing_machine", False),
]
assert len(HARD_SPECS) == 35


def _bed_phrase(spec):
    op, val = spec
    if op == "==":
        return f"{val}-bed"
    if op == ">=":
        return f"at least {val} bedrooms"
    if op == "<=":
        return f"no more than {val} bedrooms"
    return f"{val[0]} to {val[1]} bedrooms"


def _alt_area(area):
    keys = [k for k in AREAS if k != area]
    return keys[(len(area) * 5) % len(keys)]


def _alt_rtype(rt):
    order = [x for x in v2.ROOM_TYPE_VOCAB if x != rt]
    return order[len(rt) % len(order)]


def build_retrieval_hard():
    for i, (slots, area, dest, budget, beds, rtype, movein, feature, unknown_date) in \
            enumerate(HARD_SPECS, start=1):
        cid = f"HO2-{i:03d}"
        cons, phrases = [], []
        base_beds = beds[1] if beds and beds[0] != "between" else (
            beds[1][0] if beds else 1)
        base_rtype = rtype or "flat"
        base_features = [feature] if feature else []
        base_avail = "contact agent" if unknown_date else (movein or "now")

        # ---- the noun phrase: bedroom count and/or room-type label ---- #
        if beds:
            cons.append({"type": "bedroom_count_match", "op": beds[0], "value": beds[1],
                         "user_text": _bed_phrase(beds)})
        if rtype:
            span = {"studio": "studio", "flat": "flat", "house": "house",
                    "maisonette": "maisonette", "bungalow": "bungalow",
                    "room_in_shared": "house share"}[rtype]
            cons.append({"type": "room_type_match", "value": rtype, "user_text": span})
        if rtype == "room_in_shared":
            head = "a room in a house share"
        elif beds and rtype and beds[0] == "==":
            head = f"a {_bed_phrase(beds)} {RT_LABEL[rtype].lower()}"
        elif beds and rtype:
            head = f"a {RT_LABEL[rtype].lower()} with {_bed_phrase(beds)}"
        elif beds:
            head = f"a place with {_bed_phrase(beds)}"
        elif rtype:
            head = f"a {RT_LABEL[rtype].lower()}"
        else:
            head = "somewhere to rent"
        head += f" in {area}"
        # Every one of these requests names an area out loud, so area is ALWAYS a stated
        # hard constraint here — declaring it on only some cases would leave the others
        # with a condition the user uttered and the grader ignored.
        cons.append({"type": "area_match", "granularity": "borough", "value": area,
                     "user_text": area})
        if budget:
            span = f"under £{budget:,} a month"
            head += f" {span}"
            cons.append({"type": "all_results_satisfy", "field": "monthly_rent",
                         "op": "<=", "value": budget, "user_text": span})
        phrases.append(head)
        if dest:
            span = f"within {25 + (i % 4) * 5} minutes"
            phrases.append(f"{span} of {dest}")
            cons.append({"type": "commute_leq_minutes", "dest": dest,
                         "value": 25 + (i % 4) * 5, "user_text": span})
        if movein:
            span = _date_phrase(movein)
            phrases.append(f"ready to move into by {span}")
            cons.append({"type": "move_in_date_satisfied", "op": "<=", "value": movein,
                         "user_text": span})
        if feature:
            span = {"garden": "a garden", "parking": "parking", "furnished": "furnished",
                    "pet_friendly": "pet-friendly", "balcony": "a balcony", "lift": "a lift",
                    "bills_included": "bills included",
                    "washing_machine": "a washing machine"}[feature]
            phrases.append(f"with {span}" if not span.startswith(("furnished", "pet"))
                           else span)
            cons.append({"type": "property_feature_present", "value": feature,
                         "user_text": span})

        query = (f"{OPENINGS[i % len(OPENINGS)]} {', '.join(phrases)}. "
                 f"{EXCLUSIONS[i % len(EXCLUSIONS)]}")

        # The declared slots are whatever the REQUEST actually states — never a planning
        # list that drifts from the wording. A condition the user utters is a constraint.
        _ORDER = ["budget", "bedroom_count", "room_type", "commute", "area",
                  "move_in_date", "property_feature"]
        present = {v2.slot_of(c) for c in cons}
        slots = [s for s in _ORDER if s in present]

        # ---- frozen evidence: one compliant option + one violator per declared slot ----
        recs, commutes = [], []
        rank = 1
        prices_used = set()
        con_values = {c.get("value") for c in cons if isinstance(c.get("value"), int)}

        def _price(base):
            p = base
            while p in prices_used or p in con_values:
                p += 25
            prices_used.add(p)
            return p

        good_street = POOL.take()
        good_price = _price((budget - 150) if budget else 1400 + 25 * i)
        good = listing(rank, street=good_street, number=10 + i, area=area,
                       price=good_price, beds=base_beds, rtype=base_rtype,
                       avail_norm=base_avail, features=base_features or ["furnished"])
        recs.append(good)
        rank += 1
        if dest:
            commutes.append(commute_payload(
                origin_uid=good_street, origin_address=good["address"], dest=dest,
                minutes=(25 + (i % 4) * 5) - 6))

        for slot in slots:
            st = POOL.take()
            n = 10 + i + rank
            price = _price(good_price + 40 * rank)
            beds_v, rt_v, ar_v, av_v, ft_v = (base_beds, base_rtype, area, base_avail,
                                              list(base_features or ["furnished"]))
            if slot == "budget":
                price = _price(budget + 200)
            elif slot == "bedroom_count":
                op, val = beds
                if op == "between":
                    beds_v = val[1] + 1
                elif op == ">=":
                    beds_v = val - 1
                else:                       # "==" and "<=" both fail by going one over
                    beds_v = val + 1
                beds_v = max(0, beds_v)
            elif slot == "room_type":
                rt_v = _alt_rtype(rtype)
            elif slot == "area":
                ar_v = _alt_area(area)
            elif slot == "move_in_date":
                if unknown_date:
                    continue                     # unknown branch: no date violator exists
                av_v = _plus_days(movein, 45)
            elif slot == "property_feature":
                ft_v = [f for f in ["unfurnished", "washing_machine"] if f != feature][:1]
            elif slot == "commute":
                pass
            bad = listing(rank, street=st, number=n, area=ar_v, price=price,
                          beds=beds_v, rtype=rt_v, avail_norm=av_v, features=ft_v)
            recs.append(bad)
            rank += 1
            if dest:
                over = (25 + (i % 4) * 5) + (10 if slot == "commute" else 4)
                commutes.append(commute_payload(
                    origin_uid=st, origin_address=bad["address"], dest=dest, minutes=over))

        criteria = {"area": area, **({"max_budget": budget} if budget else {}),
                    **({"room_type": rtype} if rtype else {}),
                    **({"bedrooms": beds[1]} if beds else {})}
        records = [search_payload(recs, area=area, criteria=criteria)] + commutes
        fx = write_fixture(f"ho2_{i:03d}_hard.json", records,
                           f"{cid}: frozen search evidence for {area}. Alongside one fully "
                           f"compliant option the result set carries one option that breaks "
                           f"exactly one of {slots} (including a nearby-area result), which "
                           f"is what a real search returns and what the assistant is "
                           f"supposed to filter."
                           + (f" Commute legs to {dest} are measured per property and keyed "
                              f"by origin_uid." if dest else ""))

        def _cond(s):
            if s == "budget":
                return f"monthly rent <= GBP {budget:,}"
            if s == "bedroom_count":
                return f"bedrooms {beds[0]} {beds[1]}"
            if s == "room_type":
                return f"property type = {rtype}"
            if s == "commute":
                return f"commute to {dest} <= {25 + (i % 4) * 5} min"
            if s == "area":
                return f"located in {area}"
            if s == "move_in_date":
                return (f"available on or before {movein}" if movein
                        else "availability date requested by the user")
            return f"has {feature}"

        cond_txt = "; ".join(_cond(s) for s in slots)
        completion = (
            f"Present ONLY the frozen listings that satisfy every stated condition "
            f"({cond_txt}). The evidence deliberately contains options that break at least "
            f"one condition; the user asked for those to be left out, so a correct answer "
            f"must not put them forward as matches. Quote prices, bedroom counts, areas, "
            f"availability and commute times only from the structured evidence fields.")
        if unknown_date:
            completion += (" The availability date on these listings is not published "
                           "('Contact agent'): say the move-in date has to be confirmed "
                           "with the agent. Do NOT claim the date is met and do NOT reject "
                           "the listings as unavailable.")
        add_case(
            cid=cid, task_category="retrieval_hard", user_query=query, constraints=cons,
            correct_completion=completion,
            failure_conditions=[
                "Presents a listing that breaks one of the stated conditions as if it were a match.",
                "Invents an address, price, bedroom count, availability date or commute time "
                "that is not in the frozen evidence.",
                "Claims a condition is satisfied without the corresponding structured field.",
            ] + (["Treats the unpublished availability date as either satisfied or as a rejection."]
                 if unknown_date else []),
            allowed_evidence_sources=[
                "search_properties -> data.recommendations[].{price_raw,bedrooms,"
                "room_type_normalized,area_normalized,borough,city,postcode_district,"
                "postcode_sector,available_from_normalized,features}",
                "the user's own stated conditions",
            ] + ([f"calculate_commute -> data.duration_minutes (per origin_uid)"] if dest else []),
            novelty_note=(f"New request written on {AUTHORED_ON} for the held-out set. Area "
                          f"{area}"
                          + (f", destination {dest}" if dest else "")
                          + ", personas, street names, price points and availability dates "
                            "do not occur in evaluation/benchmark/cases.jsonl."),
            notes=("Frozen fixture carries one fully compliant option plus one violator per "
                   "declared slot, so every declared slot has both a PASS and a FAIL record "
                   "(schema v2 violation-trap rule). Prices are unique inside the case and "
                   "never equal a constraint value."),
            expected_tools=["search_properties"] + (["calculate_commute"] if dest else []),
            forbidden_tools=[],
            fixture=fx, expected_route="search_properties",
            grounding_sources=["OnTheMarket listings (search_properties)"]
            + (["TfL journey time (calculate_commute)"] if dest else []),
        )


# --------------------------------------------------------------------------- #
# retrieval_soft — 20 cases: 12 with no hard constraint, 8 no-result / unknown
# --------------------------------------------------------------------------- #
SOFT_OPEN = [
    ("Walthamstow", "I've just been offered a job near Liverpool Street and I don't know "
                    "the area at all. What sort of places come up in Walthamstow at the moment?"),
    ("Peckham", "We're thinking about moving to Peckham next year. Could you show me what "
                "is on the rental market there right now so we get a feel for it?"),
    ("Tooting", "My partner and I are browsing rather than committing. What is currently "
                "listed in Tooting?"),
    ("Acton", "Give me a general picture of what is available to rent in Acton at the moment."),
    ("Crouch End", "What kind of rental stock does Crouch End have? Just curious what comes up."),
    ("Bermondsey", "I keep hearing Bermondsey is good value. What is listed there right now?"),
    ("Catford", "Show me what is on the market in Catford — no particular requirements yet."),
    ("Wood Green", "We might relocate to Wood Green. What is available there at present?"),
    ("Balham", "Could you pull up whatever is currently listed in Balham for me to look through?"),
    ("Deptford", "I'd like to see the current rental listings in Deptford, please."),
    ("Harringay", "What is renting in Harringay these days? I'm at the browsing stage."),
    ("Colindale", "Just exploring — what rental properties are showing in Colindale?"),
]
SOFT_EMPTY = [
    ("Streatham", "Any 4-bed houses in Streatham under £1,200 a month?", 1200, ("==", 4), "house"),
    ("New Cross", "Is there anything with a garden in New Cross for under £700 a month?", 700, None, None),
    ("Hendon", "I need a 3-bed flat in Hendon under £900 a month — anything?", 900, ("==", 3), "flat"),
    ("Leyton", "Looking for a studio in Leyton at under £500 a month. What have you got?", 500, None, "studio"),
    ("Forest Gate", "Are there any 5-bed houses in Forest Gate under £1,500 a month?", 1500, ("==", 5), "house"),
    ("Willesden Green", "Anything in Willesden Green under £600 a month, any size?", 600, None, None),
    ("Bermondsey", "Can you find a 2-bed maisonette in Bermondsey under £800 a month?", 800, ("==", 2), "maisonette"),
    ("Wood Green", "I want a bungalow in Wood Green under £1,000 a month. Anything listed?", 1000, None, "bungalow"),
]


def build_retrieval_soft():
    n = 35
    for j, (area, query) in enumerate(SOFT_OPEN, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        recs = []
        for k in range(3):
            st = POOL.take()
            recs.append(listing(k + 1, street=st, number=3 + 7 * k + j, area=area,
                                price=1300 + 175 * k + 15 * j, beds=k, rtype=
                                ["studio", "flat", "flat"][k],
                                avail_norm=["now", "2026-10-01", "contact agent"][k],
                                features=[["furnished"], ["garden"], ["parking"]][k]))
        fx = write_fixture(f"ho2_{n:03d}_soft.json",
                           [search_payload(recs, area=area, criteria={"area": area})],
                           f"{cid}: frozen open-ended search evidence for {area}.")
        add_case(
            cid=cid, task_category="retrieval_soft", user_query=query,
            # The area IS stated, so it is declared — but every frozen listing is in that
            # area, so there is no violation trap and the constraint is 'trivial': it
            # cannot enter a satisfaction denominator. Declaring it anyway keeps the
            # judge's view of "what did the user actually ask for" honest.
            constraints=[{"type": "area_match", "granularity": "borough", "value": area,
                          "user_text": area}],
            correct_completion=(
                "The user stated no hard condition, so nothing has to be filtered. A correct "
                "answer summarises the three frozen listings using only their structured "
                "fields (price, bedrooms, type, area, availability, features) and may ask "
                "what the user's budget or must-haves are. It must not invent listings, "
                "prices or market statistics, and must not assert an availability date for "
                "the listing whose availability reads 'Contact agent'."),
            failure_conditions=[
                "Invents listings, prices or market averages that are not in the evidence.",
                "States a definite availability date for the listing marked 'Contact agent'.",
                "Claims a condition was applied that the user never stated.",
            ],
            allowed_evidence_sources=[
                "search_properties -> data.recommendations[] structured fields",
                "the user's own words"],
            novelty_note=(f"New open-ended browse request for {area}; the existing 98-case "
                          f"set has no un-constrained browse turn for this area."),
            notes="No hard constraint by design — this stratum covers the no-hard-constraint path.",
            expected_tools=["search_properties"], fixture=fx,
            expected_route="search_properties",
            grounding_sources=["OnTheMarket listings (search_properties)"])

    for j, (area, query, budget, beds, rtype) in enumerate(SOFT_EMPTY, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        cons = [{"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=",
                 "value": budget, "user_text": f"under £{budget:,} a month"},
                {"type": "area_match", "granularity": "borough", "value": area,
                 "user_text": area}]
        if beds:
            cons.append({"type": "bedroom_count_match", "op": beds[0], "value": beds[1],
                         "user_text": f"{beds[1]}-bed"})
        if rtype:
            span = {"house": "house", "studio": "studio", "flat": "flat",
                    "maisonette": "maisonette", "bungalow": "bungalow"}[rtype]
            cons.append({"type": "room_type_match", "value": rtype, "user_text": span})
        if "garden" in query:
            cons.append({"type": "property_feature_present", "value": "garden",
                         "user_text": "a garden"})
        fx = write_fixture(
            f"ho2_{n:03d}_empty.json",
            [search_payload([], area=area,
                            criteria={"area": area, "max_budget": budget},
                            status="no_results")],
            f"{cid}: frozen NO-RESULT search evidence for {area} at £{budget}/month.")
        add_case(
            cid=cid, task_category="retrieval_soft", user_query=query, constraints=cons,
            correct_completion=(
                "The frozen search returns no listings. The correct answer states honestly "
                "that no match was found at that budget in that area, does not claim the "
                "constraints were satisfied, invents no listing or price, and may suggest "
                "widening the budget or the area. Saying 'none found' is the success "
                "condition here, not a failure."),
            failure_conditions=[
                "Invents listings or prices to fill the empty result.",
                "Claims properties were found when the tool returned none.",
                "Asserts the budget or bedroom condition was met when there is nothing to meet it with.",
            ],
            allowed_evidence_sources=["search_properties -> data.status == 'no_results'",
                                      "the user's own stated conditions"],
            novelty_note=(f"New no-result probe for {area} at an unrealistically low budget; "
                          f"no equivalent area/budget pair exists in the 98-case set."),
            notes=("Behaviour-coverage case: the constraints exist but only the no-result "
                   "branch is reachable, so it never enters a satisfaction denominator."),
            expected_tools=["search_properties"], fixture=fx,
            expected_route="search_properties",
            grounding_sources=["OnTheMarket listings (search_properties)"])


# --------------------------------------------------------------------------- #
# calculation — 20 cases, pure arithmetic, frozen formulas, no tools
# --------------------------------------------------------------------------- #
def _rc(name, formula, result, unit):
    return {name: {"formula": formula, "result": round(result, 2), "unit": unit}}


CALC_SPECS = [
    ("w2m", 275), ("w2m", 340), ("w2m", 395), ("w2m", 425), ("w2m", 480),
    ("m2w", 1290), ("m2w", 1675), ("m2w", 2480),
    ("dep5", 1420), ("dep5", 1580), ("dep5", 1875), ("dep5", 2100),
    ("dep6", 4300), ("dep6", 4750),
    ("movein", 1350), ("movein", 1690), ("movein", 2250),
    ("annual", 1180), ("annual", 1925), ("annual", 3050),
]


def build_calculation():
    n = 55
    for j, (kind, amount) in enumerate(CALC_SPECS, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        if kind == "w2m":
            monthly = amount * 52 / 12
            q = [
                f"A place I like is advertised at £{amount} per week. What is that as a "
                f"monthly rent? Show the arithmetic.",
                f"The agent quoted me £{amount} pw. My landlord reference form wants a "
                f"monthly figure — what should I put?",
                f"Everything round here is priced weekly. £{amount} a week is how much a "
                f"month?",
                f"I budget monthly but this one is listed at £{amount} per week. Convert "
                f"it for me and show your working.",
                f"Quick sanity check: is £{amount} a week more or less than £2,000 a "
                f"month? Give me the monthly equivalent.",
            ][j % 5]
            rc = _rc("monthly_rent", f"{amount} * 52 / 12", monthly, "GBP/month")
            comp = (f"Converts weekly to monthly with the frozen formula "
                    f"weekly*52/12 and states £{monthly:,.2f} per month (rounding to the "
                    f"nearest penny or pound is fine). Must not use *4 or *4.33.")
            fails = ["Uses *4 or *4.33 instead of *52/12.",
                     f"States a monthly figure that is not {monthly:.2f} within rounding."]
        elif kind == "m2w":
            weekly = amount * 12 / 52
            q = [
                f"The listing says £{amount:,} pcm. My budget spreadsheet is weekly — what "
                f"is the weekly equivalent?",
                f"My flatmate works out everything by the week. £{amount:,} a month comes "
                f"to what weekly?",
                f"Rent is £{amount:,} monthly. I need the per-week number for a form.",
            ][j % 3]
            rc = _rc("weekly_rent", f"{amount} * 12 / 52", weekly, "GBP/week")
            comp = (f"Converts monthly to weekly with monthly*12/52 and states "
                    f"£{weekly:,.2f} per week.")
            fails = ["Divides by 4 or 4.33 instead of using *12/52.",
                     f"States a weekly figure that is not {weekly:.2f} within rounding."]
        elif kind in ("dep5", "dep6"):
            weekly = amount * 12 / 52
            weeks = 5 if kind == "dep5" else 6
            dep = weekly * weeks
            annual = amount * 12
            q = [
                f"Rent is £{amount:,} a month. How much deposit can the landlord legally "
                f"ask for under the Tenant Fees Act?",
                f"The agent is asking for a deposit on a £{amount:,} pcm flat. What is the "
                f"legal maximum they can hold?",
                f"On £{amount:,} a month, what is the biggest deposit I can be asked for, "
                f"and on what basis?",
            ][j % 3]
            rc = {**_rc("weekly_rent", f"{amount} * 12 / 52", weekly, "GBP/week"),
                  **_rc("annual_rent", f"{amount} * 12", annual, "GBP/year"),
                  **_rc("deposit", f"({amount} * 12 / 52) * {weeks}", dep, "GBP")}
            comp = (f"Annual rent is £{annual:,.0f}, which is "
                    f"{'below' if weeks == 5 else 'at or above'} the £50,000 threshold, so "
                    f"the cap is {weeks} weeks' rent: £{dep:,.2f}. The answer must name the "
                    f"threshold it applied.")
            fails = [f"Applies the {'6' if weeks == 5 else '5'}-week cap.",
                     "Computes the weekly rent by dividing by 4 or 4.33.",
                     f"States a deposit that is not {dep:.2f} within rounding."]
        elif kind == "movein":
            weekly = amount * 12 / 52
            dep = weekly * 5
            total = amount + dep
            q = [
                f"If I take a flat at £{amount:,} a month, what do I need up front on day "
                f"one — first month plus the deposit?",
                f"I have got £{amount:,} pcm agreed. How much cash do I need to hand over "
                f"before I get the keys, counting the first month and the deposit?",
                f"Total up front for a £{amount:,} a month place — first month and deposit "
                f"together, please.",
            ][j % 3]
            rc = {**_rc("weekly_rent", f"{amount} * 12 / 52", weekly, "GBP/week"),
                  **_rc("deposit", f"({amount} * 12 / 52) * 5", dep, "GBP"),
                  **_rc("move_in_total", f"{amount} + ({amount} * 12 / 52) * 5", total, "GBP")}
            comp = (f"First month £{amount:,} plus a 5-week deposit £{dep:,.2f} gives "
                    f"£{total:,.2f}. The answer must state the deposit basis it used.")
            fails = ["Omits the deposit or the first month.",
                     "Uses a 6-week deposit when the annual rent is below £50,000.",
                     f"States a total that is not {total:.2f} within rounding."]
        else:
            annual = amount * 12
            q = [
                f"What would £{amount:,} a month come to over a full 12-month tenancy?",
                f"Over a year, how much rent is £{amount:,} per month in total?",
                f"I want the twelve-month total for a £{amount:,} pcm tenancy.",
            ][j % 3]
            rc = _rc("annual_rent", f"{amount} * 12", annual, "GBP/year")
            comp = f"States £{annual:,.0f} for the year."
            fails = [f"States an annual figure that is not {annual:.0f}."]

        add_case(
            cid=cid, task_category="calculation", user_query=q, constraints=[],
            correct_completion=comp,
            failure_conditions=fails + [
                "Calls a search or commute tool for a question that is pure arithmetic.",
                "Introduces a price, area or listing that the user never mentioned."],
            allowed_evidence_sources=[
                "the user's own stated figures",
                "the frozen money formulas in evaluation/benchmark/README.md "
                "(monthly=weekly*52/12, weekly=monthly*12/52, deposit=weekly*5 below "
                "£50,000 annual rent and weekly*6 at or above, move-in=first month+deposit)"],
            novelty_note=("New arithmetic case with a rent figure that does not appear in "
                          "the 98-case benchmark; formula family is shared with B_money but "
                          "no input value, wording or persona is reused."),
            notes=("Pure calculation: expected_tools is empty, so producing no tool evidence "
                   "is the CORRECT behaviour and must never be scored as missing evidence."),
            expected_tools=[], forbidden_tools=["search_properties", "calculate_commute"],
            reference_calculations=rc, expected_route="direct_answer",
            grounding_sources=["user-stated figures", "README money formulas"])


# --------------------------------------------------------------------------- #
# memory — 20 cases: 10 recall, 10 write
# --------------------------------------------------------------------------- #
MEM_READ = [
    ("budget", "You told me before what my ceiling was — what budget did I give you?",
     "User's maximum rent is £1,650 per month.", "£1,650 per month"),
    ("area", "Which area did I say I wanted to live in?",
     "User wants to live in Walthamstow.", "Walthamstow"),
    ("commute", "Remind me which station I said I commute to.",
     "User commutes to Liverpool Street.", "Liverpool Street"),
    ("beds", "How many bedrooms did I tell you I needed?",
     "User needs a 2-bedroom property.", "2 bedrooms"),
    ("pets", "Did I mention anything about pets?",
     "User has a cat and needs a pet-friendly let.", "a cat, so pet-friendly"),
    ("movein", "What move-in date did I give you?",
     "User needs to move in by 1 October 2026.", "1 October 2026"),
    ("furnishing", "Did I say furnished or unfurnished?",
     "User wants an unfurnished property.", "unfurnished"),
    ("worktype", "What did I say about working from home?",
     "User works from home three days a week and wants a study or second room.",
     "works from home three days a week"),
    ("parking", "Did I tell you whether I need parking?",
     "User needs off-street parking for one car.", "off-street parking for one car"),
    ("sharers", "How many of us did I say would be living there?",
     "User will share the property with two friends (three people total).",
     "three people"),
]
MEM_WRITE = [
    ("My budget has gone up — I can now go to £1,900 a month. Please remember that.",
     "User's maximum rent is now £1,900 per month.", "£1,900 per month"),
    ("Please make a note that I have decided on Peckham rather than anywhere else.",
     "User has decided on Peckham.", "Peckham"),
    ("Save this for next time: I need to be within 30 minutes of London Bridge.",
     "User needs to be within 30 minutes of London Bridge.", "30 minutes of London Bridge"),
    ("Note that I now need three bedrooms, not two.",
     "User now needs three bedrooms (previously two).", "three bedrooms"),
    ("Remember that I have a dog, so anywhere I look has to allow pets.",
     "User has a dog and needs a pet-friendly let.", "a dog"),
    ("Please keep a note that I cannot move before 15 November.",
     "User cannot move before 15 November 2026.", "15 November"),
    ("For future reference, I would prefer somewhere furnished.",
     "User prefers a furnished property.", "furnished"),
    ("Record that I want a garden — that is non-negotiable for me now.",
     "User requires a garden.", "a garden"),
    ("Remember my new work address is near Farringdon, not where I said before.",
     "User's workplace is near Farringdon.", "Farringdon"),
    ("Please store that my maximum commute is 40 minutes door to door.",
     "User's maximum commute is 40 minutes door to door.", "40 minutes"),
]


def build_memory():
    n = 75
    for j, (topic, q, stored, human) in enumerate(MEM_READ, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        fx = write_fixture(
            f"ho2_{n:03d}_recall.json",
            [{"tool_name": "recall_memory", "success": True, "error": None,
              "data": {"success": True, "count": 1,
                       "memories": [{"content": stored, "mtype": "semantic", "score": 0.94}],
                       "formatted": f"- {stored}"}}],
            f"{cid}: frozen recall_memory bucket holding exactly one fact ({topic}).")
        add_case(
            cid=cid, task_category="memory", user_query=q, constraints=[],
            correct_completion=(
                f"Reads the stored memory and reports it back: {human}. Correct behaviour "
                f"here is NOT to produce listings — the user asked what was remembered. "
                f"The answer must not add any preference the bucket does not contain."),
            failure_conditions=[
                "Reports a value the memory bucket does not contain.",
                "Says nothing is remembered when the bucket holds the fact.",
                "Invents additional stored preferences.",
            ],
            allowed_evidence_sources=["recall_memory -> data.memories[].content",
                                      "the user's own words in this turn"],
            novelty_note=("New memory-read turn with a fact and phrasing that do not appear "
                          "in the 98-case G_memory shard; each case uses its own user_id."),
            notes="Memory read: the success condition is recalling the stored fact, not recommending property.",
            expected_tools=["recall_memory"],
            forbidden_tools=["search_properties"], fixture=fx,
            expected_route="recall_memory",
            grounding_sources=["stored user memory (recall_memory)"])

    for j, (q, stored, human) in enumerate(MEM_WRITE, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        fx = write_fixture(
            f"ho2_{n:03d}_remember.json",
            [{"tool_name": "remember", "success": True, "error": None,
              "data": {"success": True, "stored": stored, "mtype": "semantic",
                       "message": "Saved to your profile."}}],
            f"{cid}: frozen remember() acknowledgement for a preference update.")
        add_case(
            cid=cid, task_category="memory", user_query=q, constraints=[],
            correct_completion=(
                f"Writes the preference to memory and confirms back what was stored "
                f"({human}). Correct behaviour is a confirmation, not a property search; "
                f"not producing listings is not a defect here."),
            failure_conditions=[
                "Confirms storing something different from what the user said.",
                "Claims to have searched or produces listings that do not exist.",
                "Silently ignores the instruction to remember.",
            ],
            allowed_evidence_sources=["remember -> data.stored",
                                      "the user's own words in this turn"],
            novelty_note=("New memory-write turn; the preference value and wording do not "
                          "appear in the 98-case set and the user_id is unique to this case."),
            notes="Memory write: success = the preference is stored and echoed accurately.",
            expected_tools=["remember"], forbidden_tools=["search_properties"], fixture=fx,
            expected_route="remember",
            grounding_sources=["stored user memory (remember)"])


# --------------------------------------------------------------------------- #
# clarify — 15 cases: 8 under-specified, 7 out of coverage
# --------------------------------------------------------------------------- #
CLARIFY_UNDERSPEC = [
    ("Find me somewhere to live.",
     "no area, no budget, no property type — nothing to search on"),
    ("I need a place by next month, can you sort it?",
     "a date but no area, budget or property type"),
    ("What's the best option for me?",
     "'best' with no criteria and no candidate set to rank"),
    ("Book me a viewing for the good one.",
     "'the good one' has no referent and there is no prior turn in this conversation"),
    ("Is it within my budget?",
     "neither 'it' nor the budget has been stated anywhere in this conversation"),
    ("How much would the deposit be?",
     "no rent figure was given, so the deposit cannot be computed"),
    ("Which of those two is closer to work?",
     "'those two' has no referent and no workplace has been named"),
    ("Can you check if that one is still available?",
     "'that one' has no referent — no property has been mentioned"),
]
CLARIFY_OUT_OF_SCOPE = [
    ("Can you sign the tenancy agreement on my behalf?", "acting as the user's legal agent"),
    ("What will rents in Peckham be in three years' time?", "forecasting future market prices"),
    ("Can you get the landlord to drop the rent by £200?", "negotiating with a third party"),
    ("Please transfer my deposit to the agent for me.", "moving money"),
    ("Tell me which of my neighbours have criminal records.", "personal data about private individuals"),
    ("Can you write me a reference from my previous landlord?", "fabricating a document from a third party"),
    ("Will I be approved for this tenancy by the referencing company?", "a third party's credit decision"),
]


def build_clarify():
    n = 95
    for j, (q, gap) in enumerate(CLARIFY_UNDERSPEC, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        add_case(
            cid=cid, task_category="clarify", user_query=q, constraints=[],
            correct_completion=(
                f"The request cannot be answered as stated: {gap}. The correct behaviour is "
                f"to ask ONE specific clarifying question that names the missing piece, or "
                f"to state plainly what is missing. Producing listings, a price or a deposit "
                f"figure here would mean inventing the missing premise; not producing them "
                f"is the success condition for this case."),
            failure_conditions=[
                "Answers with concrete listings, prices or figures despite the missing premise.",
                "Silently assumes an area, budget or property the user never named.",
                "Asks nothing and gives a generic non-answer that does not identify what is missing.",
            ],
            allowed_evidence_sources=["the user's own words in this turn"],
            novelty_note=("New under-specified turn; wording is not taken from the 98-case "
                          "clarification cases and carries no conversation history at all, "
                          "so the missing referent is unambiguous."),
            notes="Clarification case: the success condition is asking, not answering.",
            expected_tools=[], forbidden_tools=["search_properties", "calculate_commute"],
            expected_route="clarification",
            grounding_sources=["user-stated information only"])

    for j, (q, why) in enumerate(CLARIFY_OUT_OF_SCOPE, start=1):
        n += 1
        cid = f"HO2-{n:03d}"
        add_case(
            cid=cid, task_category="clarify", user_query=q, constraints=[],
            correct_completion=(
                f"The request is outside what a rental-search assistant can do ({why}). The "
                f"correct behaviour is to say plainly that it cannot be done and, where "
                f"useful, point to what can be done instead. Refusing here is the success "
                f"condition; producing a confident answer would be the defect."),
            failure_conditions=[
                "Claims to have performed the out-of-scope action.",
                "Produces a fabricated figure, forecast, document or personal record.",
                "Gives no indication that the request is outside its scope.",
            ],
            allowed_evidence_sources=["the user's own words in this turn"],
            novelty_note=("New out-of-coverage turn; the 98-case set contains no request of "
                          "this kind for this capability boundary."),
            notes="Out-of-coverage case: the success condition is an honest refusal, not a search.",
            expected_tools=[], forbidden_tools=["search_properties", "calculate_commute",
                                                "web_search"],
            expected_route="clarification",
            grounding_sources=["user-stated information only"])


# --------------------------------------------------------------------------- #
def self_check():
    from collections import Counter
    strat = Counter(c["task_category"] for c in CASES)
    want = {"retrieval_hard": 35, "retrieval_soft": 20, "calculation": 20,
            "memory": 20, "clarify": 15}
    assert strat == Counter(want), f"stratum quota off: {dict(strat)}"
    ids = [c["case_id"] for c in CASES]
    assert len(set(ids)) == len(ids) == 110, "case_id collision or wrong count"

    cover = v2.slot_coverage(CASES, FIX_DIR)
    sat = cover["satisfaction"]
    problems = [f"{s}: {sat[s]}/{need}" for s, need in v2.SLOT_MIN_COVERAGE.items()
                if sat[s] < need]
    assert not problems, f"slot satisfaction floors unmet: {problems}"
    assert cover["no_result_or_unknown_cases"] >= 12, cover

    for c in CASES:
        if c["task_category"] != "retrieval_hard":
            continue
        cons = v2.user_hard_constraints(c)
        assert cons, c["case_id"]
        for con in cons:
            slot = v2.slot_of(con)
            verifiable = v2.constraint_is_satisfaction_verifiable(c, con, FIX_DIR)
            unknown_ok = (slot == "move_in_date" and not verifiable)
            assert verifiable or unknown_ok, \
                f"{c['case_id']} slot {slot} has no violation trap"
        probs = v2.explicitness_problems(c) + v2.contradictions(cons)
        for con in cons:
            probs += v2.arg_domain_problems(con)
        assert not probs, f"{c['case_id']}: {probs}"
        listings = v2.fixture_listings(c, FIX_DIR)
        streets = [v2.listing_tokens(l)[0] for l in listings]
        assert len(set(streets)) == len(streets), f"{c['case_id']} duplicate street token"
        prices = [l["price_raw"] for l in listings]
        assert len(set(prices)) == len(prices), f"{c['case_id']} duplicate price"
        vals = {con.get("value") for con in cons if isinstance(con.get("value"), int)}
        assert not (set(prices) & vals), f"{c['case_id']} price equals a constraint value"
        q = c["user_query"].casefold()
        assert not any(s.casefold() in q for s in streets), \
            f"{c['case_id']} a street name leaks into the request"
    print(json.dumps({"strata": dict(strat), "slot_satisfaction": sat,
                      "behaviour_cases": cover["no_result_or_unknown_cases"]},
                     indent=2, ensure_ascii=False))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_retrieval_hard()
    build_retrieval_soft()
    build_calculation()
    build_memory()
    build_clarify()
    self_check()

    with CASES_PATH.open("w", encoding="utf-8") as fh:
        for c in CASES:
            fh.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    fixtures = sorted(p for p in FIX_DIR.glob("ho2_*.json"))
    manifest = {
        "dataset": "rentcompass-holdout-v2",
        "authored_on": AUTHORED_ON,
        "schema_version": SCHEMA_VERSION,
        "n_cases": len(CASES),
        "generator": "evaluation/results/_harness/build_holdout_v2.py",
        "generator_sha256": sha(Path(__file__)),
        "schema_module_sha256": sha(Path(__file__).with_name("constraint_schema_v2.py")),
        "cases_file": str(CASES_PATH.relative_to(REPO)),
        "cases_sha256": sha(CASES_PATH),
        "n_fixtures": len(fixtures),
        "fixtures_sha256": {p.name: sha(p) for p in fixtures},
        "deterministic": "no RNG anywhere; rebuilding reproduces byte-identical output",
    }
    (OUT_DIR / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"cases  -> {CASES_PATH}  sha256={manifest['cases_sha256']}")
    print(f"fixtures: {len(fixtures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
