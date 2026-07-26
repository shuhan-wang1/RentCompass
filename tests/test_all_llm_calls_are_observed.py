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
