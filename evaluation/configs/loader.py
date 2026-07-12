"""Config loader for the RentCompass eval runner.

A config is DATA (YAML): it declares which model-routing override and which
retrieval-concurrency mode a run uses. This module turns that data into concrete
patches/env the runner applies — no business logic lives in the YAML.

Public interface (Phase-2 may import):
* :class:`EvalConfig`      — parsed config (name, description, router mode, concurrency, env).
* :func:`load_config`      — path -> EvalConfig (accepts a config name or a file path).
* :func:`apply_config`     — context manager that applies the router-override patch
  and env for the duration of a run. Retrieval concurrency is surfaced as
  ``EvalConfig.max_concurrency`` for the runner to inject into ``graph.ainvoke``.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional

_CONFIG_DIR = Path(__file__).resolve().parent


@dataclass
class EvalConfig:
    name: str
    description: str = ""
    router_mode: str = "none"          # none | all_strong
    router_purposes: Dict[str, str] = field(default_factory=dict)
    concurrency: str = "parallel"      # parallel | serial
    env: Dict[str, str] = field(default_factory=dict)
    source_path: Optional[str] = None

    @property
    def max_concurrency(self) -> Optional[int]:
        """LangGraph max_concurrency to inject on ainvoke (None = unbounded)."""
        return 1 if self.concurrency == "serial" else None


def load_config(path_or_name: str) -> EvalConfig:
    """Load a config by file path OR by bare name (resolved under configs/)."""
    import yaml

    p = Path(path_or_name)
    if not p.exists():
        cand = _CONFIG_DIR / path_or_name
        if not cand.suffix:
            cand = cand.with_suffix(".yaml")
        p = cand
    if not p.exists():
        raise FileNotFoundError(f"eval config not found: {path_or_name}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    override = raw.get("model_router_override") or {}
    retrieval = raw.get("retrieval") or {}
    return EvalConfig(
        name=str(raw.get("name", p.stem)),
        description=str(raw.get("description", "")).strip(),
        router_mode=str(override.get("mode", "none")),
        router_purposes=dict(override.get("purposes") or {}),
        concurrency=str(retrieval.get("concurrency", "parallel")),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        source_path=str(p),
    )


@contextlib.contextmanager
def _patch_router_all_strong() -> Iterator[None]:
    """Force ModelRouter.route to return the reasoner tier for EVERY purpose.

    Preserves each purpose's temperature/max_tokens (so only the MODEL changes),
    and marks reasoning=True. This is the Phase-4 Baseline A ("all-strong") seam.
    """
    from uk_rent_agent.llm import router as _router

    original = _router.ModelRouter.route

    def _all_strong(self, purpose, *, complex_task=False, low_latency=False):
        base = original(self, purpose, complex_task=complex_task, low_latency=low_latency)
        return _router.ModelRoute(
            model=self.reasoner_model,
            temperature=base.temperature,
            max_tokens=base.max_tokens,
            reasoning=True,
        )

    _router.ModelRouter.route = _all_strong
    try:
        yield
    finally:
        _router.ModelRouter.route = original


@contextlib.contextmanager
def apply_config(cfg: EvalConfig) -> Iterator[None]:
    """Apply a config's env + router override for the duration of the block.

    Retrieval concurrency is NOT patched here — it is surfaced via
    ``cfg.max_concurrency`` and injected by the runner into the LangGraph config,
    which is a pure scheduling change (see serial_retrieval.yaml).
    """
    saved_env: Dict[str, Optional[str]] = {}
    for k, v in cfg.env.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v

    stack = contextlib.ExitStack()
    try:
        if cfg.router_mode == "all_strong":
            stack.enter_context(_patch_router_all_strong())
        elif cfg.router_mode not in ("none", ""):
            raise ValueError(f"unknown model_router_override.mode: {cfg.router_mode!r}")
        yield
    finally:
        stack.close()
        for k, old in saved_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
