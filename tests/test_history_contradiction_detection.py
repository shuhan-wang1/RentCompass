"""Defect G11 — mutually inconsistent stated facts in history must be FLAGGED, not picked.

Measured on the 8793c0b internal round: history held "My absolute max is £1200 per
month." and, two turns later, "My budget is £1200 per week, by the way." The answer
silently adopted the later one — "within your £1,200/week budget" — with no detection,
no flag and no question. £1200/week is ~£5,200/month, so the search ran against a figure
the user may never have meant. The case's failure condition names exactly this:
"Silently picks one of the two budgets ... and searches as if it were settled."

THE UPDATE-vs-CONTRADICTION RULE under test (both directions, because over-flagging a
user who is allowed to change their mind would be worse than the bug):

  a later statement is an UPDATE (no flag) when it carries a revision marker
  ("actually", "make it", 「改成」) OR restates the field in the SAME unit;

  it is a CONTRADICTION (flag) when neither holds and the pair cannot both be true —
  identical value with a different unit (unit_ambiguity), different units whose
  monthly-normalised amounts differ by >= 1.5x (incompatible_magnitude), or a later
  amount that breaks an earlier ABSOLUTE ceiling (absolute_violated).

An amount the user never qualified with a period is never comparable, so it never
raises a flag.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
    """See test_agent_memory_isolation._pin_app — ``tests/`` shadows ``core``/``rag``."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]


_pin_app()

ca = importlib.import_module("core.context_assembler")


def _call(name, *args, **kwargs):
    """Resolved at CALL time so this module still COLLECTS against an assembler that has
    no detector — the assertions must fail on the old behaviour, not the import."""
    fn = getattr(ca, name, None)
    assert fn is not None, f"core.context_assembler exposes no {name}()"
    return fn(*args, **kwargs)


# The literal production strings from benchmark case G11 (evaluation/benchmark/
# cases_ext_FG.jsonl), pinned verbatim — the defect was reported against THESE.
G11_MONTH = "My absolute max is £1200 per month."
G11_WEEK = "My budget is £1200 per week, by the way."
G11_QUERY = "Find me somewhere within my budget."
G11_HISTORY = [
    {"user": G11_MONTH, "assistant": "Noted — £1200/month ceiling."},
    {"user": G11_WEEK, "assistant": "Okay."},
]


# ----------------------------------------------------- the G11 regression itself

def test_g11_month_vs_week_is_detected_as_a_contradiction():
    """THE defect. Old behaviour: nothing detected, the answer said "within your
    £1,200/week budget"."""
    conflicts = _call("detect_history_conflicts", G11_HISTORY, G11_QUERY)

    assert len(conflicts) == 1, f"£1200/month vs £1200/week not detected: {conflicts}"
    c = conflicts[0]
    assert c["field"] == "budget"
    assert c["kind"] == "unit_ambiguity"
    assert (c["earlier"]["value"], c["earlier"]["unit"]) == (1200.0, "month")
    assert (c["later"]["value"], c["later"]["unit"]) == (1200.0, "week")
    # The ~4.3x the case notes call out.
    assert 4.3 <= c["ratio"] <= 4.35


def test_g11_conflict_is_surfaced_in_the_fc_message_array():
    """The detection has to reach the model, not just exist."""
    msgs = _call("assemble_messages", user_message=G11_QUERY, history=G11_HISTORY,
                 context_block={}, reply_language="en")
    blob = "\n".join(m.content or "" for m in msgs)

    assert "UNRESOLVED CONTRADICTION" in blob
    assert "£1200 per month" in blob and "£1200 per week" in blob
    assert "ASK the user which one applies" in blob
    # The silent-pick the answer made must be explicitly forbidden.
    assert "Do NOT assume the later statement supersedes the earlier one" in blob


def test_g11_conflict_is_surfaced_in_the_legacy_string_path():
    """Both architectures. Legacy assembles one string; the block must be in it."""
    out = _call("assemble", user_message=G11_QUERY, history=G11_HISTORY)

    assert "UNRESOLVED CONTRADICTION" in out
    assert "£1200 per month" in out and "£1200 per week" in out


def test_g11_yields_a_ready_made_clarification_decision():
    """A source guard on the ROUTE, not only a prompt instruction: the router can turn the
    detection into a deterministic question with one call."""
    decision = _call("history_conflict_decision", G11_HISTORY, G11_QUERY, "en")

    assert decision is not None
    assert decision["tool"] == "clarification"
    msg = decision["clarification_message"]
    assert "£1200 per month" in msg and "£1200 per week" in msg
    assert "?" in msg
    assert "unit_ambiguity" in decision["reason"]


def test_no_conflict_returns_no_decision_and_no_section():
    """The normal case must be untouched — no section, no route change."""
    history = [{"user": "I need a 2-bed near UCL", "assistant": "Sure"}]

    assert _call("detect_history_conflicts", history, "find me one") == []
    assert _call("history_conflict_decision", history, "find me one") is None
    assert _call("render_history_conflicts", []) == ""
    out = _call("assemble", user_message="find me one", history=history)
    assert "UNRESOLVED CONTRADICTION" not in out


# ------------------------------------------- direction 1: a CONTRADICTION must flag

@pytest.mark.parametrize("earlier,later,kind", [
    # (U) identical value, different unit — G11's shape.
    ("My absolute max is £1200 per month.", "My budget is £1200 per week.",
     "unit_ambiguity"),
    ("My budget is £1200 per month.", "My budget is £1200 per week.",
     "unit_ambiguity"),
    # (M) different units AND different values, >= 1.5x apart once normalised.
    ("My budget is £1200 per month.", "My budget is £2000 per week.",
     "incompatible_magnitude"),
    # (A) a later amount breaking an earlier absolute ceiling, same unit, no marker.
    ("My absolute max is £1200 per month.", "My budget is £1800 per month.",
     "absolute_violated"),
    ("I can spend no more than £1200 pcm.", "My budget is £2000 pcm.",
     "absolute_violated"),
    # zh, both rules.
    ("我的预算上限是每月1200镑", "我的预算是每周1200镑", "unit_ambiguity"),
])
def test_contradictions_are_flagged(earlier, later, kind):
    conflicts = _call("detect_history_conflicts",
                      [{"user": earlier, "assistant": "ok"},
                       {"user": later, "assistant": "ok"}], "find me a place")
    assert len(conflicts) == 1, f"not flagged: {earlier!r} vs {later!r}"
    assert conflicts[0]["kind"] == kind


# ------------------------------------------------ direction 2: an UPDATE must NOT flag

@pytest.mark.parametrize("earlier,later,why", [
    # (1) an explicit revision marker — the user announced the change.
    ("My absolute max is £1200 per month.", "Actually, make it £1200 per week.",
     "revision marker across units"),
    ("My absolute max is £1200 per month.", "Actually my budget is £1800 per month now.",
     "revision marker over an absolute"),
    ("My budget is £1200 per month.", "I meant £300 per week.",
     "revision marker, 'I meant'"),
    ("我的预算是每月1200镑", "改成每周1200镑", "zh revision marker"),
    # (2) a same-unit restatement — the normal way to move a number.
    ("My budget is £1200 per month.", "My budget is £1500 per month.",
     "same unit, no absolute framing"),
    ("My budget is £1200 pcm.", "My budget is £900 pcm.",
     "same unit, revised downwards"),
    # a consistent cross-unit restatement is a refinement, not a conflict.
    ("My budget is £1200 per month.", "That's about £280 per week for rent.",
     "cross-unit restatement within 1.5x"),
    # an unqualified amount is not a comparable quantity (deposits, fees).
    ("My absolute max is £1200 pcm.", "The deposit on my budget list is £1800.",
     "later amount has no period unit"),
    # identical restatement.
    ("My budget is £1200 per month.", "Remember, my budget is £1200 per month.",
     "identical"),
    # a single field mention can never conflict with itself.
    ("I need a 2-bed near UCL.", "My budget is £1200 per month.",
     "only one budget statement"),
])
def test_updates_are_not_flagged(earlier, later, why):
    conflicts = _call("detect_history_conflicts",
                      [{"user": earlier, "assistant": "ok"},
                       {"user": later, "assistant": "ok"}], "find me a place")
    assert conflicts == [], f"an UPDATE was mis-flagged as a conflict ({why}): {conflicts}"


# ------------------------------------------------------------------- narrow scope

def test_only_user_turns_count_never_the_assistant_echo():
    """The assistant repeating a figure is not the user stating it."""
    history = [
        {"user": "My budget is £1200 per month.",
         "assistant": "Some places go for £1200 per week in that area."},
        {"user": "ok", "assistant": "ok"},
    ]
    assert _call("detect_history_conflicts", history, "find me a place") == []


def test_a_conflict_between_history_and_the_current_message_is_caught():
    """The current message is the newest user turn, not an exempt one."""
    conflicts = _call("detect_history_conflicts",
                      [{"user": G11_MONTH, "assistant": "ok"}],
                      "My budget is £1200 per week — find me somewhere.")
    assert len(conflicts) == 1 and conflicts[0]["kind"] == "unit_ambiguity"


def test_small_numbers_are_not_budgets():
    """Bedroom counts and commute minutes must never become a money conflict."""
    history = [
        {"user": "My budget allows 2 bedrooms per month", "assistant": "ok"},
        {"user": "and 35 per week is fine for my budget", "assistant": "ok"},
    ]
    assert _call("detect_history_conflicts", history, "find me a place") == []


def test_at_most_one_conflict_per_field():
    """Three inconsistent statements must not produce a wall of duplicate flags."""
    history = [
        {"user": "My absolute max is £1200 per month.", "assistant": "ok"},
        {"user": "My budget is £1200 per week.", "assistant": "ok"},
        {"user": "My budget is £1200 per year.", "assistant": "ok"},
    ]
    conflicts = _call("detect_history_conflicts", history, "find me a place")
    assert len(conflicts) == 1


def test_empty_and_malformed_history_are_safe():
    for history in (None, [], [None], ["not a dict"], [{}], [{"user": None}]):
        assert _call("detect_history_conflicts", history, "") == []
    assert _call("history_conflict_decision", None, "") is None


def test_conflict_question_is_bilingual():
    en = _call("conflict_question",
               _call("detect_history_conflicts", G11_HISTORY, G11_QUERY), "en")
    zh = _call("conflict_question",
               _call("detect_history_conflicts", G11_HISTORY, G11_QUERY), "zh")
    assert "Which should I use?" in en
    assert "预算" in zh and "£1200 per week" in zh
    assert _call("conflict_question", [], "en") == ""
