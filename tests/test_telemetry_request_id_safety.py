"""K8: the root task label is not content-free unless something makes it so.

The chain that made it user-reachable:

    X-Request-Id header  ->  observability.new_request_id(value)  ->  `value or uuid4`
      ->  app.py  task_id = f"turn:{request_id}"
      ->  turn_observations.note_root_agent_context(...)  which only str()'d it
      ->  the canary record's `task_id` / `root_agent_context`, AND every
          JsonFormatter line for the request (`request_id`, `task_id`).

Nothing on that path validated anything, so a client could write arbitrary text of
arbitrary length into ops telemetry that humans read and pipelines ship off-box.
"turn:" made it LOOK generated. The docstring on note_root_agent_context said
"generated IDs must contain no user text" — the rule existed; the check did not.

Fixed at the source (the header is the untrusted input) and again at the sink
(defence in depth: a future caller that builds a label some other way still cannot
smuggle text through).
"""
from __future__ import annotations

import json
import hashlib
import logging

import pytest

from core import turn_observations as tobs
from uk_rent_agent.observability import (
    JsonFormatter, agent_execution_context, new_request_id,
)


@pytest.fixture()
def turn():
    tobs.begin_turn()
    yield
    tobs.end_turn()


# --------------------------------------------------------------------------- #
# The source: new_request_id.                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", [
    "0123456789abcdef0123456789abcdef",              # uuid4().hex
    "3f2504e0-4f89-11d3-9a0c-0305e82c3301",          # dashed uuid
    "edge:12345",                                    # proxy-style
    "req.7_x-9",
    "A",
])
def test_a_real_machine_id_is_honoured(value):
    """Correlation only works if a well-formed upstream id survives. Rejecting
    everything would be safe and useless."""
    assert new_request_id(value) == value


@pytest.mark.parametrize("value", [
    "我的预算是1400镑，想住在Camden",                   # the user's own words
    "req id with spaces",
    "../../etc/passwd",
    "a" * 200,                                        # unbounded length
    "\n{\"level\": \"ERROR\", \"message\": \"fake\"}",  # log-line injection
    "",                                               # empty is not an identifier
    "-leading-punctuation",
    "id\x00nul",
])
def test_anything_that_is_not_a_machine_id_is_replaced_not_sanitised(value):
    """Replaced, never trimmed. A truncated copy of attacker text is still attacker
    text, and a partial match would also break the one-id-one-request correlation
    the field exists for."""
    generated = new_request_id(value)
    assert generated != value
    assert len(generated) == 32 and generated.isalnum()


def test_no_value_still_generates():
    assert len(new_request_id(None)) == 32
    assert new_request_id() != new_request_id()


@pytest.mark.parametrize("value", [
    "Root=1-67891233-abcdef012345678912345678",   # AWS X-Ray: contains '='
    "abc/def+ghi=",                               # base64 trace id
    "x" * 200,                                    # longer than the 96-char grammar
    "我的预算是1400镑",
])
def test_the_replacement_is_deterministic_so_idempotent_replay_still_works(value):
    """Both call sites do::

        request_id = new_request_id(request.headers.get("X-Request-Id"))
        prior = conversation_store.get_request_turn(user_id, request_id)

    With a random replacement, the same client retrying the same request got a new
    id every time, the replay lookup never matched, and the whole turn re-ran and
    re-billed. A digest is total and deterministic: it echoes none of the client's
    text and keeps one-id-one-request intact.
    """
    first, second = new_request_id(value), new_request_id(value)
    assert first == second
    assert first == hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]
    assert value not in first
    assert len(first) == 32 and first.isalnum()
    assert new_request_id(value + "!") != first, "different input, different id"


def test_the_rejected_value_is_never_logged(caplog):
    """Writing the rejected id into the log to explain why we refused to write it
    into the log is the whole defect, restated."""
    secret = "please remember my budget is 1400 for Camden"
    with caplog.at_level(logging.DEBUG, logger="uk_rent_agent.observability"):
        new_request_id(secret)
    assert secret not in caplog.text
    assert any("replaced" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# The sink: note_root_agent_context.                                           #
# --------------------------------------------------------------------------- #

def test_a_generated_root_label_is_recorded(turn):
    assert tobs.note_root_agent_context(
        agent_role="manager", task_id="turn:0123456789abcdef") is True
    assert tobs.current()["root_agent_context"] == {
        "agent_role": "manager", "task_id": "turn:0123456789abcdef"}


@pytest.mark.parametrize("field,value", [
    ("agent_role", "manager for 我想找 Camden 的房子"),
    ("task_id", "turn:我想找 Camden 的房子"),
    ("task_id", "turn:" + "x" * 400),
    ("parent_task_id", "root: with a space"),
    ("task_id", ""),
    ("task_id", None),
])
def test_an_unsafe_id_records_nothing_and_returns_false(turn, field, value):
    fields = {"agent_role": "manager", "task_id": "turn:abc123",
              "parent_task_id": "root:abc123"}
    fields[field] = value
    assert tobs.note_root_agent_context(**fields) is False
    # Nothing partially recorded: the turn stays attributable-to-nobody rather than
    # attributable to a label carrying the user's text.
    assert tobs.current()["root_agent_context"] is None


def test_refusal_does_not_burn_the_one_shot(turn):
    """The first ACCEPTED context wins. A rejected one must not consume the slot,
    or a defensive caller retrying with a safe label would silently get nothing."""
    assert tobs.note_root_agent_context(
        agent_role="manager", task_id="turn:has a space") is False
    assert tobs.note_root_agent_context(
        agent_role="manager", task_id="turn:safe1") is True
    assert tobs.current()["root_agent_context"]["task_id"] == "turn:safe1"


def test_the_snapshot_only_carries_validated_labels(turn):
    tobs.note_root_agent_context(agent_role="manager", task_id="turn:deadbeef")
    assert tobs.snapshot()["root_agent_context"] == {
        "agent_role": "manager", "task_id": "turn:deadbeef"}


# --------------------------------------------------------------------------- #
# The other consumer of the same labels: every structured log line.            #
# --------------------------------------------------------------------------- #

def test_the_json_log_line_carries_only_a_validated_request_id():
    request_id = new_request_id("nice try: 我的预算是1400镑")
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "turn.start",
                               None, None)
    record.request_id = request_id
    with agent_execution_context(agent_role="manager",
                                 task_id=f"turn:{request_id}"):
        line = json.loads(JsonFormatter().format(record))
    assert "1400" not in line["request_id"]
    assert line["task_id"] == f"turn:{request_id}"
