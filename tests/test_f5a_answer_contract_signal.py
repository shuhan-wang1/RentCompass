"""`answer_contract` reached no consumer at all (2026-09-03 review P2-1).

`manager_v1` builds a validated AnswerContract at the format boundary and writes it
into the graph's final_state — where nothing read it. The canary record did not carry
it, the API response did not expose it, and the limitation lines ("task X returned no
evidence") that explain a thin answer were therefore invisible to both the operator
and the client.

What is forwarded is a BOUNDED, CONTENT-FREE projection, and this file is the contract
for its exact shape, because the telemetry layer validates the same three keys:

    {"valid": bool, "limitations": [str], "final_response_truncated": bool}

At most 8 limitations, each at most 200 chars; the key is ABSENT (never null) on an
architecture that produced no contract, so legacy/fc_loop records are unchanged. The
answer TEXT, the evidence refs, the task ids and the root task id are deliberately
not forwarded: this object is read from an ops log.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import app as appmod
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore

MAX_LIMITATIONS = 8
MAX_LIMITATION_CHARS = 200


def _summary(final_state):
    return appmod._answer_contract_summary(final_state)


# ------------------------------------------------------------------- the shape

def test_absent_when_the_arch_produced_no_contract():
    for state in ({}, {"answer_contract": {}}, {"answer_contract": None},
                  {"answer_contract": "not-an-object"}, None, "nonsense"):
        assert _summary(state) is None
    # ...and therefore no key at all on the signals of an fc_loop turn.
    assert "answer_contract" not in appmod._build_fc_signals({})


def test_a_valid_contract_is_summarised_to_exactly_three_keys():
    summary = _summary({"answer_contract": {
        "root_task_id": "turn:abc",
        "response_type": "answer",
        "final_response": "Here are three flats in Camden ...",
        "final_response_chars": 34,
        "final_response_truncated": False,
        "used_task_ids": ["t1", "t2"],
        "evidence": [{"evidence_id": "e1"}],
        "limitations": ["listings: returned no evidence"],
    }})

    assert set(summary) == {"valid", "limitations", "final_response_truncated"}
    # The success shape carries no `valid` key — a VALIDATED contract is valid.
    assert summary["valid"] is True
    assert summary["limitations"] == ["listings: returned no evidence"]
    assert summary["final_response_truncated"] is False


def test_an_invalid_contract_says_so_and_keeps_its_limitations():
    summary = _summary({"answer_contract": {
        "valid": False,
        "error_code": "answer_contract_invalid",
        "limitations": ["area_evidence: failed (tool_error)"],
    }})
    assert summary["valid"] is False
    assert summary["limitations"] == ["area_evidence: failed (tool_error)"]


def test_truncation_is_reported_as_a_boolean():
    summary = _summary({"answer_contract": {"final_response_truncated": True,
                                            "limitations": []}})
    assert summary["final_response_truncated"] is True


def test_no_user_text_or_identifiers_leak_into_the_summary():
    """The whole point of a projection: the answer itself must not be in an ops log."""
    summary = _summary({"answer_contract": {
        "final_response": "SECRET-ANSWER-TEXT for a named tenant",
        "root_task_id": "turn:req-123",
        "evidence": [{"evidence_id": "e1", "snippet": "SECRET-EVIDENCE"}],
        "used_task_ids": ["turn:req-123/node:x"],
        "limitations": [],
    }})
    flattened = repr(summary)
    assert "SECRET-ANSWER-TEXT" not in flattened
    assert "SECRET-EVIDENCE" not in flattened
    assert "req-123" not in flattened


# ------------------------------------------------------------------ the bounds

def test_limitations_are_capped_in_count_and_length():
    summary = _summary({"answer_contract": {
        "limitations": [f"{i}:" + "x" * 500 for i in range(50)],
    }})
    assert len(summary["limitations"]) == MAX_LIMITATIONS
    assert all(len(line) <= MAX_LIMITATION_CHARS for line in summary["limitations"])


@pytest.mark.parametrize("limitations", [None, "a string", 42, [1, 2, 3], ["", "   "]])
def test_a_malformed_limitations_field_degrades_to_an_empty_list(limitations):
    summary = _summary({"answer_contract": {"limitations": limitations}})
    assert summary["limitations"] == []


# ------------------------------------------------------------- the API response

@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    monkeypatch.setattr(appmod, "conversation_store", store)
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    monkeypatch.setattr(
        appmod, "_runtime_config",
        replace(appmod._runtime_config, allow_legacy_client_user_id=True))
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True)
    yield appmod.app.test_client()
    store.close()


def _agent_publishing(signals):
    async def fake_agent(user_message, *a, **k):
        appmod._turn_fc_signals.set(signals)
        return {"response_type": "chat", "message": "answer"}
    return fake_agent


def test_the_api_response_carries_the_same_object(isolated_app, monkeypatch):
    contract = {"valid": True, "limitations": ["mobility: skipped"],
                "final_response_truncated": False}
    monkeypatch.setattr(appmod, "handle_with_react_agent",
                        _agent_publishing({"answer_contract": contract}))

    response = isolated_app.post("/api/alex", json={"message": "hi"},
                                 headers={"X-User-Id": "u_contract"})

    assert response.status_code == 200
    assert response.get_json()["answer_contract"] == contract


def test_the_response_omits_the_key_when_there_is_no_contract(isolated_app, monkeypatch):
    monkeypatch.setattr(appmod, "handle_with_react_agent", _agent_publishing({}))

    response = isolated_app.post("/api/alex", json={"message": "hi"},
                                 headers={"X-User-Id": "u_contract"})

    assert response.status_code == 200
    assert "answer_contract" not in response.get_json()
