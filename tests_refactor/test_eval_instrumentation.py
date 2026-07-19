"""Unit tests for the offline-eval instrumentation (Phase 3).

All tests use FAKES only — no network, no paid API calls. They prove:
  (a) flag OFF  -> wrappers are no-ops, outputs/behaviour unchanged, no events;
  (b) flag ON   -> a fake LLM call emits exactly one llm_call event w/ tokens;
  (c) flag ON   -> a tool call emits a tool_call event;
  (d) pricing loader returns None cost when a model's prices are null.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from evaluation.metrics import collector, pricing
from evaluation.metrics.fake_llm import FakeChatModel, make_fake_model


@pytest.fixture(autouse=True)
def _eval_off(monkeypatch):
    """Ensure each test starts with the env flag OFF and sink reset."""
    monkeypatch.delenv("RENTCOMPASS_EVAL", raising=False)
    monkeypatch.delenv("RENTCOMPASS_EVAL_LOG", raising=False)
    collector._refresh_env_flag()
    collector.reset_sink()
    yield
    collector._refresh_env_flag()
    collector.reset_sink()


def _read_events(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# (a) flag OFF -> no-op
# --------------------------------------------------------------------------- #
def test_off_is_not_active():
    assert collector.is_active() is False


def test_off_instrument_returns_same_object():
    model = FakeChatModel(purpose="responder", responses={"responder": "hi"})
    same = collector.instrument_chat_model(model, provider="deepseek", model_name="x", purpose="responder")
    assert same is model
    # No callback was attached, so behaviour is unchanged.
    assert not (getattr(model, "callbacks", None) or [])


def test_off_no_events_and_unchanged_output(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("RENTCOMPASS_EVAL_LOG", str(log))
    collector.reset_sink()
    model = make_fake_model("responder", {"responder": "canned answer"})
    out = model.invoke("anything")
    assert out.content == "canned answer"
    # record_* helpers are hard no-ops when inactive.
    collector.record_llm_call(provider="deepseek", model="x", input_tokens=1, output_tokens=1)
    collector.record_turn(route={"tool": "x"})
    assert _read_events(str(log)) == []


# --------------------------------------------------------------------------- #
# (b) flag ON -> exactly one llm_call event with correct token fields
# --------------------------------------------------------------------------- #
def test_on_llm_call_emits_single_event_with_tokens(tmp_path):
    log = tmp_path / "events.jsonl"
    with collector.capture_run("run-b", "case-b", "fake", log_path=str(log)):
        model = make_fake_model(
            "responder", {"responder": "hello world"},
            prompt_tokens=11, completion_tokens=7, cached_tokens=3,
        )
        out = model.invoke("prompt text")
        assert out.content == "hello world"  # output NOT altered by instrumentation

    events = _read_events(str(log))
    llm_events = [e for e in events if e["type"] == "llm_call"]
    assert len(llm_events) == 1
    ev = llm_events[0]
    assert ev["provider"] == "deepseek"
    assert ev["purpose"] == "responder"
    assert ev["input_tokens"] == 11
    assert ev["output_tokens"] == 7
    assert ev["cached_tokens"] == 3
    assert ev["success"] is True
    assert ev["run_id"] == "run-b" and ev["case_id"] == "case-b" and ev["config"] == "fake"
    assert "ts_monotonic" in ev


def test_env_flag_activates_without_context(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("RENTCOMPASS_EVAL", "1")
    monkeypatch.setenv("RENTCOMPASS_EVAL_LOG", str(log))
    collector._refresh_env_flag()
    collector.reset_sink()
    assert collector.is_active() is True
    model = make_fake_model("intent", {"intent": '{"tool": "search_properties"}'})
    model.invoke("q")
    llm_events = [e for e in _read_events(str(log)) if e["type"] == "llm_call"]
    assert len(llm_events) == 1


# --------------------------------------------------------------------------- #
# (c) flag ON -> tool_call event
# --------------------------------------------------------------------------- #
def test_on_tool_call_emits_event(tmp_path):
    from core.tool_system import Tool, ToolRegistry

    log = tmp_path / "events.jsonl"
    idem = tmp_path / "idem.sqlite3"
    tool = Tool(
        name="ping",
        description="test tool",
        func=lambda **kw: {"success": True, "pong": kw.get("x")},
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": []},
    )
    from uk_rent_agent.tools.idempotency import IdempotencyStore

    reg = ToolRegistry(idempotency_store=IdempotencyStore(idem))
    reg.register(tool)

    with collector.capture_run("run-c", "case-c", "fake", log_path=str(log)):
        result = asyncio.run(reg.execute_tool("ping", x="secret-value"))

    assert result.success is True
    assert result.data["pong"] == "secret-value"  # behaviour unchanged
    tool_events = [e for e in _read_events(str(log)) if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    ev = tool_events[0]
    assert ev["tool"] == "ping"
    assert ev["success"] is True
    assert ev["mcp"] is False
    assert ev["empty_result"] is False
    # Raw args must NEVER be logged — only a hash.
    assert "secret-value" not in json.dumps(ev)
    assert isinstance(ev["args_hash"], str) and len(ev["args_hash"]) == 16


# --------------------------------------------------------------------------- #
# (d) pricing loader returns None when prices null
# --------------------------------------------------------------------------- #
def test_pricing_null_returns_none_cost():
    pr = pricing.load_pricing()
    # deepseek-v4-pro gained confirmed 2026-07 pricing (it used to be the null example);
    # the null-price semantics are now covered by the unknown-model path only.
    assert pr.price_for("deepseek-v4-pro") is not None
    assert pr.cost("deepseek-v4-pro", input_tokens=1000, output_tokens=500) is not None
    # unknown model -> None.
    assert pr.price_for("no-such-model") is None
    assert pr.cost("no-such-model", input_tokens=10) is None
    assert pr.price_source and pr.price_as_of


def test_pricing_confirmed_model_computes_cost():
    pr = pricing.load_pricing()
    price = pr.price_for("deepseek-chat")
    assert price is not None and price.confirmed is True
    # 1M cache-miss input @ 0.14 + 1M output @ 0.28 = 0.42 USD
    cost = pr.cost("deepseek-chat", input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0)
    assert cost == pytest.approx(0.14 + 0.28)
    # cached tokens billed at the cheaper cache-hit rate
    cost_cached = pr.cost("deepseek-chat", input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000)
    assert cost_cached == pytest.approx(0.0028)


def test_pricing_null_from_custom_file(tmp_path):
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        "price_source: test\nprice_as_of: 2026-01-01\nper_tokens: 1000000\ncurrency: USD\n"
        "models:\n  foo:\n    input: null\n    cached_input: null\n    output: null\n",
        encoding="utf-8",
    )
    pr = pricing.load_pricing(str(cfg))
    assert pr.cost("foo", input_tokens=999, output_tokens=999) is None
