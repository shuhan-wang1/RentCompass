"""A question the ledger cannot answer keeps its answer, beside the ledger (2026-11-02).

A student asked: "could you first help me to establish a typical range for an ensuite and a
studio for a student at UCL? ... I would take 40min with public transportation". The model's
draft gave the range (en-suite about £1,300–£1,800 a month, studio £1,800–£2,600) and some
listings. The user received only "No listing is both fully eligible and verified. Not
verified:" and six rows ending ": cannot verify the commute condition" — no range, no rents,
one building listed three times. Four links of the chain each dropped part of the answer:

1. the commute redactor withdrew the whole range clause for its "25–40 minutes out";
2. the critic stopped counting the search as evidence once validation had moved every row
   to ``unverified_candidates``, so the answer's prices read as unsupported;
3. ``format_output_fc`` let an answer lead the ledger only for availability, comparison and
   cost questions, never for a price-range question;
4. app.py rebuilt the WHOLE reply from its re-checked ledger whenever any row was not
   eligible, so even an answer that led the ledger never reached the user;

and the ledger itself printed unverified rows without their rent and once per unit.

The guarantee those layers protect still holds: no listing that is not eligible is ever
presented as meeting the user's conditions.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ.setdefault("CONVERSATION_DB_PATH", os.path.join(
    tempfile.mkdtemp(prefix="answer_leads_ledger_"), "conversations.sqlite3"))
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ.setdefault("AGENT_MEMORY_ENABLED", "0")

import pytest  # noqa: E402

from core.candidate_validation import (  # noqa: E402
    answer_lead_without_unverified_claims, render_candidate_status, validate_candidates,
    validate_commute_response, validate_search_payload)

UCL = "University College London, Gower Street, London WC1E 6BT"
MESSAGE = ("1. I am not sure about the specific budget, could you first help me to establish a "
           "typical range for an ensuite and apartment for student in UCL?\n2. ensuite and studio\n"
           "3. Yes I would need to communicate to UCL, I would take 40min with public transportation")
CRITERIA = {"commute_destination": UCL, "destination": UCL, "max_travel_time": 40,
            "max_commute_time": 40, "room_type": "studio", "location": "London"}

WEYMOUTH = "L-Suite at Gravity Co., The Weymouth Marylebone, 50-60 Weymouth Street W1G"
STEPHENS = ("Furnished Studio Apartment | Bills Included at Gravity Co., Notting Hill, "
            "6 St. Stephens Crescent W2")
HAMMERSMITH = "Manhattan Style Apartment at Gravity Co., Gravity Co Hammersmith, 200 Hammersmith Road, W6 W6"

VERIFIED = [("Guilford Street, London WC1N", 1127, 19), ("Gray's Inn Road, London WC1X", 1550, 15),
            ("Lamb's Conduit Street, London WC1N", 1550, 21)]
UNVERIFIED = [(WEYMOUTH, 3995), (WEYMOUTH, 3995), (WEYMOUTH, 4200), (STEPHENS, 2100),
              (STEPHENS, 2100), (HAMMERSMITH, 2500), ("Marchmont Street, London WC1N", 1650)]

DRAFT = (
    "For a UCL student, typical rents are about £1,300–£1,800 a month for an en-suite room and "
    "£1,800–£2,600 for a studio in areas 25–40 minutes out, such as Camden or Islington.\n\n"
    "Here are some studios I found:\n"
    "1. Guilford Street, London WC1N — £1,127/month, about 19 minutes to UCL, which fits your "
    "40-minute limit.\n"
    "2. Gravity Co. Marylebone (Weymouth Street) — £3,995/month, a good option close to UCL.\n"
    "3. Gravity Co. Notting Hill (St. Stephens Crescent) — £2,100/month, bills included, a good match.\n"
    "4. Marchmont Street — £1,650/month, which meets your requirements."
)
RANGE = ("typical rents are about £1,300–£1,800 a month for an en-suite room and £1,800–£2,600 "
         "for a studio")


def _row(address, price, index):
    return {"address": address, "Address": address, "price": f"£{price:,}/month",
            "Price": f"£{price:,}/month", "price_numeric": price, "bedrooms": 0,
            "room_type": "studio", "Room_Type_Category": "studio", "property_type": "Studio",
            "area": "London", "url": f"https://listing.example/{index}", "source": "OnTheMarket"}


def _evidence(address, minutes):
    return {"success": True, "from_address": address, "to_address": UCL, "duration_minutes": minutes,
            "raw_data": {"from_address": address, "to_address": UCL, "duration_minutes": minutes,
                         "basis": "tfl_journey_plan"}}


def _payload(*, verified=True):
    rows = ([_row(a, p, i) for i, (a, p, _m) in enumerate(VERIFIED)] if verified else []) + \
        [_row(a, p, 10 + i) for i, (a, p) in enumerate(UNVERIFIED)]
    evidence = [_evidence(a, m) for a, _p, m in VERIFIED] if verified else []
    return {"status": "found", "total_found": len(rows), "recommendations": rows,
            "search_criteria": dict(CRITERIA), "summary": f"Found {len(rows)} properties.",
            "commute_evidence": evidence}


def _state(payload, *, message=MESSAGE, grounded=True):
    return {"tool_artifacts": [{"tool": "search_properties", "turn": 1, "success": True,
                                "raw_data": payload}],
            "extracted_context": {"current_message": message, "reply_language": "en"},
            "user_query": message, "user_preferences": {},
            "accumulated_search_criteria": {"max_travel_time": 40, "commute_destination": UCL,
                                            "room_type": "studio"},
            "verdict": {"grounded": grounded, "issues": []}}


# ── 4. the ledger: every unverified row carries its rent, one line per building ────────────

def test_unverified_rows_carry_their_rent_and_group_by_building():
    validation = validate_candidates([_row(a, p, i) for i, (a, p) in enumerate(UNVERIFIED)],
                                     CRITERIA, [])
    text = render_candidate_status(validation)
    assert f"- {WEYMOUTH} (3 listings: £3,995/month, £4,200/month): cannot verify the commute condition" in text
    assert f"- {STEPHENS} (2 listings at £2,100/month): cannot verify the commute condition" in text
    assert "- Marchmont Street, London WC1N (£1,650/month): cannot verify the commute condition" in text
    rows = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(rows) == len(set(rows)) == 5        # 4 addresses + the headline line


def test_unverified_rows_in_chinese_carry_their_rent():
    validation = validate_candidates([_row(a, p, i) for i, (a, p) in enumerate(UNVERIFIED)],
                                     CRITERIA, [])
    text = render_candidate_status(validation, language="zh")
    assert f"- {STEPHENS}（£2,100/month，共 2 套）：" in text
    assert f"- {WEYMOUTH}（共 3 套：£3,995/month、£4,200/month）：" in text


def test_an_unverified_row_without_a_rent_says_so():
    row = _row("Store Street, London WC1E", 0, 1)
    row.pop("price"), row.pop("Price")
    text = render_candidate_status(validate_candidates([row], CRITERIA, []))
    assert "- Store Street, London WC1E (rent not stated):" in text


# ── 2. the critic: unverified rows are still retrieved listings ────────────────────────────

def test_the_search_stays_price_evidence_when_no_row_could_be_verified():
    from core.langgraph_agent import _artifact_usable, _collect_grounding_evidence
    from uk_rent_agent.agent.critic import unsupported_reply_prices
    validated = validate_search_payload(_payload(verified=False), commute_evidence=[])
    assert validated["recommendations"] == [] and validated["unverified_candidates"]
    artifact = {"tool": "search_properties", "turn": 1, "success": True, "raw_data": validated}
    assert _artifact_usable(artifact)
    state = _state(validated)
    prices = "Asking rents: £2,100/month in Notting Hill and £3,995/month in Marylebone."
    assert unsupported_reply_prices(prices, _collect_grounding_evidence(state, "")) == []
    # The market range is an interval beside retrieved prices, so it is not gated either.
    assert unsupported_reply_prices(f"Studios: {RANGE}.", _collect_grounding_evidence(state, "")) == []


def test_an_invented_rent_is_still_unsupported():
    from core.langgraph_agent import _collect_grounding_evidence
    from uk_rent_agent.agent.critic import unsupported_reply_prices
    validated = validate_search_payload(_payload(verified=False), commute_evidence=[])
    flagged = unsupported_reply_prices("The Notting Hill studio is £1,234/month.",
                                       _collect_grounding_evidence(_state(validated), ""))
    assert flagged == [1234.0]


# ── 1. the redactor: the journey qualifier goes, the money stays ───────────────────────────

def _redact(text, payload=None):
    validated = validate_search_payload(payload or _payload(verified=False), commute_evidence=[])
    state = dict(_state(validated), candidate_validation=validated["candidate_validation"])
    return validate_commute_response(text, state)


def test_a_range_sentence_loses_only_its_unverified_minutes():
    out = _redact("For a UCL student, typical rents are about £1,300–£1,800 a month for an en-suite "
                  "room and £1,800–£2,600 for a studio in areas 25–40 minutes out, such as Camden "
                  "or Islington.")
    assert RANGE in out
    assert "in areas such as Camden or Islington." in out
    assert "25–40" not in out
    assert "could not be verified" in out            # the withdrawal is still declared


@pytest.mark.parametrize("text,kept,gone", [
    ("Studios within a 40-minute commute of UCL typically cost £1,800–£2,600 a month.",
     "Studios typically cost £1,800–£2,600 a month.", "40-minute"),
    ("Typical ranges: en-suite £1,300–£1,800 a month, studio £1,800–£2,600, in areas 25–40 "
     "minutes from UCL by tube.", "studio £1,800–£2,600", "25–40"),
])
def test_other_range_wordings_keep_their_money(text, kept, gone):
    out = _redact(text)
    assert kept in out and gone not in out


@pytest.mark.parametrize("text", [
    "The Notting Hill studio at £2,100/month is 17 minutes from UCL.",
    "Gravity Co. Notting Hill is about 17 minutes from UCL.",
])
def test_a_listing_claim_that_cannot_be_cut_cleanly_still_goes_whole(text):
    out = _redact(text)
    assert "17 minutes" not in out and " is." not in out


# ── the lead guard ──────────────────────────────────────────────────────────────────────────

def _validation(verified=True):
    payload = _payload(verified=verified)
    return validate_candidates(payload["recommendations"], CRITERIA, payload["commute_evidence"])


def test_the_lead_keeps_the_range_and_the_verified_listing_and_loses_the_rest():
    lead = answer_lead_without_unverified_claims(DRAFT, _validation())
    assert RANGE in lead
    assert "Guilford Street, London WC1N — £1,127/month" in lead
    for gone in ("Weymouth", "Stephens", "Marchmont"):
        assert gone not in lead
    assert "1. Guilford Street" in lead


def test_a_heading_whose_every_item_went_goes_too():
    draft = ("Studios usually cost £1,800–£2,600 a month.\n\nHere are some studios I found:\n"
             "- Marchmont Street — £1,650/month, which meets your requirements.\n"
             "- Gravity Co. Notting Hill — £2,100/month, a good option.")
    lead = answer_lead_without_unverified_claims(draft, _validation())
    assert lead == "Studios usually cost £1,800–£2,600 a month."


def test_a_shared_street_is_decided_by_the_rent_it_quotes():
    rows = [_row("Guilford Street, London WC1N", 1127, 1), _row("Guilford Street, London WC1N", 1890, 2)]
    validation = validate_candidates(rows, CRITERIA, [_evidence("Guilford Street, London WC1N", 19)])
    statuses = {s["candidate"]["price_numeric"]: s["status"] for s in validation["statuses"]}
    if statuses.get(1127) != "eligible" or statuses.get(1890) == "eligible":
        # one measured route evidences both rows of the street; split them by hand
        validation = {"statuses": [dict(validation["statuses"][0], status="eligible"),
                                   dict(validation["statuses"][1], status="unknown")]}
    lead = answer_lead_without_unverified_claims(
        "Guilford Street at £1,127/month fits all your conditions. "
        "Guilford Street at £1,890/month fits too.", validation)
    assert "£1,127" in lead and "£1,890" not in lead


def test_naming_an_unverified_listing_without_a_claim_is_kept():
    draft = "Gravity Co. in Notting Hill lists studios from £2,100/month; I could not check its commute."
    assert answer_lead_without_unverified_claims(draft, _validation()) == draft


def test_a_lead_with_only_unverified_claims_is_empty():
    assert answer_lead_without_unverified_claims(
        "Marchmont Street meets your requirements.", _validation()) == ""


# ── 3. format_output_fc: a price-range question keeps its answer ───────────────────────────

class _NoTools:
    def list_specs(self):
        return []


def _format(state, draft):
    from core.agent_loop import build_fc_nodes
    return build_fc_nodes(_NoTools())["format_output_fc"](dict(state, final_response=draft))


def _pipeline(payload, *, message=MESSAGE, grounded=True):
    """The draft as the agent node hands it on (commute redactor), then the formatter."""
    state = _state(payload, message=message, grounded=grounded)
    redacted = validate_commute_response(DRAFT, dict(
        state, candidate_validation=validate_search_payload(
            dict(payload), commute_evidence=payload["commute_evidence"])["candidate_validation"]))
    return state, _format(state, redacted)


def test_a_price_range_question_keeps_its_answer_before_the_ledger():
    _state_, out = _pipeline(_payload())
    text = out["final_response"]
    assert RANGE in text
    assert text.index(RANGE) < text.index("Meets all declared conditions (verified):")
    assert out["tool_data"]["answer_lead"] and RANGE in out["tool_data"]["answer_lead"]


@pytest.mark.parametrize("message", [
    "Find me a studio near UCL, 40 minutes by public transport.",
    "Find me a studio near UCL, I've set a budget of £2,000, 40 minutes by public transport.",
])
def test_a_plain_search_keeps_the_ledger_alone(message):
    """No model prose leads a plain search (r6-O8). Only the lead built from the tool data
    does (2026-11-03: the bare ledger read as a status dump with no answer)."""
    _state_, out = _pipeline(_payload(), message=message)
    text = out["final_response"]
    lead, ledger = text.split("\n\n", 1)
    assert lead == "3 listings meet everything you asked for, from £1,127 to £1,550 a month."
    assert ledger.startswith("Meets all declared conditions (verified):")
    assert RANGE not in text
    assert "answer_lead" not in out["tool_data"]


def test_an_ungrounded_answer_keeps_the_ledger_alone():
    _state_, out = _pipeline(_payload(), grounded=False)
    text = out["final_response"]
    assert RANGE not in text
    assert text.split("\n\n", 1)[1].startswith("Meets all declared conditions (verified):")
    assert "answer_lead" not in out["tool_data"]


# ── end to end through app.py ───────────────────────────────────────────────────────────────

def _run_app_turn(monkeypatch, final_state, conversation_id):
    import app as appmod

    class _FakeGraph:
        async def ainvoke(self, graph_input, config=None):
            return dict(final_state)

    fake = _FakeGraph()
    setattr(fake, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", fake)
    return asyncio.run(appmod.handle_with_react_agent(
        MESSAGE, {}, False, user_id="answer-leads-ledger", conversation_id=conversation_id,
        request_id=f"req-{conversation_id}", ui_language="en"))


def _assert_honest(message):
    assert RANGE in message
    lead, _sep, ledger = message.partition("Meets all declared conditions (verified):") \
        if "Meets all declared conditions (verified):" in message else \
        message.partition("- No listing is both fully eligible and verified.")
    # No listing that is not eligible is presented as meeting a condition in the answer...
    for line in re.split(r"(?<=[.!?])\s+|\n", lead):
        if re.search(r"Weymouth|Stephens|Hammersmith|Marchmont", line):
            assert not re.search(r"\b(?:fits?|meets?|match|good option|within)\b", line), line
    # ... nor in the ledger, where every unverified row is filed as such, with its rent.
    verified_part, _s, unverified_part = ledger.partition("Not verified:")
    assert not re.search(r"Weymouth|Stephens|Hammersmith|Marchmont", verified_part)
    rows = [line for line in unverified_part.splitlines() if line.startswith("- ")]
    assert rows and all(re.search(r"\(.*£[\d,]+", row) for row in rows)
    assert len(rows) == len(set(rows))


def test_three_verified_and_seven_unverified_reach_the_user_with_the_range(monkeypatch):
    payload = _payload()
    state, out = _pipeline(payload)
    final_state = dict(state, **out)
    result = _run_app_turn(monkeypatch, final_state, "c-answer-leads-3-7")
    message = result["message"]
    _assert_honest(message)
    assert "Guilford Street, London WC1N (£1,127/month, commute verified at 19 min)" in message
    assert "answer_lead" not in str(result.get("tool_data") or "")


def test_no_verified_listing_still_ships_the_range_beside_the_rechecked_ledger(monkeypatch):
    """The reported turn: nothing verified, so app.py rebuilds the ledger from its re-check —
    and now keeps the answer that led it."""
    payload = _payload(verified=False)
    state, out = _pipeline(payload)
    assert out["tool_data"].get("answer_lead"), out["final_response"]
    final_state = dict(state, **out)
    result = _run_app_turn(monkeypatch, final_state, "c-answer-leads-0-7")
    message = result["message"]
    _assert_honest(message)
    assert "- No listing is both fully eligible and verified." in message
    assert message.index(RANGE) < message.index("Not verified:")
    assert "answer_lead" not in str(result)


def test_a_plain_search_turn_through_app_py_still_ships_the_ledger_alone(monkeypatch):
    payload = _payload(verified=False)
    state, out = _pipeline(payload, message="Find me a studio near UCL, 40 minutes by public transport.")
    result = _run_app_turn(monkeypatch, dict(state, **out), "c-answer-leads-plain")
    lead, ledger = result["message"].split("\n\n", 1)
    # The lead app.py renders from its own re-check: built from the ledger, no model prose.
    assert lead == ("No listing could be confirmed against every condition you gave; what was "
                    "found is listed below.")
    assert ledger.startswith("- No listing is both fully eligible and verified.")
    assert RANGE not in result["message"]
