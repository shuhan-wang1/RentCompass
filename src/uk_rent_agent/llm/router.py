from __future__ import annotations

import os
from dataclasses import dataclass


def llm_request_timeout_seconds() -> float:
    """Per-provider-call ceiling; the outer turn deadline remains the final authority."""
    raw = os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if not 0.1 <= value <= 120.0:
        raise ValueError("LLM_REQUEST_TIMEOUT_SECONDS must be between 0.1 and 120")
    return value


def llm_max_retries() -> int:
    """Bound SDK retries so backoff cannot silently consume an unbounded agent turn."""
    raw = os.getenv("LLM_MAX_RETRIES", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM_MAX_RETRIES must be an integer") from exc
    if not 0 <= value <= 3:
        raise ValueError("LLM_MAX_RETRIES must be between 0 and 3")
    return value

# --------------------------------------------------------------------------- #
# Retired provider model names — the SINGLE SOURCE OF TRUTH.                   #
# --------------------------------------------------------------------------- #
# DeepSeek retired these on 2026-07-24. They now return HTTP 400 ("The supported API
# model names are deepseek-v4-pro or deepseek-v4-flash") at REQUEST time, which /health
# cannot see: on 2026-07-24 a stale ``DEEPSEEK_MODEL=deepseek-chat`` in the deployment env
# overrode three correct source defaults and both pools served 400s for a full day without
# a single alarm.
#
# NEVER REMOVE AN ENTRY. A name that was retired once is never valid again; the only cost
# of keeping it is one string comparison per model construction.
#
# ``tests/test_model_name_defaults.py`` IMPORTS this set rather than restating it. A copy
# in the test is a copy that can drift, and a drifted copy is how this class of bug stays
# invisible.
RETIRED_MODEL_NAMES: frozenset[str] = frozenset({"deepseek-chat", "deepseek-reasoner"})

# What to use instead, per retired name — the error message has to be ACTIONABLE, and
# "that name is dead" without a successor just moves the outage to the next guess. The two
# legacy aliases were the non-thinking / thinking modes of ONE model; v4-flash carries both
# via ``extra_body {"thinking": {"type": ...}}`` (see ``create()``), so both map to it.
RETIRED_MODEL_SUCCESSORS: dict[str, str] = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
}

# Env vars that can put a model name in front of the provider. Used only to name the
# offending variable in the error: the guard checks the RESOLVED value, so a name arriving
# by a route not listed here is still refused — it is just reported without a variable.
MODEL_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_MODEL", "DEEPSEEK_CHAT_MODEL", "DEEPSEEK_REASONER_MODEL",
    "DEEPSEEK_PRO_MODEL", "OLLAMA_MODEL",
)


class RetiredModelError(ValueError):
    """A known-dead model name was about to be handed to the provider.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep working,
    while a caller that genuinely wants to special-case this can.
    """


def _normalise_model_name(name: str | None) -> str:
    """Compare the way the provider does, minus transport damage.

    A value that reaches us as ``'"deepseek-chat"'`` (docker-compose list-form env keeps
    the quotes that python-dotenv would have stripped) or with stray whitespace is exactly
    as dead as the bare name; matching only the bare form would let the worst-formatted
    deployment through the guard.
    """
    return (name or "").strip().strip("\"'").strip().lower()


def is_retired_model_name(name: str | None) -> bool:
    """True if ``name`` is a model the provider has retired, however it is formatted.

    The one place that answers this question. The runtime guard below and the
    ``*.env.example`` scan in tests/test_model_name_defaults.py both call it, so the file
    a human copies by hand and the value the process refuses cannot disagree about what
    counts as dead.
    """
    return _normalise_model_name(name) in RETIRED_MODEL_NAMES


def retired_model_env_vars() -> dict[str, str]:
    """Every model env var in THIS process whose value is a retired name.

    Read at failure time rather than remembered at import time, so the message names the
    variable an operator actually has to change.
    """
    return {var: os.environ[var] for var in MODEL_ENV_VARS
            if var in os.environ and is_retired_model_name(os.environ[var])}


def reject_retired_model_names(site: str, **models: str | None) -> None:
    """Refuse a retired model name BEFORE it can reach the provider.

    ``site`` names the code path for the log; each keyword is ``label=resolved_value``.
    Raises :class:`RetiredModelError` naming the env var, the dead value and the
    successor. A no-op when every value is live, so it is safe to call on every
    construction — and it is called on every construction precisely because a guard that
    runs only at startup is a guard that a later ``monkeypatch``/reload walks around.
    """
    dead = {label: value for label, value in models.items()
            if is_retired_model_name(value)}
    if not dead:
        return

    def _successor(value: str | None) -> str:
        return RETIRED_MODEL_SUCCESSORS.get(_normalise_model_name(value), "deepseek-v4-flash")

    lines = [f"{site}: refusing a retired provider model name."]
    for label, value in sorted(dead.items()):
        lines.append(
            f"  {label} = {value!r} was RETIRED by DeepSeek on 2026-07-24 and now returns "
            f"HTTP 400 at request time — a failure /health cannot see. "
            f"Use {_successor(value)!r} instead.")
    env_hits = retired_model_env_vars()
    if env_hits:
        for var, value in sorted(env_hits.items()):
            # Deliberately lists WHERE to look, not which file is currently guilty: a
            # message that names today's offender goes stale the moment it is fixed, and a
            # stale pointer in an outage message costs more than no pointer. The tracked
            # *.env.example files are excluded from the list because a test now keeps them
            # clean (tests/test_model_name_defaults.py); everything below is untracked
            # deployment state that no test can see.
            lines.append(
                f"  Source: environment variable {var}={value!r}. Set {var}="
                f"{_successor(value)} — it will be coming from app/.env, the repo-root "
                f".env, a docker-compose environment:/env_file entry, or the shell that "
                f"launched this process.")
    else:
        lines.append(
            f"  No variable in {', '.join(MODEL_ENV_VARS)} holds that value in this "
            f"process, so it came from a source default or a direct assignment — fix it "
            f"at the assignment.")
    raise RetiredModelError("\n".join(lines))


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
        # PR #13 fixed the source-side DEFAULTS; an explicit env value still sailed
        # straight through to the provider, which is the failure that actually happened.
        # Refuse at CONSTRUCTION rather than at first request: the router is built during
        # app startup (app.py's observer warm-up, and every get_*_llm factory), so a bad
        # env is loud in the boot log instead of surfacing as a 400 on a user's turn.
        reject_retired_model_names(
            "ModelRouter", chat_model=self.chat_model,
            reasoner_model=self.reasoner_model, pro_model=self.pro_model)

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
        # Second check, at the actual client-construction boundary. Not redundant:
        # `route()` is monkeypatched by the eval configs (`model_router_override`), and a
        # subclass or a patched route table can hand back a name __init__ never saw.
        reject_retired_model_names(f"ModelRouter.create({purpose!r})", model=route.model)
        model = ChatOpenAI(
            model=route.model,
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            # base_url override: strict function-calling lives on the /beta endpoint
            # (design §2.9); everything else stays on the standard endpoint.
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            temperature=route.temperature,
            max_tokens=route.max_tokens,
            timeout=llm_request_timeout_seconds(),
            max_retries=llm_max_retries(),
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
