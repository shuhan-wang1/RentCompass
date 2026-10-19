"""The telemetry half of the 2026-09-03 T3 高-1 finding.

A one-line hostile body did not merely return 500 — it produced a canary record with
``llm_observer_installed: true`` next to ``llm_usage_status: "not_instrumented"``.
That is the four-cell combination README §13 says is never forgiven (the 2026-07-25
class: nothing was watching), so an anonymous caller could drive the gate to
INSTRUMENTATION-HOLD at will with `curl`.

The two rules pinned here:

  1. A request rejected at PARSE time is a client error and emits NO turn record at
     all — it never entered the A/B population, so it must not appear in it.
  2. A genuine crash BEFORE the observation window opened reports
     ``llm_usage_status: partial`` (the observed, exemption-eligible cell), never
     ``not_instrumented``. Nothing is guessed: the endpoints make every model call
     after ``begin_turn()``, so a turn that never reached it provably made none.
"""
from __future__ import annotations

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
    tempfile.mkdtemp(prefix="f5a_precrash_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"
os.environ.setdefault("CANARY_USER_HASH_KEY", "test-key")

import app as appmod  # noqa: E402 — heavy one-time import after env setup
from core import turn_observations as tobs  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load("f5a_report", "canary_report.py")

T3_LONE_SURROGATE = '{"message":"hello \\ud800 world","conversation_id":null}'


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture
def observer_installed():
    """Pin the process-wide callback-observer flag on, and restore it."""
    prev, prev_raw = tobs._observer_installed, tobs._raw_observer_installed
    tobs._observer_installed = True
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


def _user():
    return "u" + uuid.uuid4().hex[:16]


# --------------------------------------------------------------------- rule 1

def test_a_parse_time_rejection_emits_no_turn_record(client, caplog, observer_installed):
    with caplog.at_level(logging.INFO, logger="canary"):
        response = client.post(
            "/api/alex", data=T3_LONE_SURROGATE, content_type="application/json",
            headers={"X-User-Id": _user()},
        )
    assert response.status_code == 400
    assert _canary_turns(caplog) == [], "a 4xx client error is not a canary turn"


def test_an_over_size_body_emits_no_turn_record(client, caplog, observer_installed):
    body = json.dumps({"message": "a" * (appmod._runtime_config.max_request_bytes + 10)})
    with caplog.at_level(logging.INFO, logger="canary"):
        response = client.post(
            "/api/alex", data=body, content_type="application/json",
            headers={"X-User-Id": _user()},
        )
    assert response.status_code == 413
    assert _canary_turns(caplog) == []


# --------------------------------------------------------------------- rule 2

def test_a_crash_before_the_window_reports_partial_not_not_instrumented(
        client, caplog, monkeypatch, observer_installed):
    """The shape of the original defect: a 500 raised BEFORE `begin_turn()`."""
    def _explode(*a, **k):
        raise RuntimeError("pre-window failure")

    monkeypatch.setattr(appmod, "normalize_message", _explode)

    with caplog.at_level(logging.INFO, logger="canary"):
        response = client.post("/api/alex", json={"message": "hello"},
                               headers={"X-User-Id": _user()})

    assert response.status_code == 500
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    record = turns[0]
    assert record["llm_observer_installed"] is True
    assert record["llm_usage_status"] == "partial", (
        "observer installed + not_instrumented is the four-cell violation this fixes")
    assert record["turn_outcome"] == "server_error"


def test_that_record_passes_the_gate_without_an_instrumentation_violation(
        client, caplog, monkeypatch, observer_installed):
    def _explode(*a, **k):
        raise RuntimeError("pre-window failure")

    monkeypatch.setattr(appmod, "normalize_message", _explode)
    with caplog.at_level(logging.INFO, logger="canary"):
        client.post("/api/alex", json={"message": "hello"},
                    headers={"X-User-Id": _user()})

    turns = _canary_turns(caplog)
    assert len(turns) == 1
    for record in turns:
        problems = cr.validate_record(record)
        assert problems == [], problems
    contract = cr.validate_records(turns, candidate_arch=record["agent_arch"])
    assert contract["ok"] is True, contract


def test_a_turn_that_ran_keeps_reporting_what_its_accumulators_saw(
        client, monkeypatch, observer_installed):
    """The narrow condition must not swallow a REAL post-window failure.

    A turn that opened its window and then died reports the accumulator's own
    status; only a turn that never opened one is credited with zero calls.
    """
    seen = {}

    async def fake_agent(user_message, *a, **k):
        tobs.note_dsml_blocked()
        seen["pre_window"] = appmod._died_before_the_observation_window()
        seen["observed"] = appmod._crashed_turn_observations()
        raise RuntimeError("post-window failure")

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    response = client.post("/api/alex", json={"message": "hello"},
                           headers={"X-User-Id": _user()})

    assert response.status_code == 502
    assert seen["pre_window"] is False
    # The accumulator's OWN numbers, never the pre-window overlay's zeros.
    assert seen["observed"]["dsml_blocked"] == 1


def test_the_pre_window_predicate_is_false_once_the_window_is_open():
    """Unit-level statement of the narrow condition, outside any request."""
    with appmod.app.test_request_context("/api/alex", method="POST"):
        assert appmod._died_before_the_observation_window() is True
        tobs.begin_turn()
        appmod.g.canary_turn_started = 1.0
        assert appmod._died_before_the_observation_window() is False
    tobs.end_turn()
