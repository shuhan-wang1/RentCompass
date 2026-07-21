"""Provider-error observation: does the count actually reach the canary record?

Two things are being proven here, and only one of them is about arithmetic.

1. Classification is structural — HTTP status plus whether WE bound schemas — so it
   cannot rot when a vendor rewrites an error sentence.

2. The count SURVIVES. This is the part that needs real execution rather than a unit
   test: the accumulator lives in a ContextVar, LangGraph runs nodes as tasks (and
   sync nodes via an executor), and a child context is a COPY. The design relies on
   mutating one shared dict rather than re-setting the var — a claim that is easy to
   assert in a comment and easy to get wrong in practice. So the propagation tests
   drive the real Flask endpoint with a stubbed provider that raises, on BOTH arches,
   and read the count off the emitted canary record.
"""

import json
import logging
import os
import sys
import tempfile
import uuid

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ["CONVERSATION_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="turn_obs_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import app as appmod  # noqa: E402
from core import turn_observations as tobs  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #

class _ProviderError(Exception):
    """Shaped like an openai.BadRequestError without importing the SDK — which is
    the point: the classifier must duck-type, because LangChain may wrap or re-raise
    the provider's exception as some other class."""

    def __init__(self, status_code=400, message="rejected"):
        super().__init__(message)
        self.status_code = status_code


class _NestedResponseError(Exception):
    """The other shape in the wild: status on a nested .response object."""

    def __init__(self, status_code=400):
        super().__init__("rejected")
        self.response = type("R", (), {"status_code": status_code})()


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.fixture(autouse=True)
def _fresh_window():
    """Every test starts with a closed window, so a test that forgets to open one
    is measuring the real 'no turn in progress' path rather than a leftover."""
    tobs.end_turn()
    yield
    tobs.end_turn()


@pytest.fixture
def installed(monkeypatch):
    """Pretend the observer was installed (it is, in prod, at ModelRouter.create)."""
    monkeypatch.setattr(tobs, "_observer_installed", True)


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def _canary_turns(caplog):
    out = []
    for rec in caplog.records:
        if rec.name != "canary":
            continue
        try:
            obj = json.loads(rec.getMessage())
        except Exception:
            continue
        if obj.get("event") == "canary.turn":
            out.append(obj)
    return out


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #

def test_400_with_schemas_bound_is_a_schema_400(installed):
    tobs.begin_turn()
    assert tobs.note_provider_error(_ProviderError(400), schemas_bound=True) == "schema_400"
    assert tobs.snapshot()["provider_schema_400_count"] == 1


def test_400_without_schemas_is_not_a_schema_400(installed):
    """The gate metric must not absorb ordinary 400s — context-length, bad params —
    or a noisy unrelated failure would read as a strict-schema regression."""
    tobs.begin_turn()
    assert tobs.note_provider_error(_ProviderError(400), schemas_bound=False) == "other_400"
    assert tobs.snapshot()["provider_schema_400_count"] == 0
    assert tobs.snapshot()["provider_other_400_count"] == 1


def test_non_400_statuses_are_not_counted(installed):
    tobs.begin_turn()
    for status in (401, 429, 500, 503):
        tobs.note_provider_error(_ProviderError(status), schemas_bound=True)
    assert tobs.snapshot()["provider_schema_400_count"] == 0
    # Still recorded for forensics — "no schema 400s" must not mean "no errors".
    assert tobs.current()["provider_error_count"] == 4


def test_status_is_read_from_a_nested_response(installed):
    tobs.begin_turn()
    assert tobs.note_provider_error(_NestedResponseError(400), schemas_bound=True) == "schema_400"


def test_error_with_no_status_at_all_is_not_counted(installed):
    """A timeout or a connection reset carries no status. Counting it as a schema
    rejection would manufacture a zero-tolerance breach out of a network blip."""
    tobs.begin_turn()
    assert tobs.note_provider_error(RuntimeError("boom"), schemas_bound=True) is None
    assert tobs.snapshot()["provider_schema_400_count"] == 0


def test_forensic_ring_is_bounded(installed):
    """A retry storm must not grow the record without limit."""
    tobs.begin_turn()
    for _ in range(500):
        tobs.note_provider_error(_ProviderError(400), schemas_bound=True)
    assert tobs.snapshot()["provider_schema_400_count"] == 500  # the COUNT is exact
    assert len(tobs.current()["provider_errors"]) == 20         # the detail is capped


# --------------------------------------------------------------------------- #
# Fail-closed: null, never a fabricated zero                                  #
# --------------------------------------------------------------------------- #

def test_uninstalled_observer_reports_null_not_zero(monkeypatch):
    """If the observer never attached, we did not look — and 'did not look' must not
    render as 'looked and saw none'. This is the whole fail-closed contract: a
    refactor that bypasses ModelRouter.create HOLDS the gate instead of silently
    reporting a clean pool."""
    monkeypatch.setattr(tobs, "_observer_installed", False)
    tobs.begin_turn()
    assert tobs.snapshot()["provider_schema_400_count"] is None


def test_no_turn_window_reports_null_not_zero(installed):
    tobs.end_turn()
    assert tobs.snapshot()["provider_schema_400_count"] is None


def test_note_outside_a_window_is_a_silent_noop(installed):
    tobs.end_turn()
    assert tobs.note_provider_error(_ProviderError(400), schemas_bound=True) is None


# --------------------------------------------------------------------------- #
# Propagation into the real record — BOTH arches                              #
# --------------------------------------------------------------------------- #

def _install_agent_that_hits_a_400(monkeypatch, *, crash: bool):
    """Stub the agent so the provider error happens where it really would: inside the
    graph, mid-turn. `crash` decides whether the turn then dies (the case where
    final_state is destroyed and only the accumulator survives)."""
    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        tobs.note_provider_error(_ProviderError(400), schemas_bound=True)
        if crash:
            raise RuntimeError("provider 400 took the turn down with it")
        appmod._write_back_turn(
            user_id, conversation_id, user_message, "recovered", [],
            turn_id=(turn or {}).get("id"), reply_language="en")
        appmod._turn_fc_signals.set(appmod._build_fc_signals({}))
        return {"response_type": "chat", "message": "recovered"}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


@pytest.mark.parametrize("arch", ["fc_loop", "legacy"])
def test_schema_400_reaches_the_record_on_both_arches(client, monkeypatch, caplog, installed, arch):
    """Both arches must report this. If only fc_loop did, the CONTROL pool would sit
    on a null forever and the gate could never clear — the A/B needs both sides
    instrumented, not just the candidate."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", arch)
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", arch == "fc_loop")
    _install_agent_that_hits_a_400(monkeypatch, crash=False)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/alex", json={"message": "hi"},
                        headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert r.status_code == 200
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    assert turns[0]["agent_arch"] == arch
    assert turns[0]["provider_schema_400_count"] == 1, turns[0]


@pytest.mark.parametrize("arch", ["fc_loop", "legacy"])
def test_schema_400_survives_a_crash(client, monkeypatch, caplog, installed, arch):
    """The case the whole ContextVar design exists for. The turn crashes, so there is
    no final_state to read — but a strict-schema 400 is a plausible CAUSE of that
    crash, so it is exactly the signal that must not die with it.

    Note what stays null: the write counters. We still cannot know whether `remember`
    executed before the crash, and this test asserts we do not pretend otherwise.
    """
    monkeypatch.setattr(appmod, "AGENT_ARCH", arch)
    _install_agent_that_hits_a_400(monkeypatch, crash=True)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/alex", json={"message": "hi"},
                        headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert r.status_code == 200  # always-200 contract
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    rec = turns[0]
    assert rec["turn_outcome"] == "crash"
    assert rec["provider_schema_400_count"] == 1, rec
    assert rec["security"]["forbidden_write_executed_count"] is None, \
        "a crashed turn must still refuse to assert a clean write audit"


def test_window_does_not_leak_between_requests(client, monkeypatch, caplog, installed):
    """A count that leaked forward would attribute one turn's provider failure to the
    next one — and a gate that blames the wrong turn is worse than one that blames
    nobody."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    user = "u" + uuid.uuid4().hex[:16]

    _install_agent_that_hits_a_400(monkeypatch, crash=False)
    with caplog.at_level(logging.INFO, logger="canary"):
        client.post("/api/alex", json={"message": "one"}, headers={"X-User-Id": user})

    caplog.clear()

    async def _clean(user_message, context, is_continuation, user_id, conversation_id,
                     request_id, ui_language="en", turn=None):
        appmod._turn_fc_signals.set(appmod._build_fc_signals({}))
        return {"response_type": "chat", "message": "clean"}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _clean)

    with caplog.at_level(logging.INFO, logger="canary"):
        client.post("/api/alex", json={"message": "two"}, headers={"X-User-Id": user})
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    assert turns[0]["provider_schema_400_count"] == 0, turns[0]


def test_boundary_5xx_reports_the_observed_400(client, monkeypatch, caplog, installed):
    """A request that dies at the response boundary previously reported a blanket
    null. It should still report what the accumulator actually saw."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")

    class _Unserializable:
        pass

    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        tobs.note_provider_error(_ProviderError(400), schemas_bound=True)
        appmod._turn_fc_signals.set(None)
        return {"response_type": "chat", "message": "ok", "junk": _Unserializable()}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/alex", json={"message": "hi"},
                        headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert r.status_code == 500
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    assert turns[0]["turn_outcome"] == "server_error"
    assert turns[0]["provider_schema_400_count"] == 1, turns[0]


# --------------------------------------------------------------------------- #
# Token usage                                                                 #
# --------------------------------------------------------------------------- #

def _gen(*, usage_metadata=None, response_metadata=None, generation_info=None):
    msg = type("Msg", (), {"usage_metadata": usage_metadata,
                           "response_metadata": response_metadata or {}})()
    return type("Gen", (), {"message": msg, "generation_info": generation_info})()


def _result(gen, llm_output=None):
    return type("LLMResult", (), {"generations": [[gen]], "llm_output": llm_output})()


def test_usage_from_usage_metadata():
    r = _result(_gen(usage_metadata={"input_tokens": 100, "output_tokens": 20,
                                     "input_token_details": {"cache_read": 64}}))
    assert tobs.extract_usage(r) == {"input_tokens": 100, "output_tokens": 20,
                                     "cache_read_tokens": 64}


def test_usage_from_response_metadata_token_usage():
    r = _result(_gen(response_metadata={"token_usage": {
        "prompt_tokens": 50, "completion_tokens": 8, "prompt_cache_hit_tokens": 32}}))
    assert tobs.extract_usage(r) == {"input_tokens": 50, "output_tokens": 8,
                                     "cache_read_tokens": 32}


def test_usage_from_llm_output_token_usage():
    r = _result(_gen(), llm_output={"token_usage": {"prompt_tokens": 7,
                                                    "completion_tokens": 3}})
    u = tobs.extract_usage(r)
    assert u["input_tokens"] == 7 and u["output_tokens"] == 3


def test_the_same_usage_in_two_places_is_not_summed():
    """The single most dangerous bug in this area: all three shapes carry the SAME
    run's tokens, so a merge would silently double the turn's reported spend. The
    highest-priority source must win outright."""
    r = _result(
        _gen(usage_metadata={"input_tokens": 100, "output_tokens": 20},
             response_metadata={"token_usage": {"prompt_tokens": 100,
                                                "completion_tokens": 20}}),
        llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    u = tobs.extract_usage(r)
    assert u["input_tokens"] == 100, "sources were summed instead of ranked"
    assert u["output_tokens"] == 20


def test_cache_tokens_may_fall_back_to_a_lower_priority_source():
    """cache_read is a BREAKDOWN of input_tokens, not an addition to it, so sourcing
    it separately cannot inflate any total."""
    r = _result(_gen(usage_metadata={"input_tokens": 100, "output_tokens": 20}),
                llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                            "prompt_cache_hit_tokens": 64}})
    assert tobs.extract_usage(r)["cache_read_tokens"] == 64


def test_no_usage_anywhere_returns_none():
    assert tobs.extract_usage(_result(_gen())) is None


def test_run_is_counted_once_even_if_the_callback_fires_twice(installed):
    tobs.begin_turn()
    r = _result(_gen(usage_metadata={"input_tokens": 10, "output_tokens": 2}))
    run = uuid.uuid4()
    assert tobs.note_llm_usage(run, r, configured_model="cfg") is True
    assert tobs.note_llm_usage(run, r, configured_model="cfg") is False
    assert len(tobs.snapshot()["llm_usage_calls"]) == 1


def test_model_name_prefers_the_provider_response():
    r = _result(_gen(usage_metadata={"input_tokens": 1, "output_tokens": 1},
                     response_metadata={"model_name": "deepseek-v4-flash-0731"}))
    tobs.begin_turn()
    tobs.note_llm_usage(uuid.uuid4(), r, configured_model="deepseek-v4-flash")
    call = tobs.current()["llm_usage_calls"][0]
    assert call["model"] == "deepseek-v4-flash-0731"
    assert call["model_source"] == "response"


def test_configured_model_is_a_labelled_fallback():
    """An alias can resolve to a different snapshot server-side, and cost is
    attributed per model — so a config-sourced name must be marked as such rather
    than passed off as what actually answered."""
    r = _result(_gen(usage_metadata={"input_tokens": 1, "output_tokens": 1}))
    tobs.begin_turn()
    tobs.note_llm_usage(uuid.uuid4(), r, configured_model="deepseek-v4-flash")
    call = tobs.current()["llm_usage_calls"][0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["model_source"] == "config"


def test_a_call_with_no_usage_makes_the_turn_partial_not_zero(installed):
    """The call provably happened — we are in its completion callback. Reporting the
    OTHER calls' totals as the turn's total would understate spend by an unknown
    amount, so the turn is marked unpriceable instead."""
    tobs.begin_turn()
    tobs.note_llm_usage(uuid.uuid4(),
                        _result(_gen(usage_metadata={"input_tokens": 10, "output_tokens": 2})),
                        configured_model="cfg")
    tobs.note_llm_usage(uuid.uuid4(), _result(_gen()), configured_model="cfg")
    assert tobs.snapshot()["llm_usage_status"] == tobs.USAGE_PARTIAL


def test_all_calls_priced_is_complete(installed):
    tobs.begin_turn()
    for _ in range(3):
        tobs.note_llm_usage(uuid.uuid4(),
                            _result(_gen(usage_metadata={"input_tokens": 5, "output_tokens": 1})),
                            configured_model="cfg")
    assert tobs.snapshot()["llm_usage_status"] == tobs.USAGE_COMPLETE


def test_no_calls_is_its_own_status_not_a_failure(installed):
    """A turn that made no LLM call did not fail to measure anything."""
    tobs.begin_turn()
    assert tobs.snapshot()["llm_usage_status"] == tobs.USAGE_NO_CALLS


def test_uninstalled_observer_reports_not_instrumented(monkeypatch):
    monkeypatch.setattr(tobs, "_observer_installed", False)
    tobs.begin_turn()
    assert tobs.snapshot()["llm_usage_status"] == tobs.USAGE_NOT_INSTRUMENTED
    assert tobs.snapshot()["llm_usage_calls"] is None


@pytest.mark.parametrize("arch", ["fc_loop", "legacy"])
def test_usage_reaches_the_record_on_both_arches(client, monkeypatch, caplog, installed, arch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", arch)

    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        for _ in range(2):
            tobs.note_llm_usage(
                uuid.uuid4(),
                _result(_gen(usage_metadata={"input_tokens": 100, "output_tokens": 20,
                                             "input_token_details": {"cache_read": 64}},
                             response_metadata={"model_name": "deepseek-v4-flash"})),
                configured_model="deepseek-v4-flash")
        appmod._turn_fc_signals.set(appmod._build_fc_signals({}))
        return {"response_type": "chat", "message": "ok"}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/alex", json={"message": "hi"},
                        headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert r.status_code == 200
    rec = _canary_turns(caplog)[0]
    assert rec["llm_usage_status"] == "complete", rec
    assert rec["llm_usage"]["calls"] == 2
    assert rec["llm_usage"]["input_tokens"] == 200
    assert rec["llm_usage"]["output_tokens"] == 40
    assert rec["llm_usage"]["cache_read_tokens"] == 128
    assert rec["llm_usage"]["models"]["deepseek-v4-flash"]["calls"] == 2


def test_unpriced_call_holds_the_gate(installed):
    """End-to-end on the contract: a partial turn must not clear the report."""
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))
    import canary_report

    from core.canary_telemetry import build_canary_turn_record
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    def rec(i, status):
        return build_canary_turn_record(
            endpoint="alex", agent_arch="fc_loop", candidate_sha="sha", strict=True,
            request_id=f"r{i}", conversation_id=f"c{i}", user_id=f"u{i}",
            http_status=200, turn_outcome="ok", turn_latency_ms=100.0,
            ts=t0 + timedelta(seconds=i),
            signals={"soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
                     "security": {"denied_write_count": 0,
                                  "tainted_write_executed_count": 0,
                                  "forbidden_write_executed_count": 0},
                     "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
                     "llm_usage_status": status})

    clean = [rec(i, "complete") for i in range(10)]
    assert canary_report.validate_records(clean)["ok"] is True
    poisoned = clean[:-1] + [rec(99, "partial")]
    v = canary_report.validate_records(poisoned)
    assert v["ok"] is False
    assert any("llm_usage_status" in k for k in v["violations"]), v["violations"]


# --------------------------------------------------------------------------- #
# The observer itself                                                         #
# --------------------------------------------------------------------------- #

def test_observer_records_through_the_langchain_callback():
    """Drives the real BaseCallbackHandler subclass the router attaches, so a change
    to the callback signatures fails here rather than in production silence."""
    cls = tobs._get_callback_cls()
    handler = cls()
    tobs.begin_turn()
    tobs._mark_observer_installed()

    run = uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=run,
                                invocation_params={"tools": [{"name": "search"}]})
    handler.on_llm_error(_ProviderError(400), run_id=run)
    assert tobs.snapshot()["provider_schema_400_count"] == 1


def test_observer_distinguishes_calls_with_no_tools_bound():
    cls = tobs._get_callback_cls()
    handler = cls()
    tobs.begin_turn()
    tobs._mark_observer_installed()

    run = uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=run, invocation_params={})
    handler.on_llm_error(_ProviderError(400), run_id=run)
    assert tobs.snapshot()["provider_schema_400_count"] == 0
    assert tobs.snapshot()["provider_other_400_count"] == 1


def test_observer_never_raises_into_the_call_path():
    """Telemetry must not convert a provider error into a worse one."""
    cls = tobs._get_callback_cls()
    handler = cls()
    tobs.begin_turn()
    handler.on_llm_error(_ProviderError(400), run_id=uuid.uuid4())  # no matching start
    handler.on_llm_end(None, run_id=uuid.uuid4())


def test_install_observer_attaches_and_marks_installed(monkeypatch):
    monkeypatch.setattr(tobs, "_observer_installed", False)

    class _Model:
        callbacks = None

    m = tobs.install_observer(_Model())
    assert tobs.observer_installed() is True
    assert len(m.callbacks) == 1


def test_install_observer_preserves_existing_callbacks(monkeypatch):
    """The eval collector attaches its own handler first; ours must not evict it."""
    monkeypatch.setattr(tobs, "_observer_installed", False)
    sentinel = object()

    class _Model:
        callbacks = [sentinel]

    m = tobs.install_observer(_Model())
    assert sentinel in m.callbacks and len(m.callbacks) == 2
