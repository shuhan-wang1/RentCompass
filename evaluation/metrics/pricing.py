"""Pricing loader + cost computation for the offline evaluation framework.

Prices live in ``evaluation/model_pricing.yaml`` (never hardcoded here). Cost is
computed from token counts; when a model's prices are ``null`` (unconfirmed) the
cost is returned as ``None`` so callers never quote an invented number.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model_pricing.yaml")


@dataclass(frozen=True)
class ModelPrice:
    input: Optional[float]
    cached_input: Optional[float]
    output: Optional[float]

    @property
    def confirmed(self) -> bool:
        return self.input is not None and self.output is not None


@dataclass(frozen=True)
class Pricing:
    per_tokens: int
    currency: str
    price_source: str
    price_as_of: str
    models: dict  # name -> ModelPrice

    def price_for(self, model: str) -> Optional[ModelPrice]:
        return self.models.get(model)

    def cost(
        self,
        model: str,
        *,
        input_tokens: Optional[int] = 0,
        output_tokens: Optional[int] = 0,
        cached_tokens: Optional[int] = 0,
    ) -> Optional[float]:
        """Return total USD cost, or ``None`` if the model's prices are unconfirmed.

        Cached (cache-hit) input tokens are billed at ``cached_input`` and the
        remaining (cache-miss) input tokens at ``input``.
        """
        price = self.models.get(model)
        if price is None or not price.confirmed:
            return None
        inp = input_tokens or 0
        out = output_tokens or 0
        cached = cached_tokens or 0
        cached = max(0, min(cached, inp))  # cached tokens are a subset of input
        uncached = inp - cached
        cached_rate = price.cached_input if price.cached_input is not None else price.input
        total = (
            uncached * price.input
            + cached * cached_rate
            + out * price.output
        )
        return total / self.per_tokens


def load_pricing(path: Optional[str] = None) -> Pricing:
    """Load pricing config from YAML. Requires PyYAML (already a repo dep)."""
    import yaml

    path = path or _DEFAULT_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    models = {}
    for name, entry in (raw.get("models") or {}).items():
        entry = entry or {}
        models[name] = ModelPrice(
            input=entry.get("input"),
            cached_input=entry.get("cached_input"),
            output=entry.get("output"),
        )
    return Pricing(
        per_tokens=int(raw.get("per_tokens", 1_000_000)),
        currency=str(raw.get("currency", "USD")),
        price_source=str(raw.get("price_source", "")),
        price_as_of=str(raw.get("price_as_of", "")),
        models=models,
    )
