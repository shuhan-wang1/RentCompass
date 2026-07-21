from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRoute:
    model: str
    temperature: float
    max_tokens: int
    reasoning: bool = False


class ModelRouter:
    """Central DeepSeek route table; model aliases change in one place."""

    def __init__(self) -> None:
        # deepseek-chat / deepseek-reasoner were retired 2026-07-24; both map to
        # deepseek-v4-flash, whose non-thinking vs thinking behaviour is selected per
        # request via extra_body {"thinking": {"type": ...}} (see create()). The two
        # aliases stay separate so env overrides can still split them onto different
        # models if needed.
        self.chat_model = os.getenv("DEEPSEEK_CHAT_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
        self.reasoner_model = os.getenv("DEEPSEEK_REASONER_MODEL", "deepseek-v4-flash")
        self.pro_model = os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")

    def route(self, purpose: str, *, complex_task: bool = False, low_latency: bool = False) -> ModelRoute:
        if purpose in {"intent", "classification"}:
            return ModelRoute(self.chat_model, 0.0, 256)
        if purpose in {"memory", "judge"}:
            return ModelRoute(self.chat_model, 0.0, 1500)
        if purpose in {"planner", "critic"}:
            model = self.reasoner_model if complex_task else self.chat_model
            return ModelRoute(model, 0.0, 2000, reasoning=complex_task)
        if purpose in {"responder", "synthesis"}:
            if low_latency:
                return ModelRoute(self.chat_model, 0.1, 4000)
            return ModelRoute(self.reasoner_model, 0.1, 4000, reasoning=True)
        if purpose == "pro":
            return ModelRoute(self.pro_model, 0.0, 8000, reasoning=True)
        return ModelRoute(self.chat_model, 0.1, 4000)

    def create(self, purpose: str, *, base_url: str | None = None, **route_kwargs):
        from langchain_openai import ChatOpenAI

        route = self.route(purpose, **route_kwargs)
        model = ChatOpenAI(
            model=route.model,
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            # base_url override: strict function-calling lives on the /beta endpoint
            # (design §2.9); everything else stays on the standard endpoint.
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            temperature=route.temperature,
            max_tokens=route.max_tokens,
            # v4-flash defaults to thinking ENABLED — every route must pick a mode
            # explicitly or the cheap classification/latency paths silently get
            # chain-of-thought latency and cost.
            extra_body={"thinking": {"type": "enabled" if route.reasoning else "disabled"}},
        )
        # Offline-eval instrumentation (additive; no-op unless RENTCOMPASS_EVAL is
        # active). Records tokens/latency via a callback that never alters output.
        try:
            from evaluation.metrics.collector import instrument_chat_model

            model = instrument_chat_model(
                model, provider="deepseek", model_name=route.model, purpose=purpose
            )
        except Exception:
            pass
        # Canary observation. ALWAYS on, unlike the eval hook above: the canary gate
        # is a production control, and an observer that only ran under
        # RENTCOMPASS_EVAL would observe nothing in the very pool it is gating.
        # This is the single construction point every LLM client in the process
        # passes through — both arches, all call sites — so attaching here cannot be
        # bypassed by the next call site somebody adds. If the import fails the
        # observer is simply absent and turn_observations.snapshot() reports null,
        # which HOLDS the gate; it never degrades to a fabricated zero.
        try:
            from core.turn_observations import install_observer

            # route.model is the CONFIGURED name and is only a fallback: the
            # provider's response metadata wins, because an alias can resolve to a
            # different snapshot server-side and cost is attributed per model.
            model = install_observer(model, configured_model=route.model)
        except Exception:
            pass
        return model
