"""The critic's no-evidence HARD REPLACE is countable (app/core/langgraph_agent.py).

When ``retrieval_expected ∧ ¬verdict.grounded ∧ has_specific_price_claims ∧
¬_has_usable_retrieval_evidence``, the critic node REPLACES the generated answer with
``_artifact_grounded_fallback_answer(reason="no_reliable_numbers")`` (fc) or
``no_reliable_data_message`` (legacy).

On the 2026-07-25 round of record this fired on 3 of 98 eval cases — B8, B12 and G16, verified
by the canned opener "Sorry — I couldn't retrieve reliable specific figures right now, so here
is what I have verified:" appearing in exactly those three retained answer bodies. ALL THREE
recorded ``soft_wrapped=False``. Every counter the project has for "did the user get
boilerplate instead of an answer" is denominated in soft wraps, so this path was invisible to
all of them, and the record accordingly states that the canned-template fallback is gone. It
is not. HANDOFF §0's defect class: a value produced and never asserted on.

These tests pin the COUNTER only. The replacement's behaviour is deliberately unchanged —
reworking it is a behaviour change needing its own hypothesis and gate
(docs/evaluator_contract.md records the fail-closed variant as a CONFIRMED quality
regression, case A14), and the existing behaviour stays pinned by tests/test_fc_critic.py.

No live API: the regeneration LLM is stubbed, mirroring tests/test_fc_critic.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Pin the real source roots ahead of tests/ (stale shadow `core` copies live under tests/
# and would otherwise shadow the app packages under prepend mode). Mirrors test_agent_loop.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return _FakeResp(self.content)


def _all_events(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _events_of(path, event_type):
    return [e for e in _all_events(path) if e.get("type") == event_type]


def _no_evidence_state(msg="any 1-bed in Islington?"):
    """A legacy turn whose retrieval produced nothing usable and whose answer still asserts
    a price — the exact shape that drives the hard replace."""
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state(
        msg, extracted_context={"current_message": msg, "reply_language": "en"})
    state["tool_decision"] = {"tool": "search_properties"}   # retrieval_expected
    state["tool_raw_data"] = None                            # no usable evidence
    state["final_response"] = "1-bed flats in Islington are about £1,650 pcm."
    return state


def test_a_round_can_count_the_replacement_from_its_own_telemetry(lga, monkeypatch, tmp_path):
    """THE defect, stated without naming any new symbol: run a turn that IS hard-replaced and
    ask the round's own events.jsonl how many replacements happened.

    Before this change the answer was zero — the replacement wrote a log line and nothing
    structured, and ``soft_wrapped`` (the only counter in range) was False on all three cases
    it hit. This test therefore fails on the old behaviour for the RIGHT reason: the
    replacement is observed to happen, and the telemetry is observed not to record it.
    """
    pytest.importorskip("langgraph")
    from evaluation.metrics import collector
    import core.llm_config as llm_config
    from uk_rent_agent.agent.critic import no_reliable_data_message

    monkeypatch.setattr(llm_config, "get_react_llm",
                        lambda *a, **k: _FakeLLM("About £1,650 pcm on average."))
    log = str(tmp_path / "events.jsonl")
    with collector.capture_run("r0", case_id="B8", log_path=log):
        update = asyncio.run(lga._make_critic_node()(_no_evidence_state()))
    collector.reset_sink()

    events = _all_events(log)
    # 1. the replacement really happened on this turn ...
    assert update["final_response"] == no_reliable_data_message("en")
    # 2. ... and the soft-wrap counter the project reports cannot see it (the round's own
    #    finding: all three affected cases recorded soft_wrapped=False)
    assert _events_of(log, "turn_soft_wrap") == []
    # 3. ... so SOMETHING in this turn's telemetry must mark it, exactly once.
    marks = [e for e in events if e.get("reason") == "no_reliable_numbers"]
    assert len(marks) == 1, [e.get("type") for e in events]


def test_critic_hard_replace_emits_a_countable_event(lga, monkeypatch, tmp_path):
    """The counter fires. Before this change the replacement emitted only a log line, and
    the only structured signal in range (soft_wrapped) was False on all three cases it hit —
    so a round reported the path as absent."""
    pytest.importorskip("langgraph")
    from evaluation.metrics import collector
    import core.llm_config as llm_config
    from uk_rent_agent.agent.critic import no_reliable_data_message

    monkeypatch.setattr(llm_config, "get_react_llm",
                        lambda *a, **k: _FakeLLM("About £1,650 pcm on average."))

    log = str(tmp_path / "events.jsonl")
    state = _no_evidence_state()
    with collector.capture_run("r1", case_id="B8", config_name="legacy", log_path=log):
        update = asyncio.run(lga._make_critic_node()(state))
    collector.reset_sink()

    # the replacement still happened, unchanged
    assert update["final_response"] == no_reliable_data_message("en")

    events = _events_of(log, lga.CRITIC_HARD_REPLACE_EVENT)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["reason"] == "no_reliable_numbers"
    assert ev["variant"] == "generic_template"
    assert ev["tool"] == "search_properties"
    assert ev["reply_language"] == "en"
    # tagged with the round context, so a round can report it per case
    assert ev["case_id"] == "B8" and ev["run_id"] == "r1"


def test_hard_replace_event_is_distinct_from_critic_verdict(lga, monkeypatch, tmp_path):
    """It must NOT be emitted as another critic_verdict: run_benchmark derives
    critic_triggers from len(critic_verdicts), so reusing that type would inflate an
    existing metric in order to observe a new one."""
    pytest.importorskip("langgraph")
    from evaluation.metrics import collector
    import core.llm_config as llm_config

    monkeypatch.setattr(llm_config, "get_react_llm",
                        lambda *a, **k: _FakeLLM("About £1,650 pcm on average."))
    log = str(tmp_path / "events.jsonl")
    with collector.capture_run("r2", case_id="B8", log_path=log):
        asyncio.run(lga._make_critic_node()(_no_evidence_state()))
    collector.reset_sink()

    assert lga.CRITIC_HARD_REPLACE_EVENT != "critic_verdict"
    assert len(_events_of(log, lga.CRITIC_HARD_REPLACE_EVENT)) == 1
    # the verdict stream is untouched by the new counter
    verdicts = _events_of(log, "critic_verdict")
    assert verdicts and all("reason" not in v for v in verdicts)


def test_no_hard_replace_event_when_the_answer_is_kept(lga, monkeypatch, tmp_path):
    """Precision: a turn whose answer survives emits NO hard-replace event, so the counter
    is a count of replacements and not of critic activity."""
    pytest.importorskip("langgraph")
    from evaluation.metrics import collector
    import core.llm_config as llm_config

    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: _FakeLLM("unused"))
    state = _no_evidence_state()
    state["tool_decision"] = {"tool": "direct_answer"}   # non-retrieval turn: critic skips
    log = str(tmp_path / "events.jsonl")
    with collector.capture_run("r3", case_id="B1", log_path=log):
        asyncio.run(lga._make_critic_node()(state))
    collector.reset_sink()

    assert _events_of(log, lga.CRITIC_HARD_REPLACE_EVENT) == []


def test_hard_replace_counter_is_inert_outside_capture(lga, monkeypatch):
    """Instrumentation must never change production behaviour: with capture inactive the
    emitter is a no-op and the turn's result is identical."""
    pytest.importorskip("langgraph")
    from evaluation.metrics import collector
    import core.llm_config as llm_config
    from uk_rent_agent.agent.critic import no_reliable_data_message

    assert collector.is_active() is False
    monkeypatch.setattr(llm_config, "get_react_llm",
                        lambda *a, **k: _FakeLLM("About £1,650 pcm on average."))
    update = asyncio.run(lga._make_critic_node()(_no_evidence_state()))
    assert update["final_response"] == no_reliable_data_message("en")


def test_hard_replace_emitter_swallows_a_broken_collector(lga, monkeypatch):
    """Best-effort, like every other instrumentation call in this module: a collector that
    raises must not break the turn."""
    from evaluation.metrics import collector

    monkeypatch.setattr(collector, "is_active",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    lga._record_critic_hard_replace(reason="no_reliable_numbers", variant="generic_template",
                                   tool="search_properties", critic_attempts=1,
                                   reply_language="en")
