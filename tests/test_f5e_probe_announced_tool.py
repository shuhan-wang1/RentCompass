"""A turn that ANNOUNCES a tool call and never makes one must fail the answer probe.

THE DEFECT (P1-2, observed live on 2026-09-03). An `fc_loop` follow-up turn ended
in a 105-character *"Let me search for actual one-bedroom listings…"* stub. Its
canary record reported ``llm_calls=1`` and ``tool_batches=0``: the announced tool
call was never made. The answer carried **no** canned/fallback marker, so
``deploy/probe_pool_answer.py`` — and every other marker-based monitor — scored it
as a healthy answer. A pool that promises to look something up and then does not
is a failed turn, and after a weight change it is precisely the turn nobody sees.

WHY THE CHECK NEEDS BOTH HALVES. The announcement string on its own is not a
defect: a real turn may narrate "let me check that for you" and then actually call
the tool. The conviction is announcement AND an observed, trustworthy
``tool_batches == 0``. That is why the check lives behind ``--canary-log``: the
turn endpoint does not return the record, so without the sink the probe cannot see
the second half and must not guess.

HOW THE SINK IS SEEDED. The probe mints its own ``X-Request-Id`` and reads the
sink only after the turn returns, so the stub writes the record while it is
serving the request — which is what ``app/app.py`` does in production. Writing the
sink from the test instead would need the id before it exists.

Every case here runs against a throwaway HTTP server on an ephemeral loopback
port. No real pool is ever contacted.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "deploy" / "probe_pool_answer.py"

# Grounded, so the substring check passes and the announced-tool check is what
# actually decides the run. Without this the run would fail earlier, on grounding,
# and would prove nothing about the new check.
GROUNDING = "data.police.uk"

ANNOUNCING_ANSWER = (
    "Let me search for actual one-bedroom listings in that area and compare them "
    f"against the crime figures from {GROUNDING}."
)
ANNOUNCING_ANSWER_ZH = (
    f"让我搜索一下该区域的一居室房源，并结合 {GROUNDING} 的犯罪数据来比较。"
)
HONEST_ANSWER = (
    f"Peckham (SE15 5DP) recorded 412 crimes in the latest month per {GROUNDING}, "
    "which is around the Southwark average."
)
# An announcement-shaped phrase the patterns must NOT match: the auxiliary without
# a lookup verb. A false positive here fails a weight change on a healthy pool.
NEAR_MISS_ANSWER = (
    f"Peckham recorded 412 crimes per {GROUNDING}. Let me know if you want the "
    "borough breakdown, and I'll be honest about how noisy that figure is."
)

# Read verbatim out of the probe, which in turn copies them from agent_loop. Using
# the constants rather than retyping the sentences keeps this test honest if the
# wording changes; test_f5e_announced_tool_patterns.py holds the two copies equal.
_probe_spec = importlib.util.spec_from_file_location("probe_pool_answer", PROBE)
_probe_mod = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(_probe_mod)
STUB_MARKER_EN = _probe_mod.ANNOUNCED_TOOL_STUB_MARKER_EN
STUB_MARKER_ZH = _probe_mod.ANNOUNCED_TOOL_STUB_MARKER_ZH


def _handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            json.loads(self.rfile.read(length) or b"{}")
            request_id = self.headers.get("X-Request-Id", "")
            state.setdefault("requests", []).append(request_id)

            # Emit the canary record for THIS turn, exactly as app/app.py does:
            # to the JSONL sink, while the request is being served, keyed on the
            # id the caller sent.
            sink = state.get("sink")
            if sink is not None:
                record = {"event": "canary.turn", "request_id": request_id,
                          "llm_usage_status": "complete",
                          "tool_ledger_status": "complete"}
                record.update(state.get("record", {}))
                with open(sink, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")

            payload = json.dumps(state["payload"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            for name, value in state["headers"].items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _serve(state: dict):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _state(message: str, sink: Path | None = None, **record) -> dict:
    return {
        "payload": {"response_type": "answer", "message": message,
                    "conversation_id": "c1", "turn_id": "t1"},
        "headers": {"X-Agent-Arch": "fc_loop", "X-Agent-Specialists": "0",
                    "X-Agent-Version": "b" * 40, "X-Agent-Outcome": "ok"},
        "sink": str(sink) if sink is not None else None,
        "record": record,
    }


def _run_probe(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), "--url", url, "--timeout", "10",
         "--expect-arch", "fc_loop", "--expect-specialists", "0", *args],
        text=True, capture_output=True, timeout=60, check=False,
    )


def _probe(message: str, tmp_path: Path, **record):
    sink = tmp_path / "canary.jsonl"
    state = _state(message, sink, **record)
    server, url = _serve(state)
    try:
        result = _run_probe(url, "--canary-log", str(sink))
    finally:
        server.shutdown()
    assert len(state["requests"]) == 1, "the probe must drive exactly one turn"
    return result


@pytest.mark.parametrize("answer", [ANNOUNCING_ANSWER, ANNOUNCING_ANSWER_ZH])
def test_an_announced_tool_call_with_zero_batches_fails(tmp_path, answer):
    """The P1-2 class, in English and in Chinese."""
    result = _probe(answer, tmp_path, tool_batches=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "announces a tool call" in result.stdout
    assert "tool_batches=0" in result.stdout


def test_the_same_answer_passes_when_the_tool_actually_ran(tmp_path):
    """Announcing and then DOING it is a healthy turn. The narration alone must
    never fail a pool — that would reject real answers on a weight change."""
    result = _probe(ANNOUNCING_ANSWER, tmp_path, tool_batches=2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tool_batches=2" in result.stdout


def test_a_silent_zero_batch_turn_still_passes(tmp_path):
    """`tool_batches == 0` is legitimate on its own — `guard_node` and any
    knowledge answer produce it. Only the ANNOUNCEMENT makes it a defect."""
    result = _probe(HONEST_ANSWER, tmp_path, tool_batches=0)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_auxiliary_without_a_lookup_verb_is_not_an_announcement(tmp_path):
    """"Let me know" / "I'll be honest" must not convict. The verb is required."""
    result = _probe(NEAR_MISS_ANSWER, tmp_path, tool_batches=0)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_unavailable_ledger_never_convicts(tmp_path):
    """A crashed turn reports `tool_ledger_status: "unavailable"`; its 0 is an
    absence of measurement, not an observed zero, and must not fail the pool."""
    result = _probe(ANNOUNCING_ANSWER, tmp_path, tool_batches=0,
                    tool_ledger_status="unavailable")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_missing_tool_batches_key_never_convicts(tmp_path):
    """A producing build that predates the field reports no `tool_batches` at
    all. Absent is not zero."""
    result = _probe(ANNOUNCING_ANSWER, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_without_the_sink_the_check_cannot_and_does_not_fire(tmp_path):
    """No `--canary-log` means no second half of the evidence. The probe must
    stay silent rather than convict on the announcement string alone."""
    state = _state(ANNOUNCING_ANSWER)
    server, url = _serve(state)
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "announces a tool call" not in result.stdout


@pytest.mark.parametrize("marker", [STUB_MARKER_EN, STUB_MARKER_ZH])
def test_the_runtimes_own_marker_fails_without_any_canary_record(tmp_path, marker):
    """`agent_loop._mark_announced_stub` appends an explicit sentence when its
    bounded retry ALSO produced no tool call. That is the runtime admitting the
    lookup did not run, so it needs no record to be believed — and it must be
    caught even when the deploy path has no readable sink. `tool_batches` is 1
    here, so only the marker can fail this run."""
    state = _state(f"Here is what I found in {GROUNDING} for that postcode. {marker}")
    server, url = _serve(state)
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "announced-but-unexecuted marker" in result.stdout
