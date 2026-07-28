"""A turn with no budget left is the turn most likely to have invented something.

Production, 2026-07-27, one real session, two turns, same user:

  * the UNwrapped turn was graded and shipped with the price caveat appended;
  * the SOFT-WRAPPED turn asserted tube lines and journey times for four areas
    ("Camden — Northern Line, 20 min", "Holloway — Piccadilly Line, 25 min", ...) with
    no TfL call behind any of them, plus an invented "£200-£300/week" range — and
    shipped with **no caveat at all**.

The wrapped turn was the one that skipped the check. `_wrap_up` routed straight to
`format_output_fc`, bypassing the critic node, so the fabricated-price and
ungrounded-station checks never ran on it.

The stated reason was cost — "a 3s critic at t~=40 is pointless when the turn is already
out of budget" — and that reason is sound for the CORRECTIVE REGENERATION, which is an
LLM round-trip. It was never true of the grading: everything before the `await` in
`enforce_grounding` is regex and arithmetic over one string. Skipping the whole node threw
the cheap half away with the expensive half, and exempted precisely the turns that had the
least evidence and no time to gather more.
"""

import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import pytest

from uk_rent_agent.agent.critic import (
    CAVEAT,
    STATION_CAVEAT,
    enforce_grounding,
)

# Evidence with ONE real price. Anything else the answer asserts is unsupported.
EVIDENCE = [{"address": "12 Test Road, London", "price_pcm": 1500}]


def _run(**kw):
    return asyncio.run(enforce_grounding(**kw))


_REGEN_CALLS: list = []


async def _recording_regen(correction):
    """Records rather than raises: `enforce_grounding` wraps the regeneration in a bare
    `except Exception` so a raised assertion would be silently swallowed and the test
    would pass for the wrong reason."""
    _REGEN_CALLS.append(correction)
    return "This one is £1,500 pcm."


# ══════════════════════════════════════════════════════════════════════════
# regenerate=None — grade, caveat, do NOT call the LLM
# ══════════════════════════════════════════════════════════════════════════

def test_an_ungrounded_answer_is_still_caveated_without_a_rewrite_budget():
    """The production failure: no budget must not mean no check."""
    out = _run(
        response="Rooms near UCL are typically £2,750 pcm.",
        evidence=EVIDENCE, regenerate=None,
    )
    assert not out.verdict.grounded, "fixture is wrong: this answer should fail grading"
    assert out.response.endswith(CAVEAT), (
        "an answer that failed the grounding check shipped verbatim, with nothing "
        "telling the reader a figure in it is unsupported"
    )
    assert out.regenerated is False
    assert out.attempts == 1


def test_no_llm_round_trip_happens_when_the_budget_is_gone():
    """The expensive half must still be skipped — that part of FIX 3 was correct.

    Proved in two steps so the assertion cannot pass vacuously: first show THIS input does
    reach the regeneration when a callable is supplied, then show `None` is what stops it.
    """
    bad = "Rooms near UCL are typically £2,750 pcm."

    _REGEN_CALLS.clear()
    _run(response=bad, evidence=EVIDENCE, regenerate=_recording_regen)
    assert _REGEN_CALLS, "fixture is vacuous: this input never reaches the regeneration"

    _REGEN_CALLS.clear()
    out = _run(response=bad, evidence=EVIDENCE, regenerate=None)
    assert not _REGEN_CALLS, "an LLM round-trip ran on a turn that had no budget for it"
    assert out.regenerated is False


def test_a_grounded_answer_is_untouched_without_a_rewrite_budget():
    out = _run(
        response="This one is £1,500 pcm.", evidence=EVIDENCE, regenerate=None,
    )
    assert out.verdict.grounded
    assert out.response == "This one is £1,500 pcm."
    assert CAVEAT not in out.response


def test_the_caveat_still_matches_what_actually_failed():
    """A station-only failure must not be delivered with the price caveat — the
    regenerate=None path must not lose `_caveat_for`'s discrimination."""
    out = _run(
        response="This one is £1,500 pcm, two minutes from Ravenscourt Junction.",
        evidence=EVIDENCE, regenerate=None,
    )
    if not out.verdict.grounded and any(
        i.startswith("ungrounded_stations:") for i in (out.verdict.issues or [])
    ) and not any(i.startswith("unsupported_prices:") for i in (out.verdict.issues or [])):
        assert out.response.endswith(STATION_CAVEAT)


# ══════════════════════════════════════════════════════════════════════════
# The regenerate path is unchanged
# ══════════════════════════════════════════════════════════════════════════

def test_with_a_budget_the_corrective_pass_still_runs():
    calls = []

    async def _regen(correction):
        calls.append(correction)
        return "This one is £1,500 pcm."

    out = _run(
        response="Rooms near UCL are typically £2,750 pcm.",
        evidence=EVIDENCE, regenerate=_regen,
    )
    assert calls, "the corrective regeneration stopped running when a budget WAS available"
    assert out.regenerated is True
    assert out.attempts == 2
    assert out.verdict.grounded


def test_a_regeneration_that_returns_nothing_still_caveats():
    async def _regen(_c):
        return ""

    out = _run(
        response="Rooms near UCL are typically £2,750 pcm.",
        evidence=EVIDENCE, regenerate=_regen,
    )
    assert out.response.endswith(CAVEAT)
    assert out.regenerated is True


# ══════════════════════════════════════════════════════════════════════════
# The routing change itself
# ══════════════════════════════════════════════════════════════════════════

def test_the_soft_wrap_routes_through_critic_not_around_it():
    """A source guard, because the routing is one string in a Command and a silent
    revert to `format_output_fc` would restore the production bug with every test above
    still passing."""
    src = open(os.path.join(_ROOT, "app", "core", "agent_loop.py"),
               encoding="utf-8").read()
    # Anchor on the state write that MAKES a turn wrapped — unique, and it is the same
    # flag critic_node reads to disable regeneration, so the two cannot drift apart.
    marker = src.index('"soft_wrapped": True')
    tail = src[marker:marker + 800]
    assert 'goto="critic"' in tail, (
        "the soft-wrap path no longer routes through the critic node — the wrapped turn "
        "is unguarded again"
    )
    assert 'goto="format_output_fc"' not in tail


def test_the_wrap_reserve_docstring_does_not_still_claim_the_critic_never_runs():
    """The docstring of _wrap_critic_reserve_s documented the OLD behaviour in prose.
    A stale declaration nobody compares to reality is this repo's recurring defect
    class, so it is asserted rather than trusted."""
    src = open(os.path.join(_ROOT, "app", "core", "agent_loop.py"),
               encoding="utf-8").read()
    assert "deliberately never runs the\n    critic" not in src
    assert "never runs the critic" not in src
