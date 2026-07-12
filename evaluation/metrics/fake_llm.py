"""Deterministic fake-LLM seam for offline / unbilled eval runs.

Two seams cover 100% of model calls (matching the audit's two LLM shims):

* :func:`patch_model_router` replaces ``ModelRouter.create`` so every
  router-based call (intent, planner, responder/critic) returns a scripted
  :class:`FakeChatModel` instead of a real ``ChatOpenAI``. The fake is still run
  through :func:`evaluation.metrics.collector.instrument_chat_model`, so an
  active capture records a faithful ``llm_call`` event with token fields.
* :func:`patch_call_ollama` replaces ``app/core/llm_interface.call_ollama`` (the
  path used by memory + on-demand place classification) with a scripted stub that
  also emits a synthetic ``llm_call`` event.

Responses are keyed by *purpose* (router) / *tag* (call_ollama), falling back to a
``"default"`` entry. NOTHING here makes a network call.

Example
-------
    from evaluation.metrics import fake_llm, collector
    scripts = {"responder": "Here are three flats near UCL ...",
               "intent": '{"tool": "search_properties"}'}
    with collector.capture_run("run1", "case1", "fake"):
        with fake_llm.patch_model_router(scripts), fake_llm.patch_call_ollama({"default": "{}"}):
            ... drive the real graph ...  # zero paid calls
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from evaluation.metrics import collector


class FakeChatModel(BaseChatModel):
    """A canned LangChain chat model. Returns a fixed string for its purpose and
    reports deterministic token usage in both the OpenAI ``token_usage`` shape and
    the ``usage_metadata`` shape, so the collector's extractor is exercised."""

    responses: Dict[str, str] = {}
    purpose: str = "default"
    prompt_tokens: int = 11
    completion_tokens: int = 7
    cached_tokens: int = 0

    @property
    def _llm_type(self) -> str:  # pragma: no cover - trivial
        return "fake-chat"

    def _text(self) -> str:
        return self.responses.get(self.purpose, self.responses.get("default", "OK"))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        text = self._text()
        message = AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": self.prompt_tokens,
                "output_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "input_token_details": {"cache_read": self.cached_tokens},
            },
        )
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={
                "token_usage": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens,
                    "prompt_cache_hit_tokens": self.cached_tokens,
                    "prompt_cache_miss_tokens": self.prompt_tokens - self.cached_tokens,
                },
                "model_name": "fake-chat",
            },
        )


def make_fake_model(purpose: str, responses: Dict[str, str], **usage) -> Any:
    """Build a FakeChatModel for ``purpose`` and instrument it like the real one."""
    model = FakeChatModel(purpose=purpose, responses=dict(responses), **usage)
    return collector.instrument_chat_model(
        model, provider="deepseek", model_name="fake-chat", purpose=purpose
    )


@contextlib.contextmanager
def patch_model_router(responses: Dict[str, str], **usage):
    """Monkeypatch ``ModelRouter.create`` to return scripted fakes.

    ``responses`` maps purpose -> canned text (``"default"`` used as fallback).
    """
    from uk_rent_agent.llm import router as _router

    original = _router.ModelRouter.create

    def _fake_create(self, purpose, **route_kwargs):  # noqa: ANN001
        return make_fake_model(purpose, responses, **usage)

    _router.ModelRouter.create = _fake_create
    try:
        yield
    finally:
        _router.ModelRouter.create = original


@contextlib.contextmanager
def patch_call_ollama(responses: Dict[str, str], *, tag: str = "default"):
    """Monkeypatch ``core.llm_interface.call_ollama`` to a scripted stub.

    Emits a synthetic ``llm_call`` event (approximate token counts from text
    length) so fake e2e runs still produce memory/place-classify rows.
    """
    from core import llm_interface as _iface

    original = _iface.call_ollama

    def _fake_call_ollama(prompt, system_prompt=None, timeout=360):  # noqa: ANN001
        text = responses.get(tag, responses.get("default", "{}"))
        collector.record_llm_call(
            provider="deepseek",
            model="fake-chat",
            purpose="memory",
            input_tokens=len(str(prompt)) // 4,
            output_tokens=len(str(text)) // 4,
            cached_tokens=0,
            latency_ms=0.0,
            success=True,
        )
        return text

    _iface.call_ollama = _fake_call_ollama
    try:
        yield
    finally:
        _iface.call_ollama = original
