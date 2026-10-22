"""A degenerate model answer must not stall the process (2026-10-19 B-01, P0).

A message carrying a 1 500-3 000 digit "flat number" made the model emit 8 192 tokens of
digits (finish_reason=length). ``validate_commute_response`` then ran superlinear regexes over
that answer — ``_minute_claim_values`` and ``_MINUTE_TOKEN`` retried at every digit of the run,
``_FRAGMENT_SPLIT``/``_PARENTHETICAL`` at every blank of a long blank run — synchronously on
the event loop: ~100 s with no request served, /health timing out, another user's 1.3 s tool
turn taking 83 s. Offline: 2 000 chars 0.28 s, 6 000 chars 2.26 s, 12 000 chars 9.33 s.

The limits below are ~100x looser than the fixed code needs and ~50x tighter than the old
code took, so they catch the quadratic class without flaking on a slow CI runner.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage

import core.agent_loop as agent_loop
from core import candidate_validation as cv
from tests.test_agent_contract_guards import Chat, Provider, _state

_B01 = "How safe is Flat " + "9" * 30_000 + " Mallory Road, London?"


def _commute_state():
    return {
        "extracted_context": {"current_message": "commute to King's Cross under 30 minutes?",
                              "reply_language": "en"},
        "accumulated_search_criteria": {"max_travel_time": 30},
        "tool_artifacts": [{"turn": 1, "tool": "calculate_commute", "success": True,
                            "params": {"origin": "12 Mallory Road, London NW8",
                                       "destination": "King's Cross"},
                            "raw_data": {"origin": "12 Mallory Road, London NW8",
                                         "destination": "King's Cross",
                                         "duration_minutes": 22}}],
        "candidate_validation": {"constraints": {"max_commute_minutes": 30}, "statuses": [
            {"candidate": {"address": "12 Mallory Road, London NW8", "price": "£1,900"},
             "status": "unknown", "evidence_status": "missing", "reasons": [],
             "unknown_reasons": ["commute evidence is missing"]}]},
    }


_SHAPES = {
    "b01-digit-run": _B01,
    "digits-then-minutes": "The commute takes " + "9" * 30_000 + " minutes",
    "blank-run": "commute 25" + " " * 30_000 + "minutes",
    "blank-run-then-paren": "commute takes 25 minutes" + " " * 30_000 + "x (17 min)",
    "many-conjunctions": "12 Mallory Road takes 25 minutes"
                         + " and 12 Mallory Road" * 1_500,
}


@pytest.mark.parametrize("label", sorted(_SHAPES))
@pytest.mark.parametrize("state_factory", [lambda: {"tool_artifacts": [], "extracted_context": {}},
                                           _commute_state], ids=["no-evidence", "evidence"])
def test_the_validator_is_fast_on_degenerate_answers(label, state_factory):
    started = time.perf_counter()
    out = cv.validate_commute_response(_SHAPES[label], state_factory())
    assert time.perf_counter() - started < 1.5, label
    assert "9" * 100 not in out           # a degenerate run never ships verbatim


@pytest.mark.parametrize("text", ["9" * 40_000, "9" * 40_000 + " minutes",
                                  "1" + " " * 40_000 + "x", "1.5" * 10_000 + " min"])
def test_the_helpers_are_linear_on_their_own(text):
    for helper in (cv._minute_claim_values, cv._clean_commute_fragments, cv._guard_text_parts,
                   cv._asserts_commute):
        started = time.perf_counter()
        helper(text)
        assert time.perf_counter() - started < 1.0, helper.__name__


def test_the_anchored_patterns_still_read_every_real_figure():
    text = ("It takes 23 minutes, then a 1.5 min walk; your 45-minute limit; 30mins; "
            "约 18 分钟; total 120 minutes")
    assert cv._minute_claim_values(text) == [23.0, 1.5, 45.0, 30.0, 18.0, 120.0]
    assert cv._asserts_commute("The flat is 17 min from Canary Wharf")
    assert cv._guard_text_parts("A - B")[1].strip() == "-"
    assert cv._PARENTHETICAL.sub("", "Oak Row (17 min to UCL) is cheap") == "Oak Row is cheap"


def test_the_input_bound_collapses_runs_and_caps_length():
    bounded = cv.bound_validator_input(_B01)
    assert "9" * 13 not in bounded and "Mallory Road" in bounded
    assert cv.bound_validator_input("call 020 7946 0958, flat 12") == "call 020 7946 0958, flat 12"
    huge = ("line of ordinary prose\n" * 3_000)
    assert len(cv.bound_validator_input(huge)) <= cv.VALIDATOR_MAX_CHARS + 2


def test_validation_runs_off_the_event_loop(monkeypatch):
    """A slow validator must not stop other coroutines from running."""
    def slow(text, state):
        time.sleep(0.4)      # blocking, like a CPU-bound regex
        return text

    monkeypatch.setattr(agent_loop, "validate_commute_response", slow)

    async def main():
        ticks = []

        async def ticker():
            for _ in range(8):
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.02)

        started = time.perf_counter()
        result, _ = await asyncio.gather(
            agent_loop._validate_commute_off_loop("answer", _state()), ticker())
        return result, ticks, started

    result, ticks, started = asyncio.run(main())
    assert result == "answer"
    # On the loop, the ticker could not have run until the 0.4 s sleep finished.
    assert ticks[-1] - started < 0.35


def test_a_validator_that_never_returns_fails_closed(monkeypatch):
    monkeypatch.setattr(agent_loop, "_COMMUTE_VALIDATION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(agent_loop, "validate_commute_response",
                        lambda text, state: time.sleep(0.5) or text)
    out = asyncio.run(agent_loop._validate_commute_off_loop("It takes 99 minutes.", _state()))
    assert "could not be verified" in out


def test_the_agent_node_ships_a_bounded_answer_for_the_b01_output():
    nodes = agent_loop.build_fc_nodes(Provider([]), agent_llm=Chat(AIMessage(content=_B01)))
    state = _state(_B01)
    started = time.perf_counter()
    command = asyncio.run(nodes["agent"](state))
    assert time.perf_counter() - started < 3.0
    final = (command.update or {}).get("final_response") or ""
    assert "9" * 100 not in final


# ── the same class on the INPUT side ─────────────────────────────────────────
# The B-01 message itself (1 500-3 000 digits) and any long paste go through the
# deterministic criteria parsers before the model is called, also on the event loop.
# Measured before the fix with a 6 000-character message: 17.9 s (digits), 10.7 s
# (unpunctuated prose); 28 000 characters of repeated Chinese criteria: 11.4 s, and still 6 s
# after it (area_list_edit and _extract_bedrooms re-read the whole prefix per mention) — 8.01 s
# once under full-suite load against an absolute 8 s limit (r6e). Each message is now parsed
# at half and at full size: the full size must stay far inside the limit, and doubling the
# message must cost about twice the time, not four times, so a quadratic regression fails
# here long before it could make CI flaky.
_LONG_MESSAGES = {
    "b01-digits": lambda k: "How safe is Flat " + "9" * (15_000 * k) + " Mallory Road, London?",
    "unpunctuated-prose": lambda k: "i want a flat near the park with a garden " * (350 * k),
    "comma-list": lambda k: "camden, hackney, islington, " * (550 * k),
    "zh-repeated-criteria": lambda k: "我想在卡姆登找一个两居室预算每月两千镑以内" * (650 * k),
    "digit-commas": lambda k: "£" + "1," * (7_500 * k),
}


def _parse_seconds(text: str) -> float:
    from core.constraint_updates import housing_update_text, scoped_update_text
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.search_readiness import detect_relative_budget_delta

    started = time.perf_counter()
    scoped_update_text(text)
    housing_update_text(text)
    detect_relative_budget_delta(text)
    _apply_explicit_criteria_updates({}, text)
    nodes = agent_loop.build_fc_nodes(Provider([]), agent_llm=Chat(AIMessage(content="ok")))
    asyncio.run(nodes["agent"](_state(text)))
    return time.perf_counter() - started


def test_the_b01_message_is_the_one_measured():
    assert _LONG_MESSAGES["b01-digits"](2) == _B01


@pytest.mark.parametrize("label", sorted(_LONG_MESSAGES))
def test_a_long_message_is_parsed_in_linear_time(label):
    build = _LONG_MESSAGES[label]
    _parse_seconds(build(1))                     # warm: patterns compiled, imports done
    # the faster of two runs at each size, so one noisy run cannot decide the ratio
    half = min(_parse_seconds(build(1)) for _ in range(2))
    full = min(_parse_seconds(build(2)) for _ in range(2))
    assert full < 8.0, (label, full)
    assert full < 2.5 * half + 0.5, (label, half, full)     # linear ~2x; quadratic ~4x


def test_the_anchored_input_patterns_keep_their_meaning():
    from core.constraint_updates import clause_at, mention_is_requirement, scoped_update_text
    from core.search_readiness import detect_relative_budget_delta

    assert scoped_update_text("Ignore the Watford plan. Search Camden under £2,000") \
        == " Search Camden under £2,000"
    assert scoped_update_text("Keep Hackney out of this decision; find 1-bed in Camden") \
        == " find 1-bed in Camden"
    assert detect_relative_budget_delta("raise the budget by £1,200") == 1200
    assert detect_relative_budget_delta("£200 more a month is fine") == 200
    assert detect_relative_budget_delta("raise the budget to £2,400") is None
    text = "I live in Leeds, but find me a flat in Camden"
    start = text.index("Camden")
    assert clause_at(text, start, start + 6) == " but find me a flat in Camden"
    assert mention_is_requirement(text, start, start + 6)
    neg = "not in Camden"
    assert not mention_is_requirement(neg, neg.index("Camden"), len(neg))
