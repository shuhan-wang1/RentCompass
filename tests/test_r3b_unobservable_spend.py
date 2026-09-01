"""A turn we could not observe must never assert that it was free.

R2-3.  ``unknown_turn_signals`` exists because zeros are fail-open on a turn whose
own bookkeeping does not exist — that is its docstring's whole argument — and it
then copied ``llm_usage_status`` verbatim out of the accumulator. The accumulator
reports ``no_llm_calls`` whenever no run reached its COMPLETION callback, which is
exactly the state of a turn killed mid-flight by the graph timeout and of one whose
only provider call errored (``note_provider_error`` does not touch
``llm_usage_missing``). Both may already have been billed. The record then said
``llm_usage_status="no_llm_calls", llm_calls=0, llm_usage=null`` and every consumer
accepted it as a fully measured, zero-cost turn: ``canary_cost`` took it out of the
chargeable population entirely.

R2-4.  ``note_raw_llm_call`` set the SAME process-wide flag as ``install_observer``,
so one raw ``_call_deepseek`` declared the LangChain observer installed. If the
callback install had failed — its ``except`` deliberately swallows — ``snapshot()``
stopped saying ``not_instrumented`` and started saying ``complete`` over a count
containing only the raw calls, with every ModelRouter call invisible. The gate reads
a clean, cheap candidate.

R2-10.  ``asyncio.CancelledError`` is a ``BaseException``, so a client disconnect
was seen by neither the endpoint's ``except Exception`` nor Flask's
``errorhandler(Exception)`` and the turn emitted NOTHING — silently shrinking the
denominator every rate in the window is computed over.

R2-5.  Two 5xx raised before ``g.canary_request_id`` is stamped both emitted the
literal ``request_id="unknown"``, and the report's ``(request_id, endpoint, arch)``
uniqueness rule convicted them as one turn emitted twice — the wrong diagnosis
handed to an operator who is already looking at two server errors.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ["CONVERSATION_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="r3b_unobservable_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"
os.environ.setdefault("CANARY_USER_HASH_KEY", "test-key")

import app as appmod  # noqa: E402 — heavy one-time import after env setup
from core import canary_telemetry as ct  # noqa: E402
from core import turn_observations as tobs  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load("r3b_report", "canary_report.py")
cc = _load("r3b_cost", "canary_cost.py")


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture
def user():
    return "u" + uuid.uuid4().hex[:16]


@pytest.fixture
def observer_flags():
    """Pin BOTH process-wide observer flags; restore whatever the session had."""
    prev, prev_raw = tobs._observer_installed, tobs._raw_observer_installed
    yield
    tobs._observer_installed, tobs._raw_observer_installed = prev, prev_raw
    tobs.end_turn()


def _canary_turns(caplog):
    out = []
    for record in caplog.records:
        if record.name != "canary":
            continue
        try:
            obj = json.loads(record.getMessage())
        except Exception:
            continue
        if obj.get("event") == "canary.turn":
            out.append(obj)
    return out


def _post(client, user, message="hello"):
    return client.post("/api/alex", json={"message": message},
                       headers={"X-User-Id": user})


# --------------------------------------------------------------------------- #
# R2-3 — the producer                                                         #
# --------------------------------------------------------------------------- #

def test_an_unobservable_turn_never_reports_no_llm_calls():
    """The exact snapshot a timed-out or provider-errored turn produces."""
    observed = {"llm_usage_status": "no_llm_calls", "llm_calls": 0,
                "provider_schema_400_count": 0, "llm_usage_calls": []}
    signals = ct.unknown_turn_signals(observed)

    assert signals["llm_usage_status"] == ct.USAGE_PARTIAL
    assert signals["llm_usage"] is None
    # The observed call count is still a fact and still reported — it is the
    # STATUS, not the number, that decides whether the zero may be trusted.
    assert signals["llm_calls"] == 0


@pytest.mark.parametrize("status", ["complete", "partial", "not_instrumented"])
def test_every_other_status_passes_through_unchanged(status):
    """Only the positive zero-spend assertion is refused. `complete` still means
    the calls that reached their terminal callback, and rewriting it would drop a
    crashed turn's real spend out of the cost side."""
    signals = ct.unknown_turn_signals({"llm_usage_status": status, "llm_calls": 2})
    assert signals["llm_usage_status"] == status


def test_a_turn_with_no_accumulator_at_all_still_holds():
    """No observations to overlay: the default is the honest "nothing watched"."""
    assert ct.unknown_turn_signals()["llm_usage_status"] == ct.USAGE_NOT_INSTRUMENTED
    assert ct.unknown_turn_signals({})["llm_usage_status"] == ct.USAGE_NOT_INSTRUMENTED


def _crash_record(status_source: dict, *, request_id="req-crash"):
    return ct.build_canary_turn_record(
        endpoint="alex", agent_arch="fc_loop", candidate_sha="c" * 12, strict=True,
        request_id=request_id, conversation_id="conv-1", user_id="user-1",
        http_status=200, turn_outcome=ct.OUTCOME_CRASH, turn_latency_ms=10.0,
        signals=ct.unknown_turn_signals(status_source))


def test_both_consumers_treat_the_crashed_turn_as_unmeasured_not_as_zero():
    """The two halves of "unmeasured": the report holds, the cost tool refuses to
    price the turn at zero. Compared against the same record claiming no_llm_calls,
    which is what the producer used to emit."""
    observed = {"llm_usage_status": "no_llm_calls", "llm_calls": 0,
                "provider_schema_400_count": 0,
                "denied_write_count": 0, "tainted_write_executed_count": 0,
                "forbidden_write_executed_count": 0, "write_audit": [],
                "dsml_blocked": 0, "dsml_leak": 0}
    fixed = _crash_record(observed)
    assert fixed["llm_usage_status"] == "partial"

    problems = cr.validate_record(fixed)
    assert any("undercount of unknown size" in p for p in problems), problems

    priced = cc.sum_usage([fixed])
    assert priced["_unmeasured_turns"]["count"] == 1
    assert priced["_no_llm_call_turns"]["count"] == 0
    assert priced["_chargeable_turns"]["count"] == 1

    # The old shape, for contrast: it left the chargeable population entirely.
    old = dict(fixed, llm_usage_status="no_llm_calls")
    old_priced = cc.sum_usage([old])
    assert old_priced["_no_llm_call_turns"]["count"] == 1
    assert old_priced["_chargeable_turns"]["count"] == 0


def test_a_search_direct_turn_still_states_its_provable_zero():
    """The endpoint that genuinely cannot call an LLM keeps its measured zero.
    Widening the refusal to every turn would make the deterministic path
    permanently unmeasured for no reason."""
    assert ct.search_direct_signals()["llm_usage_status"] == ct.USAGE_NO_CALLS


# --------------------------------------------------------------------------- #
# R2-3 — driven through the real endpoint: crash, timeout, cancellation        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("exc", [
    RuntimeError("boom"),                      # a crash inside the graph
    asyncio.TimeoutError(),                    # _ainvoke_graph_with_timeout fired
])
def test_a_crashed_or_timed_out_turn_does_not_claim_zero_spend(
        client, user, monkeypatch, caplog, observer_flags, exc):
    tobs._observer_installed = True   # the observer IS running; nothing completed
    tobs._raw_observer_installed = False

    async def _raise(*a, **k):
        raise exc
    monkeypatch.setattr(appmod, "handle_with_react_agent", _raise)

    with caplog.at_level(logging.INFO, logger="canary"):
        response = _post(client, user, "please die")

    assert response.status_code == 502
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    record = turns[0]
    assert record["turn_outcome"] == "crash"
    assert record["llm_usage_status"] == "partial", (
        "a turn that died before any call completed cannot certify zero spend")
    assert record["llm_usage"] is None


def test_a_cancelled_turn_emits_a_record_instead_of_vanishing(
        client, user, monkeypatch, caplog, observer_flags):
    """A client disconnect must cost the window a recorded turn, not a missing one."""
    async def _cancel(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(appmod, "handle_with_react_agent", _cancel)

    with caplog.at_level(logging.INFO, logger="canary"):
        with pytest.raises(asyncio.CancelledError):
            _post(client, user, "hang up on me")

    turns = _canary_turns(caplog)
    assert len(turns) == 1, "the cancelled turn must not vanish from the denominator"
    record = turns[0]
    assert record["turn_outcome"] == "crash"
    assert record["http_status"] == appmod.CANARY_CLIENT_CLOSED_STATUS == 499
    assert record["llm_usage_status"] in cr.USAGE_STATUS_HOLD
    assert record["tool_ledger_status"] == "unavailable"
    assert record["turn_latency_ms"] >= 0


def test_the_cancelled_record_is_not_counted_as_a_server_error():
    """499 is "client closed request". Folding it into the 5xx rate — a relative
    stage-pause threshold — would charge the candidate for the client's behaviour."""
    record = ct.build_canary_turn_record(
        endpoint="alex", agent_arch="fc_loop", candidate_sha="c" * 12, strict=True,
        request_id="req-cancelled", conversation_id="conv-1", user_id="user-1",
        http_status=appmod.CANARY_CLIENT_CLOSED_STATUS,
        turn_outcome=ct.OUTCOME_CRASH, turn_latency_ms=4200.0,
        signals=ct.unknown_turn_signals({"llm_usage_status": "no_llm_calls"}))

    assert cr.classify(record)["http_5xx"] == 0


# --------------------------------------------------------------------------- #
# R2-4                                                                        #
# --------------------------------------------------------------------------- #

def test_a_raw_call_does_not_declare_the_callback_observer_installed(observer_flags):
    tobs._observer_installed = False
    tobs._raw_observer_installed = False
    tobs.begin_turn()

    assert tobs.note_raw_llm_call(
        "rawds:0", usage_blob={"prompt_tokens": 10, "completion_tokens": 2},
        configured_model="deepseek-v4-flash") is True

    assert tobs.observer_installed() is False, (
        "only install_observer may claim the LangChain path is observed")
    assert tobs.raw_observer_installed() is True


def test_raw_only_observation_reports_its_own_facts_and_flags_the_gap(observer_flags):
    """TWO FACTS, TWO FIELDS.

    The raw path's observations are real: the call happened, it was billed, and a
    429 on it is a fact about that request. The snapshot states them. What it must
    NOT do is imply they are the whole turn — and that is what
    `llm_observer_installed: False` says, in its own field, without deleting
    anything true. (An earlier revision expressed the gap by degrading the STATUS
    to `partial` and nulling these counters; it erased real observations to state a
    different fact, and CI caught it as seven contradictions in the raw-path tests.)
    """
    tobs._observer_installed = False
    tobs._raw_observer_installed = False
    tobs.begin_turn()
    tobs.note_raw_llm_call("rawds:0",
                           usage_blob={"prompt_tokens": 10, "completion_tokens": 2},
                           configured_model="deepseek-v4-flash")
    snapshot = tobs.snapshot()

    assert snapshot["llm_calls"] == 1
    assert len(snapshot["llm_usage_calls"]) == 1
    # Complete FOR WHAT WAS WATCHED — the same rule the callback path follows.
    assert snapshot["llm_usage_status"] == tobs.USAGE_COMPLETE
    # A raw-path provider error is still counted, not nulled away.
    assert snapshot["provider_schema_400_count"] == 0
    assert snapshot["provider_other_400_count"] == 0
    # ...and the scope of all of it is stated explicitly.
    assert snapshot["llm_observer_installed"] is False


def test_a_raw_only_record_cannot_reach_the_gate_as_a_clean_cheap_turn(observer_flags):
    """R2-4's actual failure mode, asserted where it is now caught: the RECORD.

    A process whose callback observer never attached emits turns whose counters
    omit every ModelRouter call. The status may legitimately read `complete`; the
    flag is what stops that from being promotable, in both consumers."""
    tobs._observer_installed = False
    tobs._raw_observer_installed = False
    tobs.begin_turn()
    tobs.note_raw_llm_call("rawds:0",
                           usage_blob={"prompt_tokens": 10, "completion_tokens": 2},
                           configured_model="deepseek-v4-flash")
    snapshot = tobs.snapshot()
    signals = {
        "soft_wrapped": False, "wrapped_by": None, "partial": False,
        "tool_budget_timeout": False,
        "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                     "forbidden_write_executed_count": 0, "write_audit": []},
        "dsml_blocked": 0, "dsml_leak": 0, "tool_batches": 0,
        "tool_ledger_status": ct.TOOL_LEDGER_COMPLETE,
    }
    signals.update({
        "llm_usage_status": snapshot["llm_usage_status"],
        "llm_calls": snapshot["llm_calls"],
        "llm_usage": ct.aggregate_llm_usage(snapshot["llm_usage_calls"]),
        "provider_schema_400_count": snapshot["provider_schema_400_count"],
        ct.OBSERVER_INSTALLED_FIELD: snapshot["llm_observer_installed"],
    })
    record = ct.build_canary_turn_record(
        endpoint="alex", agent_arch="fc_loop", candidate_sha="c" * 12, strict=True,
        request_id="req-raw-only", conversation_id="conv-1", user_id="user-1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=10.0,
        signals=signals)

    assert record["llm_usage_status"] == "complete"
    assert record[ct.OBSERVER_INSTALLED_FIELD] is False
    problems = cr.validate_record(record)
    assert any("llm_observer_installed=false" in p for p in problems), problems
    # And the cost side refuses to price a floor as a total.
    priced = cc.sum_usage([record])
    assert priced["_unmeasured_turns"]["count"] == 1
    assert priced["_chargeable_turns"]["count"] == 1
    assert priced["_no_llm_call_turns"]["count"] == 0


def test_the_callback_observer_still_certifies_a_complete_turn(observer_flags):
    tobs._observer_installed = True
    tobs._raw_observer_installed = False
    tobs.begin_turn()
    tobs.note_raw_llm_call("rawds:0",
                           usage_blob={"prompt_tokens": 10, "completion_tokens": 2},
                           configured_model="deepseek-v4-flash")
    snapshot = tobs.snapshot()

    assert snapshot["llm_usage_status"] == tobs.USAGE_COMPLETE
    assert snapshot["provider_schema_400_count"] == 0


def test_a_raw_call_cannot_suppress_the_startup_wiring(observer_flags):
    """`_wire_canary_llm_observer` returns early when observer_installed() is True.
    With one flag, a raw call made before startup wiring ran would have skipped the
    install altogether."""
    tobs._observer_installed = False
    tobs._raw_observer_installed = False
    tobs.begin_turn()
    tobs.note_raw_llm_call("rawds:0",
                           usage_blob={"prompt_tokens": 1, "completion_tokens": 1},
                           configured_model="m")

    assert tobs.observer_installed() is False


# --------------------------------------------------------------------------- #
# R2-5                                                                        #
# --------------------------------------------------------------------------- #

def test_two_pre_identity_5xx_are_two_turns_not_one_duplicate(
        client, user, monkeypatch, caplog):
    """Both fail before g.canary_request_id exists, so both must mint their own
    sentinel — otherwise the operator is told the app emitted one turn twice."""
    def _explode(*a, **k):
        raise RuntimeError("pre-identity failure")
    monkeypatch.setattr(appmod, "normalize_message", _explode)

    with caplog.at_level(logging.INFO, logger="canary"):
        first = _post(client, user, "one")
        second = _post(client, "u" + uuid.uuid4().hex[:16], "two")

    assert first.status_code == second.status_code == 500
    turns = _canary_turns(caplog)
    assert len(turns) == 2
    ids = [t["request_id"] for t in turns]
    assert ids[0] != ids[1]
    assert all(i.startswith("unknown:") for i in ids), ids

    violations = cr.validate_records(turns, candidate_arch="legacy")["violations"]
    assert not any("duplicate records" in v for v in violations), violations
