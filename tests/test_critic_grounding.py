"""Acceptance tests for the redesigned grounding critic.

Two layers:

* pure rubric (:mod:`uk_rent_agent.agent.critic`) — currency-agnostic numeric
  normalization, derivation rules, and the evidence-surface semantics;
* enforcement — a not-grounded verdict triggers exactly one regeneration pass and
  the user-facing text is never the legacy hard-replacement fallback.

Run with the project venv, e.g.::

    python -m pytest tests/test_critic_grounding.py -q

``pythonpath = ["src", "app"]`` in ``pyproject.toml`` puts both
``uk_rent_agent`` and ``core`` on the path.
"""

from __future__ import annotations

import asyncio

import pytest

from uk_rent_agent.agent.critic import (
    CAVEAT,
    LEGACY_INCONSISTENCY_FALLBACK,
    LEGACY_RETRIEVAL_MISS_FALLBACK,
    STATION_CAVEAT,
    append_caveat,
    build_correction_instruction,
    enforce_grounding,
    evaluate_grounding,
    station_name_claims,
    ungrounded_station_names,
    unsupported_reply_prices,
)

_LEGACY_FALLBACKS = {LEGACY_INCONSISTENCY_FALLBACK, LEGACY_RETRIEVAL_MISS_FALLBACK}


# ── numeric normalization: the acceptance matrix ───────────────────────────

def test_formatting_only_difference_is_grounded():
    # evidence "2678 pcm" (no currency symbol) vs reply "£2,678"
    verdict = evaluate_grounding("The rent is £2,678.", "2678 pcm")
    assert verdict.grounded is True
    assert unsupported_reply_prices("The rent is £2,678.", "2678 pcm") == []


def test_fabricated_price_is_caught_even_with_suffix_currency_evidence():
    # evidence "2678 GBP per month" (suffix currency) + reply inventing "£3,999"
    evidence = "2678 GBP per month"
    verdict = evaluate_grounding("A comparable flat is £3,999.", evidence)
    assert verdict.grounded is False
    assert verdict.needs_replan is True
    assert unsupported_reply_prices("A comparable flat is £3,999.", evidence) == [3999.0]


def test_annual_total_derivation_is_grounded():
    # evidence "£2678/month" + reply "£32,136 total over 12 months" (2678 * 12)
    evidence = "£2678/month"
    reply = "That is £32,136 total over 12 months."
    assert unsupported_reply_prices(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_weekly_to_monthly_conversion_is_grounded():
    # evidence weekly "£450 pw" + reply "£1,950 pcm" (450 * 52 / 12)
    evidence = "£450 pw"
    reply = "That works out to about £1,950 pcm."
    assert unsupported_reply_prices(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_deposit_multiple_is_grounded():
    # deposit of 5-6 weeks derived from a monthly rent
    evidence = "£2000 pcm"
    weekly = 2000 * 12 / 52  # ~461.54
    deposit = round(weekly * 6)  # ~2769
    reply = f"The deposit is £{deposit:,} (six weeks' rent)."
    assert unsupported_reply_prices(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_user_budget_from_context_is_grounded():
    # reply echoing the user's own budget present in the assembled context
    evidence = [
        {"property_info": "A studio near UCL"},
        "=== USER PREFERENCES ===\nBudget: £1,200 pcm\n=== END PREFERENCES ===",
        {"max_budget": 1200},
    ]
    reply = "Both options sit within your £1,200 budget."
    assert unsupported_reply_prices(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_plain_integers_are_not_gated():
    # "12 months" / "3 bedrooms" carry no currency/period marker -> ignored
    evidence = "£1500 pcm"
    reply = "This 3-bedroom flat has a 12 month tenancy for £1,500 pcm."
    assert unsupported_reply_prices(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_empty_result_with_honest_synthesis_is_grounded():
    # legitimately-empty tool result + conversational synthesis quoting no figures
    reply = "I couldn't find listings under your budget yet; try widening the area."
    verdict = evaluate_grounding(reply, "", retrieval_expected=True, tool_errored=False)
    assert verdict.grounded is True
    assert "retrieval_miss" not in verdict.issues


def test_retrieval_miss_only_when_errored_and_asserting_facts():
    # tool errored AND the reply asserts a specific figure -> flagged
    errored = evaluate_grounding("It is £2,500 pcm.", "", retrieval_expected=True, tool_errored=True)
    assert errored.grounded is False
    assert "retrieval_miss" in errored.issues
    # tool errored but reply asserts no figures -> not a retrieval_miss
    no_facts = evaluate_grounding(
        "Sorry, I hit an error fetching listings.", "", retrieval_expected=True, tool_errored=True
    )
    assert "retrieval_miss" not in no_facts.issues


def test_direct_answer_skips_price_gating():
    # retrieval_expected False -> conversational reply echoing a number is fine
    verdict = evaluate_grounding("Rents around there are roughly £2,000.", None, retrieval_expected=False)
    assert verdict.grounded is True
    assert verdict.issues == []


def test_backward_compatible_signature():
    # the legacy two-arg call still detects fabrication
    verdict = evaluate_grounding(
        "The rent is £1,999.", [{"address": "Camden", "price": "£1,500 pcm"}]
    )
    assert verdict.grounded is False
    assert verdict.needs_replan is True


# ── enforcement: regeneration, never the bare fallback ─────────────────────

class _Recorder:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[str] = []

    async def __call__(self, correction: str) -> str:
        self.calls.append(correction)
        return self.reply


def _run(coro):
    return asyncio.run(coro)


def test_grounded_answer_skips_regeneration():
    regen = _Recorder("unused")
    outcome = _run(enforce_grounding("Rent is £1,500 pcm.", "£1500 pcm", regenerate=regen))
    assert outcome.regenerated is False
    assert outcome.attempts == 1
    assert regen.calls == []
    assert outcome.response == "Rent is £1,500 pcm."


def test_not_grounded_triggers_one_regeneration_and_delivers_fixed_answer():
    regen = _Recorder("The rent is £1,500 pcm.")  # corrected, grounded
    outcome = _run(enforce_grounding("The rent is £9,999 pcm.", "£1500 pcm", regenerate=regen))
    assert len(regen.calls) == 1  # exactly one corrective pass
    assert outcome.regenerated is True
    assert outcome.attempts == 2
    assert outcome.verdict.grounded is True
    assert outcome.response == "The rent is £1,500 pcm."
    assert outcome.response not in _LEGACY_FALLBACKS


def test_persistent_failure_delivers_caveat_not_fallback():
    regen = _Recorder("Actually it is £8,888 pcm.")  # still fabricated
    original = "The rent is £9,999 pcm."
    outcome = _run(enforce_grounding(original, "£1500 pcm", regenerate=regen))
    assert len(regen.calls) == 1
    assert outcome.regenerated is True
    assert CAVEAT in outcome.response
    assert outcome.response.startswith("Actually it is £8,888 pcm.")
    assert outcome.response not in _LEGACY_FALLBACKS
    assert original in outcome.response or "£8,888" in outcome.response


def test_regeneration_failure_keeps_original_with_caveat():
    async def broken(_correction):
        raise RuntimeError("LLM offline")

    original = "The rent is £9,999 pcm."
    outcome = _run(enforce_grounding(original, "£1500 pcm", regenerate=broken))
    assert CAVEAT in outcome.response
    assert original in outcome.response
    assert outcome.response not in _LEGACY_FALLBACKS


def test_append_caveat_is_idempotent():
    once = append_caveat("Body text.")
    twice = append_caveat(once)
    assert once == twice
    assert once.count(CAVEAT) == 1


# ── node-level wiring (graph import required) ──────────────────────────────

def _load_local_core():
    """Import ``core.*`` from ``app``.

    ``tests/`` has no ``__init__.py`` so pytest prepends it to ``sys.path``, where
    the unrelated ``tests/core`` package shadows the real ``app/core``.
    Put ``app`` first and evict any shadowing ``core`` module.
    """
    import importlib
    import os
    import sys

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name == "core" or name.startswith("core."):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]
    return importlib.import_module("core.llm_config"), importlib.import_module("core.langgraph_agent")


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return _FakeResp(self.content)


def _reasoning_state(final_response: str):
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("tell me more about this flat")
    state["tool_decision"] = {"tool": "reasoning_property"}
    state["tool_observation"] = "Property: 40 Merchant St\nPrice: £2,678 pcm\nRoom Type: Studio"
    state["tool_raw_data"] = {"property_info": "Price: £2,678 pcm"}
    state["final_response"] = final_response
    return state


def test_node_regenerates_and_never_emits_fallback(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    _make_critic_node = lga._make_critic_node

    fake = _FakeLLM("The monthly rent is £2,678 pcm.")  # corrected & grounded
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _reasoning_state("The rent is £4,500 pcm.")  # fabricated
    update = _run(_make_critic_node()(state))

    assert fake.calls == 1  # one regeneration pass fired
    assert update["final_response"] == "The monthly rent is £2,678 pcm."
    assert update["final_response"] not in _LEGACY_FALLBACKS
    assert update["verdict"]["grounded"] is True
    # recommendations payload must be preserved (node must not touch tool_raw_data)
    assert "tool_raw_data" not in update
    assert state["tool_raw_data"] == {"property_info": "Price: £2,678 pcm"}


def test_node_persistent_failure_appends_caveat(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    _make_critic_node = lga._make_critic_node

    fake = _FakeLLM("Actually the rent is £7,777 pcm.")  # still fabricated
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _reasoning_state("The rent is £4,500 pcm.")
    update = _run(_make_critic_node()(state))

    assert fake.calls == 1
    assert CAVEAT in update["final_response"]
    assert update["final_response"] not in _LEGACY_FALLBACKS


def test_node_direct_answer_is_untouched(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    _make_critic_node = lga._make_critic_node

    fake = _FakeLLM("should not be called")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("what's my budget again?")
    state["tool_decision"] = {"tool": "direct_answer"}
    state["final_response"] = "Your budget is £1,200 pcm."
    update = _run(_make_critic_node()(state))

    assert fake.calls == 0  # no gating, no regeneration
    assert "final_response" not in update  # answer unchanged
    assert update["verdict"]["grounded"] is True


# ── fabricated station NAMES (the "Covent Garden" incident) ────────────────
# Prices were normalized, derived and gated; a NAME was gated by nothing. In a real
# session the same property (Tavistock Court, WC1H, Bloomsbury) was reported as nearest
# to "Covent Garden" in one turn and "Russell Square" in another. TfL puts Russell Square
# 214 m from that point; "Covent Garden" exists NOWHERE in this repo — no table, no
# listing field, no scraper, no prompt, no dataset — so it was neither geocoding drift
# nor a lookup bug. It was an invention, and the only thing standing against it was a
# prompt sentence. Every test below fails on the pre-fix critic, which validated money
# figures only.

# The evidence a real nearest-station turn carries: the TfL StopPoint block that
# core.place_reference.nearest_stations returns, as it reaches the critic.
_WC1H_EVIDENCE = [
    {"search_nearby_pois#1": {
        "reference_point": {"resolved_name": "Tavistock Court, Bloomsbury, London WC1H"},
        "nearest_station": {"name": "Russell Square Underground Station",
                            "distance_m": 214, "modes": ["tube"],
                            "source": "TfL StopPoint API"},
        "other_stations_nearby": [
            {"name": "Goodge Street Underground Station", "distance_m": 635},
            {"name": "London Euston Rail Station", "distance_m": 665}],
        "note": "Nearest station per TfL StopPoint API: Russell Square Underground "
                "Station, 214 m straight-line from the geocoded address."}},
]


def test_covent_garden_is_refused():
    """THE regression case, the literal string. The model named a station 1.3 km away
    that no tool supplied; the answer must not be certified as grounded."""
    reply = ("Tavistock Court is a great spot in Bloomsbury. The nearest station is "
             "Covent Garden, about five minutes' walk.")
    assert ungrounded_station_names(reply, _WC1H_EVIDENCE) == ["Covent Garden"]
    verdict = evaluate_grounding(reply, _WC1H_EVIDENCE)
    assert verdict.grounded is False
    assert verdict.needs_replan is True
    assert "ungrounded_stations:Covent Garden" in verdict.issues


def test_covent_garden_is_refused_with_no_station_evidence_at_all():
    """The commoner shape: no tool supplied a nearest station, so nothing constrained the
    model. An absent answer must not read as a blank to fill in."""
    evidence = [{"search_properties#1": {"recommendations": [
        {"address": "Tavistock Court, WC1H", "price": "£2,678 pcm"}]}}]
    reply = "Tavistock Court's nearest tube station is Covent Garden."
    assert ungrounded_station_names(reply, evidence) == ["Covent Garden"]
    assert evaluate_grounding(reply, evidence).grounded is False


def test_the_station_the_data_layer_supplied_is_grounded():
    """The other half of the incident: the turn that got it right must stay right, in
    every spelling an answer plausibly uses for TfL's 'Russell Square Underground
    Station'."""
    for reply in (
        "The nearest station is Russell Square, 214 m away.",
        "The nearest station is Russell Square Underground Station (214 m).",
        "It is a 3-minute walk to Russell Square station.",
        "Russell Square Underground Station is the closest, with Goodge Street and "
        "London Euston stations a little further.",
    ):
        assert ungrounded_station_names(reply, _WC1H_EVIDENCE) == [], reply
        assert evaluate_grounding(reply, _WC1H_EVIDENCE).grounded is True, reply


def test_the_issue_is_surfaced_the_same_way_unsupported_prices_is():
    """Mechanism, not just detection: the evaluator, the critic log line and the
    regeneration pass all read CriticVerdict.issues, so a new failure mode is only
    visible if it is reported there in the same '<kind>:<detail>' shape."""
    reply = "The nearest station is Covent Garden and the rent is £1,234 pcm."
    issues = evaluate_grounding(reply, _WC1H_EVIDENCE).issues
    kinds = [i.split(":", 1)[0] for i in issues]
    assert "unsupported_prices" in kinds
    assert "ungrounded_stations" in kinds
    detail = next(i for i in issues if i.startswith("ungrounded_stations:"))
    assert detail.split(":", 1)[1] == "Covent Garden"  # the name, not a bare flag


def test_correction_instruction_names_the_invented_station():
    """A generic 'do not fabricate' line was already in the prompt when Covent Garden
    shipped. The corrective pass must name it, as it names invented prices."""
    text = build_correction_instruction([], ["Covent Garden"])
    assert "'Covent Garden'" in text
    assert "Name a station ONLY if that exact name is present in the data" in text
    # ...and the price wording must not claim a price problem that did not happen.
    assert "NOT present in the data above: £" not in text


def test_station_failure_regenerates_and_delivers_the_fixed_answer():
    regen = _Recorder("The nearest station is Russell Square, 214 m away.")
    outcome = _run(enforce_grounding(
        "The nearest station is Covent Garden.", _WC1H_EVIDENCE, regenerate=regen))
    assert len(regen.calls) == 1
    assert "Covent Garden" in regen.calls[0]
    assert outcome.verdict.grounded is True
    assert outcome.response == "The nearest station is Russell Square, 214 m away."
    assert outcome.response not in _LEGACY_FALLBACKS


def test_persistent_station_failure_is_caveated_never_deleted():
    """Asymmetry preserved: catching an invented name must not destroy the answer, and
    the caveat must point at the name rather than at the prices, which were fine."""
    regen = _Recorder("Actually the nearest station is Leicester Square.")
    outcome = _run(enforce_grounding(
        "The nearest station is Covent Garden.", _WC1H_EVIDENCE, regenerate=regen))
    assert outcome.response.startswith("Actually the nearest station is Leicester Square.")
    assert STATION_CAVEAT in outcome.response
    assert CAVEAT not in outcome.response
    assert outcome.response not in _LEGACY_FALLBACKS


def test_a_price_failure_still_gets_the_price_caveat():
    """Non-regression on the caveat split."""
    regen = _Recorder("Actually it is £8,888 pcm.")
    outcome = _run(enforce_grounding("The rent is £9,999 pcm.", "£1500 pcm", regenerate=regen))
    assert CAVEAT in outcome.response
    assert STATION_CAVEAT not in outcome.response


def test_station_gating_is_skipped_for_direct_answers():
    """A chat turn that echoes a station the user just named is not a fabrication."""
    verdict = evaluate_grounding(
        "Covent Garden station is on the Piccadilly line, yes.", None,
        retrieval_expected=False)
    assert verdict.grounded is True
    assert verdict.issues == []


# ── the other direction: legitimate prose must NOT be flagged ──────────────
# An over-eager name check is worse than no check: it burns a regeneration pass and
# caveats a correct answer. Every string below is real prose from a retained answer of
# the 2026-07-25 internal round (.runtime/round-8793c0b-internal-2026-07-25/bodies),
# or the exact trap phrasings — borough names, "central London", "the West End", generic
# area words and the user's own words echoed back.

_LEGITIMATE_MENTIONS = [
    # generic, unnamed station references (D10 / D12 / D6)
    "Walking from the station on main roads is considered relatively safe.",
    "Night safety is rated as relatively good -- walking from the tube station on main "
    "roads is considered safe.",
    "Standard city precautions apply -- especially at night, and around the station and "
    "high street.",
    # a mode/network named where a station is not (E4, C8)
    "Chessington does not have a London Underground (Tube) station.",
    "Did you mean a 15-min walk to a **train station** instead?",
    "Manchester does not have a \"Tube\" (London Underground) -- it has the "
    "**Manchester Metrolink** tram system.",
    # line names, which collide with real station names (Victoria, Piccadilly, Waterloo)
    "A Northern line station would put you 20 minutes from campus.",
    "There is a Victoria line station within walking distance.",
    # areas, boroughs and generic geography — never station claims
    "This is a well-connected part of central London with fast links to the West End.",
    "Camden and Islington are both boroughs with good transport, and Zone 2 stations "
    "are cheap to travel from.",
    "The property is in the West End, close to Covent Garden and Soho.",
    "Rents in Hackney, Peckham and Bloomsbury vary a lot by street.",
    "It is well connected to central London stations.",
    # attributes of a station rather than another station's name
    "The station is in Zone 2 and is step-free.",
    "The nearest station is a 5-minute walk away.",
    "The nearest station is NOT known from the data I retrieved, so I will not name one.",
]


@pytest.mark.parametrize("prose", _LEGITIMATE_MENTIONS)
def test_legitimate_prose_is_not_flagged(prose):
    """Both directions, measured. Evidence is deliberately EMPTY: nothing in these
    sentences may be treated as an asserted station name even with no evidence at all,
    because none of them names a station."""
    assert station_name_claims(prose) == [], prose
    assert ungrounded_station_names(prose, "") == [], prose
    assert evaluate_grounding(prose, "").grounded is True, prose


def test_the_users_own_station_is_grounded():
    """'Covent Garden' echoed back because the USER asked about it is not an invention.
    The user's area reaches the critic through the search criteria in the artifact."""
    evidence = [{"search_properties#1": {
        "search_criteria": {"area": "Covent Garden", "max_budget": 2500},
        "status": "no_results"}}]
    reply = ("I could not find anything in Covent Garden under £2,500 pcm. Covent Garden "
             "station is very central, so stock is thin there.")
    assert ungrounded_station_names(reply, evidence) == []
    assert evaluate_grounding(reply, evidence).grounded is True


def test_route_leg_stations_are_grounded():
    """The real C6 shape: every station in a commute answer comes from the route legs of
    the journey plan, so a multi-leg answer must pass unflagged."""
    evidence = [{"calculate_commute#1": {
        "duration_minutes": 26, "route_source": "tfl",
        "route_summary": "Walk to Angel (7 min) -> Northern line to Euston (4 min) -> "
                         "Walk to UCL (15 min)"}}]
    reply = ("From 20 Liverpool Road it is 26 minutes: walk to Angel station, then the "
             "Northern line to Euston station.")
    assert ungrounded_station_names(reply, evidence) == []


def test_a_listing_description_grounds_the_stations_it_names():
    """A4, verbatim: the names come from the listing's own text, not from the model."""
    evidence = [{"search_properties#1": {"recommendations": [{
        "address": "419-437 Hackney Road E2",
        "description": "Close to Old Street and Liverpool Street stations, with the "
                       "Overground at Hoxton."}]}}]
    reply = "Close to Old Street and Liverpool Street stations."
    assert station_name_claims(reply) == ["Old Street and Liverpool Street"]
    assert ungrounded_station_names(reply, evidence) == []


def test_an_ampersand_name_matches_either_spelling():
    """A9, verbatim: '~10 min walk to Highbury & Islington Station'. TfL spells it with
    '&', a listing may spell it 'and'; the same station either way."""
    reply = "~10 min walk to Highbury & Islington Station and Upper Street"
    assert station_name_claims(reply) == ["Highbury & Islington"]
    assert ungrounded_station_names(
        reply, [{"nearest_station": {"name": "Highbury and Islington"}}]) == []
    assert ungrounded_station_names(
        reply, [{"nearest_station": {"name": "Highbury & Islington"}}]) == []
    # ...but it is still caught when nothing supplied it.
    assert ungrounded_station_names(reply, [{"area": "Camden"}]) == ["Highbury & Islington"]


def test_a_list_of_stations_flags_only_the_invented_one():
    """Precision matters: flagging the whole run would send a correct name back for
    rewriting along with the invented one."""
    evidence = [{"nearest_station": {"name": "Russell Square Underground Station"}}]
    reply = "The nearest stations are Russell Square and Covent Garden."
    assert ungrounded_station_names(reply, evidence) == ["Covent Garden"]
