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
        self.chat_model = os.getenv("DEEPSEEK_CHAT_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))
        self.reasoner_model = os.getenv("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner")
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

    def create(self, purpose: str, **route_kwargs):
        from langchain_openai import ChatOpenAI

        route = self.route(purpose, **route_kwargs)
        model = ChatOpenAI(
            model=route.model,
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            temperature=route.temperature,
            max_tokens=route.max_tokens,
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
        return model
