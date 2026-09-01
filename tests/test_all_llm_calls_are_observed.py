"""Every LLM call must reach the counter the gate reads.

`install_observer` attaches a LangChain callback, so it only ever saw models built
through `ModelRouter`. Two production paths were not:

  * `core/llm_interface._call_deepseek` drives the raw `openai` SDK directly;
  * `core/llm_config._deepseek_llm` returned an unobserved `ChatOpenAI`.

Calls on those paths were real and billed. In the 2026-07-25 round of record there were
**48 of them at p50 934ms**, and `llm_calls` counted none — so "the median turn makes 2
LLM calls" and "3+ call turns never make the bar" were both computed from a counter that
was undercounting. Wall-clock latency was unaffected; the per-call attribution was not.

The bypass was never hidden. `llm_interface`'s own module docstring says "calls that
bypass ModelRouter". It was known and simply never wired — the same shape as
`canary_report.py --since` computing a window it then does not filter on, as the health
monitor computing telemetry growth it never alerted on, and as `route_source` being
returned and read by nobody. Six instances of one defect class.

So the tests below are not only "is it wired now". The last one is a SOURCE GUARD: a new
unobserved client cannot be added without failing a test.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from core import turn_observations as obs


REPO = pathlib.Path(__file__).resolve().parents[1]

# The ONLY places allowed to construct a chat client. Everything else must go through
# ModelRouter or one of these. Adding a path here is a deliberate act that shows up in
# review; forgetting to observe one no longer is.
_CLIENT_CONSTRUCTION_ALLOWLIST = {
    "src/uk_rent_agent/llm/router.py",   # installs the observer itself
    "app/core/llm_config.py",            # installs the observer itself (_deepseek_llm)
    "app/core/llm_interface.py",         # raw SDK; reports via note_raw_llm_call
}


class _Resp:
    """A raw OpenAI-SDK-shaped response: no LangChain envelope."""
    def __init__(self, prompt=100, completion=20, cached=64):
        self.usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_cache_hit_tokens": cached}


@pytest.fixture()
def turn():
    obs.begin_turn()
    yield
    obs.end_turn()


# --------------------------------------------------------------------------- #
# The raw-SDK path is counted, and counted the same way as the LangChain one.  #
# --------------------------------------------------------------------------- #

def test_a_raw_only_turn_is_not_reported_as_uninstrumented(turn):
    """snapshot() returns all-None unless an observer was installed, and that flag was
    only ever set by the LangChain path. A turn whose only LLM work is raw-SDK would have
    had its record taken and then thrown away."""
    obs.note_raw_llm_call(99, usage_blob=_Resp().usage, configured_model="m")
    assert obs.snapshot()["llm_usage_status"] != "not_instrumented"


def test_a_raw_sdk_call_reaches_the_counter(turn):
    assert obs.note_raw_llm_call(1, usage_blob=_Resp().usage,
                                 configured_model="deepseek-v4-flash") is True
    snap = obs.snapshot()
    calls = snap.get("llm_usage_calls")
    assert calls is not None, "a raw-SDK-only turn must not snapshot as not_instrumented"
    assert len(calls) == 1, "the call the gate could not see must now be visible"
    assert calls[0]["input_tokens"] == 100
    assert calls[0]["output_tokens"] == 20
    assert calls[0]["cache_read_tokens"] == 64
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert snap["llm_usage_status"] == "complete"


def test_the_same_run_is_not_counted_twice(turn):
    assert obs.note_raw_llm_call(7, usage_blob=_Resp().usage, configured_model="m") is True
    assert obs.note_raw_llm_call(7, usage_blob=_Resp().usage, configured_model="m") is False
    assert len(obs.snapshot()["llm_usage_calls"]) == 1


def test_usage_absent_is_recorded_as_missing_not_as_zero(turn):
    """A call that provably happened but reported no tokens must not let the remaining
    calls' totals stand in for the whole turn — the same rule note_llm_usage follows."""
    assert obs.note_raw_llm_call(2, usage_blob={}, configured_model="m") is False
    snap = obs.snapshot()
    assert snap.get("llm_usage_status") != "complete"


def test_outside_a_turn_it_is_a_no_op(turn=None):
    obs.end_turn()
    assert obs.note_raw_llm_call(3, usage_blob=_Resp().usage, configured_model="m") is False


def test_deepseek_cache_hits_are_a_breakdown_not_an_addition(turn):
    """DeepSeek reports cache hits INSIDE prompt_tokens. Adding them would double-count
    spend — the raw path must follow the same convention as the LangChain path."""
    obs.note_raw_llm_call(4, usage_blob={"prompt_tokens": 1000, "completion_tokens": 10,
                                         "prompt_cache_hit_tokens": 900},
                          configured_model="m")
    call = obs.snapshot()["llm_usage_calls"][0]
    assert call["input_tokens"] == 1000, "cache hits are part of prompt_tokens"
    assert call["cache_read_tokens"] == 900


# --------------------------------------------------------------------------- #
# The two bypassing call sites are actually wired.                            #
# --------------------------------------------------------------------------- #

def test_llm_interface_reports_its_raw_calls():
    src = (REPO / "app/core/llm_interface.py").read_text()
    assert "note_raw_llm_call" in src, (
        "_call_deepseek drives the raw SDK; without this its calls never reach llm_calls")


def test_llm_config_attaches_the_observer_at_construction():
    src = (REPO / "app/core/llm_config.py").read_text()
    assert "install_observer" in src, (
        "_deepseek_llm returned an unobserved ChatOpenAI; attaching at construction is "
        "what stops a new caller from forgetting")


# --------------------------------------------------------------------------- #
# SOURCE GUARD — the point of this file.                                      #
# --------------------------------------------------------------------------- #

def test_no_new_unobserved_chat_client_can_be_added():
    """Fail if any file outside the allowlist constructs a chat client directly.

    Wiring the two known bypasses fixes today. This is what stops tomorrow's: the defect
    has recurred six times in this repo, always as "the value was produced and nobody
    consumed it", and every previous fix addressed one instance.
    """
    offenders: list[str] = []
    for path in list(REPO.glob("app/**/*.py")) + list(REPO.glob("src/**/*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel in _CLIENT_CONSTRUCTION_ALLOWLIST or "/tests/" in f"/{rel}":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in {"ChatOpenAI", "OpenAI", "AsyncOpenAI"}:
                offenders.append(f"{rel}:{node.lineno} constructs {name}")
    assert not offenders, (
        "unobserved LLM client construction — route it through ModelRouter, or attach "
        "install_observer / note_raw_llm_call and add the file to the allowlist:\n  "
        + "\n  ".join(offenders))


def test_the_source_guard_actually_scans_something():
    """Guard the guard: a scan that silently matches nothing would pass forever."""
    hits = 0
    for path in list(REPO.glob("app/**/*.py")) + list(REPO.glob("src/**/*.py")):
        if re.search(r"\b(ChatOpenAI|OpenAI)\s*\(", path.read_text()):
            hits += 1
    assert hits >= 2, "the allowlisted constructors should still be found by the scan"


# --------------------------------------------------------------------------- #
# The raw path's own bookkeeping must not undercount either.                   #
# --------------------------------------------------------------------------- #

def test_two_raw_calls_in_a_turn_are_both_counted_even_if_the_first_is_freed():
    """Regression: the run id was `id(resp)`.

    CPython recycles an address as soon as the object is freed, so two sequential
    calls in one turn could be handed the SAME id — and the second was then dropped
    by the de-duplication as a repeat of the first. Silent undercounting, inside the
    function whose entire purpose is to stop this path from undercounting. It only
    shows up when the first response is not held alive, i.e. in production and not
    in a test that keeps both in locals.
    """
    import gc

    from core import llm_interface

    obs.begin_turn()
    try:
        ids = []
        for _ in range(2):
            resp = _Resp()
            ids.append(id(resp))
            assert obs.note_raw_llm_call(
                f"rawds:{next(llm_interface._raw_call_seq)}",
                usage_blob=resp.usage, configured_model="deepseek-v4-flash") is True
            del resp
            gc.collect()
        snap = obs.snapshot()
        assert snap["llm_calls"] == 2, (
            "both billed calls must reach the counter regardless of object lifetime")
        assert len(snap["llm_usage_calls"]) == 2
    finally:
        obs.end_turn()


def test_the_raw_path_uses_a_monotonic_run_id_not_an_object_address():
    src = (REPO / "app/core/llm_interface.py").read_text()
    assert "note_raw_llm_call(id(resp)" not in src, (
        "id() is reused after garbage collection; two calls in one turn can collide "
        "and the second is silently de-duplicated away")
    assert "_raw_call_seq" in src


def test_a_provider_failure_on_the_raw_path_is_classified_not_swallowed(monkeypatch):
    """`_call_deepseek` catches every provider error into `return None` plus a print.

    The canary observer is a LangChain callback and there is no LangChain here, so
    those failures reached no counter at all — the same blind spot that let a day of
    HTTP 400s produce no alarm. `schemas_bound=False` is a FACT about this request
    (it binds no tools, functions or response_format), so a raw-path failure can
    never inflate the zero-tolerance provider_schema_400 metric.

    `_observer_installed` is a MODULE-level global that stays True once any client
    has been built, so it must be pinned: leaving it ambient makes these counters
    read 0 or null depending purely on which test ran first in the process.
    """
    src = (REPO / "app/core/llm_interface.py").read_text()
    assert "note_provider_error" in src

    monkeypatch.setattr(obs, "_observer_installed", True)
    obs.begin_turn()
    try:
        class _Boom(Exception):
            status_code = 400

        assert obs.note_provider_error(_Boom(), schemas_bound=False) == "other_400"
        snap = obs.snapshot()
        assert snap["provider_schema_400_count"] == 0
        assert snap["provider_other_400_count"] == 1
    finally:
        obs.end_turn()


def test_without_an_observer_the_same_turn_reports_null_not_zero(monkeypatch):
    """The fail-closed half of the pair above: "we looked and saw none" (0) and
    "nothing was watching" (null) must never collapse into the same number.

    NOTHING watching means BOTH observers off. There are two — the LangChain
    callback and the raw-SDK reporter — and either one being live means real
    observations exist and 0 is an honest count of them. Both are process-wide
    globals, so the raw one stays set for the rest of the session once any test in
    this file makes a raw call; pinning only the callback made this assertion
    depend on test order (CI seed 1009 found it)."""
    monkeypatch.setattr(obs, "_observer_installed", False)
    monkeypatch.setattr(obs, "_raw_observer_installed", False)
    obs.begin_turn()
    try:
        assert obs.snapshot()["provider_schema_400_count"] is None
    finally:
        obs.end_turn()


def test_the_nested_tool_internal_call_is_attributed_to_its_agent_scope():
    """A nested call made inside a specialist belongs to that specialist in
    `llm_usage.roles`, not to the whole turn. `_call_deepseek` passes
    agent_context=None, which means "read the live scope"."""
    from core.canary_telemetry import aggregate_llm_usage
    from uk_rent_agent.observability import agent_execution_context

    obs.begin_turn()
    try:
        with agent_execution_context(agent_role="listings", task_id="task:1",
                                     parent_task_id="turn:req-1"):
            obs.note_raw_llm_call("rawds:0", usage_blob=_Resp().usage,
                                  configured_model="deepseek-v4-flash",
                                  agent_context=None)
        usage = aggregate_llm_usage(obs.snapshot()["llm_usage_calls"])
        assert usage["roles"]["listings"]["calls"] == 1
        assert usage["roles"]["listings"]["input_tokens"] == 100
    finally:
        obs.end_turn()


def test_both_recorders_are_driven_from_the_same_place():
    """The eval collector and the canary observer had drifted: eval saw this path
    (48 calls at p50 934ms in the 2026-07-25 round), `llm_calls` saw none of them.
    Keeping the two calls adjacent in one function is what stops them re-drifting.
    """
    src = (REPO / "app/core/llm_interface.py").read_text()
    body = src[src.index("def _call_deepseek("):src.index("def call_ollama(")]
    assert "_record_deepseek_eval" in body and "note_raw_llm_call" in body
    # ...and the failure exit reports to both as well.
    failure = body[body.rindex("except Exception as e:"):]
    assert "_record_deepseek_eval" in failure and "note_provider_error" in failure


# --------------------------------------------------------------------------- #
# The raw path, driven through the REAL function body.                        #
# --------------------------------------------------------------------------- #
#
# Everything above this section reaches `note_raw_llm_call` / `note_provider_error`
# DIRECTLY, or asserts on a source-code slice. That guards against the recorders
# being deleted, and against a new unobserved client being added. It does not guard
# the one argument in this file with the largest blast radius:
# `note_provider_error(..., schemas_bound=False)`. Flipping that single keyword to
# True routes every provider 400 on the nested tool-internal path into the
# ZERO-TOLERANCE `provider_schema_400` counter, which trips `run_canary_gate.sh`
# into exit 3 and an automatic rollback to 0% candidate weight. Until these tests,
# a comment was the only thing holding it.

class _FakeCompletions:
    """`_call_deepseek` builds a NEW client per call, so the call counter has to
    live outside the client or every call looks like the first one."""

    def __init__(self, script, seen):
        self._script = script
        self._seen = seen

    def create(self, **kwargs):
        self._seen.append(kwargs)
        outcome = self._script(len(self._seen))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeOpenAI:
    """Stands in for the raw SDK client. Constructed INSIDE _call_deepseek, so the
    only way to inject it is through the `openai` module the function imports."""

    def __init__(self, script, seen):
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _FakeCompletions(script, seen)


class _SdkResponse:
    def __init__(self, text="answer", prompt=100, completion=20, cached=0):
        self.usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_cache_hit_tokens": cached}
        message = type("_Msg", (), {"content": text})()
        self.choices = [type("_Choice", (), {"message": message})()]


class _ProviderError(Exception):
    def __init__(self, status):
        super().__init__(f"provider said {status}")
        self.status_code = status


@pytest.fixture()
def raw_client(monkeypatch):
    """Install a fake `openai.OpenAI` and return a factory that scripts responses."""
    import openai

    seen: list = []

    def _install(script):
        def _factory(**_kwargs):
            return _FakeOpenAI(script, seen)
        monkeypatch.setattr(openai, "OpenAI", _factory)
        return seen

    return _install


def test_the_real_call_deepseek_body_accounts_a_success_exactly_once(turn, raw_client):
    from core import llm_interface

    raw_client(lambda n: _SdkResponse(text="ok", prompt=100, completion=20))

    assert llm_interface._call_deepseek("hello") == "ok"

    from core.canary_telemetry import aggregate_llm_usage

    snap = obs.snapshot()
    assert snap["llm_calls"] == 1, "one provider call, one accounting"
    assert snap["llm_usage_status"] == "complete"
    usage = aggregate_llm_usage(snap["llm_usage_calls"])
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20
    assert snap["provider_schema_400_count"] == 0


def test_two_real_calls_get_distinct_run_ids(turn, raw_client):
    """`id(response)` was the de-duplication key: CPython recycles an address the
    moment an object is freed, so two sequential calls in a turn could be handed the
    SAME id and the second was silently dropped as a repeat — undercounting, in the
    function whose entire purpose is to stop this path undercounting."""
    from core import llm_interface

    raw_client(lambda n: _SdkResponse(text=f"answer {n}"))

    assert llm_interface._call_deepseek("first") == "answer 1"
    assert llm_interface._call_deepseek("second") == "answer 2"

    snap = obs.snapshot()
    assert snap["llm_calls"] == 2, "the second call must not be dropped as a repeat"
    run_ids = obs.current()["llm_runs_seen"]
    assert len(run_ids) == 2, run_ids
    assert all(str(rid).startswith("rawds:") for rid in run_ids), run_ids
    # The raw-SDK key space must not collide with LangChain's run UUIDs, or a
    # de-duplication in one path would silently drop a call in the other.
    assert all(":" in str(rid) for rid in run_ids)


def test_a_real_provider_400_never_touches_the_zero_tolerance_counter(turn, raw_client):
    """This request binds no tools, no functions and no response_format, so a 400 on
    it is structurally NOT a strict-schema rejection. Counting it as one would trip
    the release gate's exit 3 and roll the candidate back to 0% on a failure that
    has nothing to do with strict function calling."""
    from core import llm_interface

    raw_client(lambda n: _ProviderError(400))

    assert llm_interface._call_deepseek("hello") is None

    snap = obs.snapshot()
    assert snap["provider_schema_400_count"] == 0, (
        "schemas_bound=False is a FACT about this request, not a default")
    assert snap["provider_other_400_count"] == 1
    assert snap["llm_calls"] == 0, "a failed call bills no observed usage"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_other_provider_failures_are_classified_not_swallowed(turn, raw_client, status):
    """Before the hook existed these returned None plus a print, and were counted
    nowhere — which is how a whole day of provider errors produced no alarm."""
    from core import llm_interface

    raw_client(lambda n: _ProviderError(status))

    assert llm_interface._call_deepseek("hello") is None

    snap = obs.snapshot()
    assert snap["provider_schema_400_count"] == 0
    assert snap["provider_other_400_count"] == 0
    assert obs.current()["provider_error_count"] == 1


def test_a_success_and_a_failure_in_one_turn_are_both_accounted(turn, raw_client):
    from core import llm_interface

    raw_client(lambda n: _SdkResponse() if n == 1 else _ProviderError(400))

    assert llm_interface._call_deepseek("first") is not None
    assert llm_interface._call_deepseek("second") is None

    snap = obs.snapshot()
    assert snap["llm_calls"] == 1
    assert snap["provider_other_400_count"] == 1
    assert snap["provider_schema_400_count"] == 0
