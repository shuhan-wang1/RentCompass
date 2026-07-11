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
    append_caveat,
    enforce_grounding,
    evaluate_grounding,
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
    monkeypatch.setattr(llm_config, "get_react_llm", lambda: fake)

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
    monkeypatch.setattr(llm_config, "get_react_llm", lambda: fake)

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
    monkeypatch.setattr(llm_config, "get_react_llm", lambda: fake)

    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("what's my budget again?")
    state["tool_decision"] = {"tool": "direct_answer"}
    state["final_response"] = "Your budget is £1,200 pcm."
    update = _run(_make_critic_node()(state))

    assert fake.calls == 0  # no gating, no regeneration
    assert "final_response" not in update  # answer unchanged
    assert update["verdict"]["grounded"] is True
