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
import importlib
import sys
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from evaluation.metrics import collector

#: Modules that bound ``call_ollama`` / ``_call_deepseek`` at import time.  Rebinding
#: ``core.llm_interface`` does not change these aliases, so each must be patched
#: individually -- and each must be IMPORTED first: a host that is merely not in
#: ``sys.modules`` yet (offline memory seeding and area recommendation are both
#: imported lazily, mid-run) would otherwise import the REAL provider after the patch
#: window opened and reach DeepSeek from an "offline" round.
#:
#: These two are leaf modules whose import costs nothing.  ``app`` is NOT one of
#: them; see ``_OPPORTUNISTIC_LLM_ALIAS_HOSTS``.
_DIRECT_LLM_ALIAS_HOSTS = (
    ("rag.agent_memory", "call_ollama"),
    ("core.recommend_areas", "_call_deepseek"),
)

#: Hosts patched ONLY if the process already imported them.  ``app`` resolves to
#: ``app/app.py``, whose import builds the Flask app, configures the auth and
#: conversation stores, initialises 14 tools, loads the property CSV and calls
#: ``_wire_canary_sink()``.  Force-importing that from inside a context manager
#: whose job is "swap two function references" ran the entire application startup
#: in the middle of an eval TURN, and the canary sink it wires defaults to
#: ``<runtime>/logs/canary-<arch>.jsonl`` when ``CANARY_LOG_PATH`` is unset -- so a
#: caller that had not been through ``run_benchmark._bootstrap_env`` would attach a
#: handler to the REAL production telemetry log.  If ``app`` is already imported,
#: its alias is real and worth patching; if it is not, importing it is far more
#: dangerous than the alias it would fix.
_OPPORTUNISTIC_LLM_ALIAS_HOSTS = (
    ("app", "call_ollama"),
)


def _resolve_alias_host(module_name: str, *, may_import: bool) -> List[Any]:
    """Every live object that ``import <module_name>`` could hand a caller.

    Normally exactly one, but ``sys.modules["pkg.mod"]`` and the ``pkg.mod``
    ATTRIBUTE on the parent package can diverge -- a test that deletes the
    ``sys.modules`` entry, lets something re-import it, and then restores the entry
    leaves the parent attribute pointing at the second copy.  ``from pkg import
    mod`` reads the attribute, so patching only the ``sys.modules`` object left the
    copy that callers actually use bound to the REAL provider, and an "offline"
    round reached DeepSeek for real.  Both are returned so both get patched.
    """
    modules: List[Any] = []
    module = sys.modules.get(module_name)
    if module is None and may_import:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # A host that cannot be imported in this process cannot alias the
            # provider in it either; skip it rather than fail the whole patch.
            module = None
    if module is not None:
        modules.append(module)
    if "." in module_name:
        parent_name, _, child = module_name.rpartition(".")
        parent = sys.modules.get(parent_name)
        attached = getattr(parent, child, None) if parent is not None else None
        if attached is not None and all(attached is not m for m in modules):
            modules.append(attached)
    return modules


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

    def _fake_call_deepseek(
        prompt, system_prompt=None, timeout=360, temperature=0.1, max_tokens=4000,
        purpose="memory", **_kwargs,
    ):  # noqa: ANN001
        # ``purpose`` and ``**_kwargs`` keep this twin accepting everything the real
        # ``llm_interface._call_deepseek`` accepts. Without them the first caller to
        # start passing ``purpose=`` would get a TypeError offline -- and because
        # nearly every call site sits in a ``try/except``, that TypeError would be
        # swallowed and read as a tool failure rather than as a broken shim.
        return _fake_call_ollama(prompt, system_prompt, timeout)

    # Several older modules imported these functions directly at module load time. Merely
    # rebinding ``core.llm_interface.call_ollama`` does not change those aliases, which
    # allowed offline memory seeding / area recommendation to reach DeepSeek. Patch the
    # known aliases and restore their exact previous objects on exit.
    #
    # FORCE-IMPORT each direct alias host first. ``sys.modules.get`` only saw hosts that
    # had already been imported, so a host imported LATER (lazily, mid-run) rebound the
    # real provider inside the patch window and the offline guarantee silently lapsed.
    targets = [
        (_iface, "call_ollama", _fake_call_ollama),
        (_iface, "_call_deepseek", _fake_call_deepseek),
    ]
    hosts = [(name, attr, True) for name, attr in _DIRECT_LLM_ALIAS_HOSTS]
    hosts += [(name, attr, False) for name, attr in _OPPORTUNISTIC_LLM_ALIAS_HOSTS]
    for module_name, attr, may_import in hosts:
        for module in _resolve_alias_host(module_name, may_import=may_import):
            if not hasattr(module, attr):
                continue
            replacement = (
                _fake_call_deepseek if attr == "_call_deepseek" else _fake_call_ollama
            )
            targets.append((module, attr, replacement))
    previous = [(module, attr, getattr(module, attr)) for module, attr, _ in targets]
    for module, attr, replacement in targets:
        setattr(module, attr, replacement)
    try:
        yield
    finally:
        for module, attr, value in reversed(previous):
            setattr(module, attr, value)
