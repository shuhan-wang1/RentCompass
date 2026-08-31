"""`deploy/probe_pool_answer.py` must fail a pool that cannot ANSWER.

`/ready` and `set_canary_weight.sh::verify_local` only prove identity: the
readiness LLM check asserts a non-empty credential and reports
``connectivity: "not_probed"``. On 2026-07-25 a stale ``DEEPSEEK_MODEL`` left
both pools green on `/ready` for a day. A greeting cannot detect it either —
`app/core/agent_loop.py::guard_node` answers greetings deterministically with
zero model calls. So the probe drives one real turn and reads the answer.

Every case here runs against a throwaway HTTP server this test starts on an
ephemeral loopback port. No real pool is ever contacted.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "deploy" / "probe_pool_answer.py"

# The real answer shape for the default query (benchmark case D1): a grounded
# `check_safety` answer must cite data.police.uk.
GOOD_ANSWER = (
    "Peckham (SE15 5DP) recorded 412 crimes in the latest month according to "
    "data.police.uk, which is around the Southwark average."
)


def _handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            state.setdefault("requests", []).append(
                {"path": self.path, "body": body,
                 "request_id": self.headers.get("X-Request-Id", "")}
            )
            payload = json.dumps(state["payload"]).encode("utf-8")
            self.send_response(state.get("status", 200))
            self.send_header("Content-Type", "application/json")
            for name, value in state.get("headers", {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _serve(state: dict):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _run_probe(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), "--url", url, "--timeout", "10", *args],
        text=True, capture_output=True, timeout=60, check=False,
    )


def _state(message: str, *, arch: str = "manager_v1", specialists: str | None = "1",
           response_type: str = "chat", status: int = 200) -> dict:
    headers = {
        "X-Agent-Arch": arch,
        "X-Agent-Version": "b" * 40,
        "X-Agent-Outcome": "ok",
    }
    # specialists=None reproduces a pool that does not emit the header at all.
    if specialists is not None:
        headers["X-Agent-Specialists"] = specialists
    return {
        "status": status,
        "payload": {"response_type": response_type, "message": message,
                    "conversation_id": "c1", "turn_id": "t1"},
        "headers": headers,
    }


def test_a_grounded_answer_passes_and_drives_exactly_one_turn():
    state = _state(GOOD_ANSWER)
    server, url = _serve(state)
    try:
        result = _run_probe(url, "--expect-arch", "manager_v1",
                            "--expect-specialists", "1")
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("PASS answer-probe:")
    assert len(state["requests"]) == 1
    request = state["requests"][0]
    assert request["path"] == "/api/alex"
    # The default query must not be one guard_node short-circuits (greeting, rent
    # conversion, statutory money) — those answer with zero model calls.
    assert request["body"]["message"] == "Is Peckham (SE15 5DP) safe to live in?"
    # A fresh idempotency key per run: app/app.py replays a stored response for a
    # repeated X-Request-Id, which would prove nothing about the pool right now.
    assert request["request_id"].startswith("answer-probe-")


def test_every_canned_renderer_is_rejected():
    """The 2026-07-25 shape: HTTP 200, correct identity, canned text."""
    canned = (
        "Sorry — this turn ran long, so here is a brief answer from what I have "
        "gathered so far (it may be incomplete): data.police.uk",
        "Sorry — I couldn't retrieve reliable specific figures right now, so here "
        "is what I have verified: data.police.uk",
        "Sorry, I don't have reliable data to give specific figures for this right "
        "now. data.police.uk",
        "Sorry — I couldn't put that answer together properly. data.police.uk",
        "Hi! I'm Alex, your UK student-housing assistant. data.police.uk",
        "I'm here to help! What would you like to know? data.police.uk",
        "抱歉，我暂时无法获取可靠数据来回答这个问题里的具体数字 data.police.uk",
    )
    for message in canned:
        state = _state(message)
        server, url = _serve(state)
        try:
            result = _run_probe(url)
        finally:
            server.shutdown()
        assert result.returncode == 1, message
        assert "canned/fallback renderer" in result.stdout, message


def test_an_answer_without_tool_grounding_is_rejected():
    state = _state("Peckham is generally fine, in my experience.")
    server, url = _serve(state)
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "grounding marker" in result.stdout


def test_identity_mismatch_and_error_shapes_fail():
    for kwargs, marker in (
        ({"arch": "fc_loop"}, "expected 'manager_v1'"),
        ({"specialists": "0"}, "expected '1'"),
        ({"response_type": "error"}, "response_type=error"),
    ):
        state = _state(GOOD_ANSWER, **kwargs)
        server, url = _serve(state)
        try:
            result = _run_probe(url, "--expect-arch", "manager_v1",
                                "--expect-specialists", "1")
        finally:
            server.shutdown()
        assert result.returncode == 1, kwargs
        assert marker in result.stdout, (kwargs, result.stdout)


def test_a_non_200_and_an_unreachable_pool_both_fail_closed():
    state = _state(GOOD_ANSWER, status=502)
    server, url = _serve(state)
    port = server.server_port
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "HTTP 502" in result.stdout

    # The same port with nothing listening: a refused connection is a failed probe,
    # never a pass by omission.
    closed = _run_probe(f"http://127.0.0.1:{port}")
    assert closed.returncode == 1
    assert "did not answer" in closed.stdout


def test_a_not_instrumented_canary_record_fails_when_the_sink_is_given(tmp_path):
    state = _state(GOOD_ANSWER)
    server, url = _serve(state)
    sink = tmp_path / "canary.jsonl"
    try:
        # First run with no sink: the record does not exist yet, and the probe
        # must not require it unless asked.
        assert _run_probe(url).returncode == 0
        request_id = state["requests"][-1]["request_id"]
        sink.write_text(
            json.dumps({"event": "canary.turn", "request_id": request_id,
                        "llm_usage_status": "not_instrumented"}) + "\n",
            encoding="utf-8",
        )
        # A sink whose record for THIS request says the model usage was never
        # observed cannot authorise exposure.
        result = _run_probe(url, "--canary-log", str(sink))
        assert result.returncode == 1
        assert "not_instrumented" in result.stdout

        sink.write_text(
            "\n".join(
                json.dumps({"event": "canary.turn", "request_id": rid,
                            "llm_usage_status": "complete"})
                for rid in ("stale-id", "another-id")
            ) + "\n",
            encoding="utf-8",
        )
        # ...and a sink with no record for this exact request is equally a failure:
        # a stale row from an earlier turn must never stand in for this one.
        missing = _run_probe(url, "--canary-log", str(sink))
        assert missing.returncode == 1
        assert "no canary record" in missing.stdout
    finally:
        server.shutdown()


def test_the_legacy_pool_may_omit_the_specialist_header_entirely():
    """`X-Agent-Specialists` does not exist on the commit the legacy pool runs: it
    is the standing rollback escape hatch and is never recreated, so the header
    added in this release is simply absent there and `header()` returns "".

    `set_canary_weight.sh::verify_local` has exempted exactly that case since the
    header was introduced; the probe did not, so `verify_answer legacy ... 0` — the
    call every weight > 0 makes — failed on ''  != '0' and blocked the whole rollout
    ladder, including the 5% first step, on a pool that was perfectly healthy."""
    state = _state(GOOD_ANSWER, arch="legacy", specialists=None)
    server, url = _serve(state)
    try:
        # Verbatim the expectation set_canary_weight.sh::verify_answer passes.
        result = _run_probe(url, "--expect-arch", "legacy", "--expect-specialists", "0")
        # `none` is the other spelling verify_local compares against.
        spelled = _run_probe(url, "--expect-arch", "legacy",
                             "--expect-specialists", "none")
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert spelled.returncode == 0, spelled.stdout + spelled.stderr


def test_a_candidate_that_omits_the_specialist_header_is_still_refused():
    """The exemption is legacy-only. A candidate that cannot state its specialist
    bit is the exact failure the manager_v1 rollout is gated on, so an absent
    header there must not be laundered into a 0."""
    for arch, expected in (("manager_v1", "1"), ("fc_loop", "0")):
        state = _state(GOOD_ANSWER, arch=arch, specialists=None)
        server, url = _serve(state)
        try:
            result = _run_probe(url, "--expect-arch", arch,
                                "--expect-specialists", expected)
        finally:
            server.shutdown()
        assert result.returncode == 1, (arch, result.stdout)
        assert "specialists=''" in result.stdout, (arch, result.stdout)


def test_a_clarification_is_inconclusive_rather_than_a_failure():
    """`app/app.py` answers `response_type: "clarification"` when the graph asked a
    follow-up question (the missing-area / soft-criteria gates). It is a real reply
    but can never carry the tool grounding, so the substring check made it a hard
    FAIL — one clarification would have killed a weight change. It gets its own
    exit code instead, and set_canary_weight.sh retries once."""
    state = _state("Which city are you looking in?", response_type="clarification")
    server, url = _serve(state)
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.startswith("INCONCLUSIVE answer-probe:")
    assert "clarif" in result.stdout

    # ...but a canned renderer wearing a clarification's response_type is still a
    # FAIL: the fallback markers are checked first, on purpose.
    canned = _state("Sorry — I couldn't put that answer together properly.",
                    response_type="clarification")
    server, url = _serve(canned)
    try:
        result = _run_probe(url)
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "canned/fallback renderer" in result.stdout


def test_the_probe_is_stdlib_only_so_it_runs_inside_the_deploy_path():
    source = PROBE.read_text(encoding="utf-8")
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    }
    assert imports <= {"argparse", "json", "sys", "urllib", "uuid"}, imports
