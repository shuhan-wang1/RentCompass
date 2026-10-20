"""A provider call made from post-turn background work must carry the turn's id.

In one measured 8-minute production window, 40 of 93 billed DeepSeek calls logged
`request_id: "-"` and belonged to no canary record: post-turn critic regeneration,
on-demand scraping and auto-memory writes, all spawned on plain threads and
executors. asyncio propagates a ContextVar across `create_task` / `call_soon` /
`run_coroutine_threadsafe`; `threading.Thread` and `Executor.submit` do not.

Two properties are pinned here, and they pull in opposite directions:

* the spawned work must INHERIT the request id, so its spend is attributable; and
* it must NOT re-open the turn's observation window, because the canary record for
  that turn has already been written and a late append would make the in-memory
  turn disagree with the line on disk.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "app"), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from uk_rent_agent.observability import (  # noqa: E402
    JsonFormatter, bind_current_context, request_context, request_id_var,
    spawn_observed_thread, submit_with_context,
)


def _load_turn_observations():
    spec = importlib.util.spec_from_file_location(
        "turn_observations_f5b", _ROOT / "app" / "core" / "turn_observations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["turn_observations_f5b"] = mod
    spec.loader.exec_module(mod)
    return mod


tobs = _load_turn_observations()


def _seen_request_id(spawn):
    seen = {}
    with request_context("turn-abc123", "user-1"):
        spawn(lambda: seen.setdefault("id", request_id_var.get())).join()
    return seen.get("id")


def test_a_bare_thread_loses_the_request_id():
    """The defect itself, pinned so the fix cannot be quietly reverted."""
    def spawn(fn):
        t = threading.Thread(target=fn)
        t.start()
        return t

    assert _seen_request_id(spawn) == "-"


def test_spawn_observed_thread_carries_the_request_id():
    def spawn(fn):
        return spawn_observed_thread(fn)

    assert _seen_request_id(spawn) == "turn-abc123"


def test_submit_with_context_carries_the_request_id_into_an_executor():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        with request_context("turn-exec", "user-1"):
            future = submit_with_context(pool, request_id_var.get)
        assert future.result() == "turn-exec"


def test_a_bound_callable_is_reusable_and_concurrency_safe():
    """A Context cannot be entered twice at once; the binder must copy per call."""
    with request_context("turn-reuse", "user-1"):
        bound = bind_current_context(request_id_var.get)

    barrier = threading.Barrier(4)

    def _run():
        barrier.wait(timeout=5)
        return bound()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result(timeout=5) for f in [pool.submit(_run) for _ in range(4)]]

    assert results == ["turn-reuse"] * 4


def test_a_provider_call_from_a_background_task_carries_the_parent_request_id():
    """The end-to-end shape: the log line a post-turn provider call emits."""
    import logging

    captured = {}

    def _post_turn_provider_call():
        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1,
            "HTTP Request: POST https://api.deepseek.com/chat/completions 200 OK",
            None, None)
        captured["line"] = JsonFormatter().format(record)

    with request_context("agenttest-t1-b-fc", "user-1"):
        tobs.begin_turn()
        try:
            spawn = tobs.bind_post_turn_context(_post_turn_provider_call)
        finally:
            tobs.end_turn()

    thread = threading.Thread(target=spawn)
    thread.start()
    thread.join()

    import json as _json
    assert _json.loads(captured["line"])["request_id"] == "agenttest-t1-b-fc"


def test_post_turn_work_does_not_reopen_the_already_written_turn_record():
    windows = {}

    def _late_work():
        windows["seen"] = tobs.current()
        tobs.note_raw_llm_call(
            "rawds:late", usage_blob={"prompt_tokens": 10, "completion_tokens": 5},
            configured_model="deepseek-v4-flash")

    with request_context("turn-late", "user-1"):
        obs = tobs.begin_turn()
        spawn = tobs.bind_post_turn_context(_late_work)
        before = tobs.detached_llm_call_count()
        tobs.end_turn()

    thread = threading.Thread(target=spawn)
    thread.start()
    thread.join()

    assert windows["seen"] is None, "a closed turn must not be re-opened"
    assert obs["llm_usage_calls"] == [], "the emitted record must not gain a late call"
    # ...and the billed call is still COUNTED, as the detached spend it is.
    assert tobs.detached_llm_call_count() == before + 1


def test_detached_call_counter_only_counts_calls_with_no_window():
    with request_context("turn-counted", "user-1"):
        tobs.begin_turn()
        before = tobs.detached_llm_call_count()
        assert tobs.note_raw_llm_call(
            "rawds:inside", usage_blob={"prompt_tokens": 3, "completion_tokens": 1},
            configured_model="deepseek-v4-flash") is True
        assert tobs.detached_llm_call_count() == before
        tobs.end_turn()


@pytest.mark.parametrize("helper", [spawn_observed_thread, submit_with_context])
def test_helpers_are_exported(helper):
    assert callable(helper)
