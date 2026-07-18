"""Tests for app/core/context_assembler.py.

Parity tests lock the exact query strings today's app.py produces (copied from the
current logic in handle_with_react_agent, lines ~1059-1114). The rest cover the
token budget, snapshot whitelist/round-trip, and rolling-summary fallback.
"""

import math

import pytest

from core.context_assembler import (
    CONTEXT_SCHEMA_VERSION,
    SnapshotSchemaError,
    assemble,
    build_turn_snapshot,
    estimate_tokens,
    should_update_summary,
    snapshot_to_session_patch,
    update_rolling_summary,
)


# ---------------------------------------------------------------------------
# assemble() parity
# ---------------------------------------------------------------------------

def test_property_context_short_circuit():
    # When property context is present, the base query is just the user_message.
    out = assemble(
        user_message="Is this pet friendly?",
        history=[{"user": "hi", "assistant": "hello"}],
        has_property_context=True,
    )
    assert out == "Is this pet friendly?"


def test_property_context_still_gets_memory_prefix():
    # Parity: today's code prefixes memory even in the property-context path.
    out = assemble(
        user_message="Is this pet friendly?",
        history=[],
        memory_block="MEM",
        has_property_context=True,
    )
    assert out == "MEM\n\nIs this pet friendly?"


def test_no_history_plain_returns_user_message():
    out = assemble(user_message="find me a flat near UCL", history=[])
    assert out == "find me a flat near UCL"


def test_history_wrapper_exact_parity():
    history = [
        {"user": "u1", "assistant": "a1"},
        {"user": "u2", "assistant": "a2"},
        {"user": "u3", "assistant": "a3"},
    ]
    user_message = "what about 2 bed flats in a completely different area please now"
    out = assemble(user_message=user_message, history=history)

    history_text = "User: u1\nAlex: a1\nUser: u2\nAlex: a2\nUser: u3\nAlex: a3"
    expected = (
        f"Previous conversation:\n{history_text}\n\n"
        f"Current user message: {user_message}"
    )
    assert out == expected


def test_history_wrapper_uses_last_3_turns_only():
    history = [{"user": f"u{i}", "assistant": f"a{i}"} for i in range(1, 6)]
    out = assemble(user_message="longer message that is not a short reply here", history=history)
    # Only the last 3 turns are included.
    assert "u3" in out and "u4" in out and "u5" in out
    assert "u1" not in out and "u2" not in out


def test_clarification_wrapper_exact_parity():
    history = [
        {"user": "u1", "assistant": "a1"},
        {"user": "u2", "assistant": "Which area do you prefer?"},
    ]
    user_message = "Camden please"  # <= 5 words
    out = assemble(user_message=user_message, history=history)

    history_text = "User: u1\nAlex: a1\nUser: u2\nAlex: Which area do you prefer?"
    expected = (
        "Previous conversation (IMPORTANT - user is answering a clarification "
        "question):\n"
        f"{history_text}\n\n"
        f"User's answer to the clarification question: {user_message}\n\n"
        "INSTRUCTIONS: The user just answered your clarification question. Use "
        "their answer to complete the ORIGINAL request. Do NOT ask more questions "
        "about the same thing. Do NOT treat their answer as a confusing new command."
    )
    assert out == expected


def test_clarification_detected_via_question_mark():
    history = [{"user": "u1", "assistant": "Sure, anything else?"}]
    out = assemble(user_message="no thanks", history=history)
    assert out.startswith("Previous conversation (IMPORTANT")


def test_clarification_not_triggered_when_reply_too_long():
    # last reply is a clarification, but the answer is > 5 words → plain wrapper.
    history = [{"user": "u1", "assistant": "What is your budget?"}]
    out = assemble(
        user_message="my budget is around fifteen hundred pounds a month roughly",
        history=history,
    )
    assert out.startswith("Previous conversation:\n")


def test_memory_prefix_ordering_and_summary_placement():
    history = [{"user": "u1", "assistant": "reply one two three four five"}]
    out = assemble(
        user_message="show me more options in a nearby cheaper area instead",
        history=history,
        memory_block="MEMORYBLOCK",
        rolling_summary="SUMMARYTEXT",
    )
    # memory then summary then history block.
    assert out.startswith("MEMORYBLOCK\n\n")
    idx_mem = out.index("MEMORYBLOCK")
    idx_summary = out.index("Earlier conversation summary:\nSUMMARYTEXT")
    idx_history = out.index("Previous conversation:")
    assert idx_mem < idx_summary < idx_history


def test_summary_not_inserted_without_history_context():
    # plain mode (no history) → summary section is not added.
    out = assemble(
        user_message="find a flat",
        history=[],
        memory_block="MEM",
        rolling_summary="SUMMARY",
    )
    assert "Earlier conversation summary" not in out
    assert out == "MEM\n\nfind a flat"


# ---------------------------------------------------------------------------
# Token budget / trimming
# ---------------------------------------------------------------------------

def test_estimate_tokens_cjk_weighting():
    assert estimate_tokens("你好") == 2                 # 2 CJK, 0 other
    assert estimate_tokens("abcd") == 1                 # ceil(4/4)
    assert estimate_tokens("abcde") == 2                # ceil(5/4)
    assert estimate_tokens("你好abcd") == 2 + 1          # 2 CJK + ceil(4/4)
    assert estimate_tokens("") == 0


def test_trimming_reduces_history_turns_first():
    # 5 fat turns, small budget → history turns reduced but user_message survives.
    # Sized so 5 turns blow the budget but the 2-turn floor fits under it.
    filler = "word " * 30
    history = [{"user": f"u{i} {filler}", "assistant": f"a{i} {filler}"}
               for i in range(1, 6)]
    user_message = "UNIQUEUSERMSG please"  # clarification-ish (short)
    # make it a clarification path so initial=5 turns
    history[-1]["assistant"] = "Which area do you prefer? " + filler
    out = assemble(user_message=user_message, history=history, token_budget=300)
    assert "UNIQUEUSERMSG" in out
    assert estimate_tokens(out) <= 300


def test_user_message_never_trimmed_even_when_over_budget():
    huge_user = "KEEPME " * 500
    out = assemble(user_message=huge_user, history=[], token_budget=10)
    # user_message survives verbatim even though it blows the budget.
    assert huge_user == out


def test_memory_dropped_when_still_over_budget():
    huge_mem = "memline\n" * 2000
    history = [{"user": "u1", "assistant": "a1"}]
    user_message = "a modest current user message that must survive intact here ok"
    out = assemble(
        user_message=user_message,
        history=history,
        memory_block=huge_mem,
        token_budget=50,
    )
    assert user_message in out
    # memory should have been capped or dropped to try to fit.
    assert out.count("memline") < 2000


def test_within_budget_no_trimming_is_byte_identical():
    history = [
        {"user": "u1", "assistant": "a1"},
        {"user": "u2", "assistant": "a2"},
        {"user": "u3", "assistant": "a3"},
    ]
    user_message = "a normal length message that will not trigger any trimming here"
    out = assemble(user_message=user_message, history=history, token_budget=6000)
    history_text = "User: u1\nAlex: a1\nUser: u2\nAlex: a2\nUser: u3\nAlex: a3"
    expected = (
        f"Previous conversation:\n{history_text}\n\n"
        f"Current user message: {user_message}"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Snapshot build / apply
# ---------------------------------------------------------------------------

def _persistent_state_with_transients():
    return {
        "user_preferences": {"lang": "zh", "pets": True},
        "accumulated_search_criteria": {"budget": 1500, "area": "camden"},
        "extracted_context": {
            "last_results": [{"id": 1}, {"id": 2}],
            "rolling_summary": "Goals: find a flat",
            "rolling_summary_through_turn_id": "turn-abc",
            # transient keys that MUST NOT leak:
            "run_id": "r1",
            "request_id": "req1",
            "tool_decision": {"x": 1},
            "tool_observation": "obs",
            "loop_turn": 3,
            "observations": ["o1"],
            "task_plan": {"p": 1},
            "task_results": ["tr"],
            "critic_attempts": 2,
            "verdict": "ok",
            "current_message": "hi",
            "memory_context": "mc",
            "reply_language": "zh",
            "previous_search_results": "rendered text",
            "comparison_properties": "cmp",
            "property_address": "1 Main St",
            "property_price": "£1500",
            "viewed_properties": ["v1"],
        },
    }


def test_snapshot_whitelist_blacklist():
    snap = build_turn_snapshot(
        turn_id="t1", persistent_state=_persistent_state_with_transients(),
        context_revision=4)

    assert snap["schema_version"] == CONTEXT_SCHEMA_VERSION
    assert snap["turn_id"] == "t1"
    assert snap["context_revision"] == 4
    assert snap["user_preferences"] == {"lang": "zh", "pets": True}
    assert snap["accumulated_search_criteria"] == {"budget": 1500, "area": "camden"}
    assert snap["last_results"] == [{"id": 1}, {"id": 2}]
    assert snap["summary"] == "Goals: find a flat"
    assert snap["summary_through_turn_id"] == "turn-abc"
    assert snap["open_questions"] == []
    assert snap["active_property"] is None

    allowed = {
        "schema_version", "turn_id", "user_preferences",
        "accumulated_search_criteria", "last_results", "summary",
        "summary_through_turn_id", "open_questions", "active_property",
        "context_revision",
    }
    assert set(snap.keys()) == allowed

    # None of the transient keys leaked anywhere in the snapshot.
    forbidden = [
        "run_id", "request_id", "tool_decision", "tool_observation", "loop_turn",
        "observations", "task_plan", "task_results", "critic_attempts", "verdict",
        "current_message", "memory_context", "reply_language",
        "previous_search_results", "comparison_properties", "property_address",
        "property_price", "viewed_properties",
    ]
    for key in forbidden:
        assert key not in snap


def test_snapshot_deepcopies_source():
    state = _persistent_state_with_transients()
    snap = build_turn_snapshot(turn_id="t1", persistent_state=state)
    snap["user_preferences"]["lang"] = "en"
    snap["last_results"].append({"id": 99})
    # Source must be untouched.
    assert state["user_preferences"]["lang"] == "zh"
    assert state["extracted_context"]["last_results"] == [{"id": 1}, {"id": 2}]


def test_snapshot_defaults_when_context_missing():
    snap = build_turn_snapshot(
        turn_id="t1",
        persistent_state={"user_preferences": {}, "accumulated_search_criteria": {}},
    )
    assert snap["last_results"] == []
    assert snap["summary"] is None
    assert snap["summary_through_turn_id"] is None


def test_snapshot_round_trip():
    snap = build_turn_snapshot(
        turn_id="t1", persistent_state=_persistent_state_with_transients())
    patch = snapshot_to_session_patch(snap)
    assert patch["user_preferences"] == {"lang": "zh", "pets": True}
    assert patch["accumulated_search_criteria"] == {"budget": 1500, "area": "camden"}
    assert patch["last_results"] == [{"id": 1}, {"id": 2}]
    assert patch["rolling_summary"] == "Goals: find a flat"
    assert patch["rolling_summary_through_turn_id"] == "turn-abc"


def test_snapshot_patch_rejects_unknown_schema():
    with pytest.raises(SnapshotSchemaError):
        snapshot_to_session_patch({"schema_version": 999})
    with pytest.raises(SnapshotSchemaError):
        snapshot_to_session_patch("not a dict")


def test_snapshot_patch_sanitizes_malformed_content():
    patch = snapshot_to_session_patch({
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "user_preferences": "not a dict",
        "accumulated_search_criteria": 12345,
        "last_results": "not a list",
        "summary": 999,               # not a str
        "summary_through_turn_id": "",  # empty → None
    })
    assert patch["user_preferences"] == {}
    assert patch["accumulated_search_criteria"] == {}
    assert patch["last_results"] == []
    assert patch["rolling_summary"] is None
    assert patch["rolling_summary_through_turn_id"] is None


def test_snapshot_patch_tolerates_missing_keys():
    patch = snapshot_to_session_patch({"schema_version": CONTEXT_SCHEMA_VERSION})
    assert patch == {
        "user_preferences": {},
        "accumulated_search_criteria": {},
        "last_results": [],
        "rolling_summary": None,
        "rolling_summary_through_turn_id": None,
    }


# ---------------------------------------------------------------------------
# Rolling summary
# ---------------------------------------------------------------------------

def test_should_update_summary():
    assert should_update_summary(10, 10) is True
    assert should_update_summary(11, 10) is True
    assert should_update_summary(9, 10) is False


def test_update_rolling_summary_happy_path():
    captured = {}

    def fake_llm(prompt):
        captured["prompt"] = prompt
        return "  Goals: find a 1-bed near UCL\nHard criteria: budget £1500 (turn 1)  "

    out = update_rolling_summary(fake_llm, "prior", [{"user": "u", "assistant": "a"}])
    assert out == "Goals: find a 1-bed near UCL\nHard criteria: budget £1500 (turn 1)"
    # The prompt merges prior summary and folded turns.
    assert "prior" in captured["prompt"]
    assert "Hard criteria" in captured["prompt"]


def test_update_rolling_summary_truncates_to_1600():
    out = update_rolling_summary(lambda p: "x" * 5000, None, [])
    assert len(out) == 1600


def test_update_rolling_summary_fallback_on_exception():
    def boom(prompt):
        raise RuntimeError("llm down")

    assert update_rolling_summary(boom, "PRIOR", []) == "PRIOR"


def test_update_rolling_summary_fallback_on_empty_output():
    assert update_rolling_summary(lambda p: "   ", "PRIOR", []) == "PRIOR"
    assert update_rolling_summary(lambda p: None, "PRIOR", []) == "PRIOR"


def test_update_rolling_summary_language_hint():
    captured = {}

    def fake_llm(prompt):
        captured["prompt"] = prompt
        return "summary"

    update_rolling_summary(fake_llm, None, [], reply_language="zh")
    assert "Chinese" in captured["prompt"]
    update_rolling_summary(fake_llm, None, [], reply_language="en")
    assert "English" in captured["prompt"]
