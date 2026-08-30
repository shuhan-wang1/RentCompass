"""
Centralized LLM Configuration for the LangGraph Agent.

Two providers are supported via the LLM_PROVIDER env var (set in .env):
  - 'deepseek' (default): DeepSeek's OpenAI-compatible API (langchain_openai.ChatOpenAI)
  - 'ollama'            : local Ollama server (langchain_ollama.ChatOllama)

Per-task factories:
  - get_react_llm:          low temperature for deterministic response generation
  - get_classification_llm: higher temperature for diverse tool-selection voting
  - get_planning_llm:       higher temperature for creative search-query planning
"""
import os
from dotenv import load_dotenv

# The retired-name guard lives in the router because the router is the one place every
# model name is SUPPOSED to flow through. This module and llm_interface are the two that
# genuinely do not (see _deepseek_llm's docstring), so they import the guard rather than
# inherit it. Import is unconditional and at module scope on purpose: wrapping it in
# try/except ImportError would turn a hard guarantee into a promise that silently lapses
# the day the package layout changes.
from uk_rent_agent.llm.router import (
    llm_max_retries,
    llm_request_timeout_seconds,
    reject_retired_model_names,
)

# Load .env from app/ regardless of the current working directory.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()

# DeepSeek (OpenAI-compatible) -------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-chat was retired 2026-07-24; deepseek-v4-flash is the successor (thinking
# mode selected per request via extra_body — this module's callers want non-thinking).
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# STARTUP-time refusal. This module is imported transitively by app.py (via
# llm_interface / langgraph_agent), so a stale DEEPSEEK_MODEL in the deployment env now
# kills the boot with an actionable message instead of producing a process that answers
# /health happily and 400s on every real turn. The offline suite runs with no model env
# vars set, so this resolves to the live default and never fires there.
reject_retired_model_names("core.llm_config (import time)", DEEPSEEK_MODEL=DEEPSEEK_MODEL)

# Ollama (local) ---------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "gemma3:27b-cloud")


def _deepseek_llm(temperature: float, max_tokens: int):
    """A DeepSeek chat model for callers outside ModelRouter.

    The observer is attached HERE rather than left to the caller. This helper is the
    second path that bypassed ``ModelRouter`` -- and therefore ``install_observer`` --
    so models it returned made real, billed calls that never reached ``llm_calls``.
    Attaching at construction means a new caller cannot forget: the only way to get an
    unobserved model is to build ``ChatOpenAI`` directly, which
    ``tests/test_all_llm_calls_are_observed.py`` now forbids.
    """
    # Re-checked here, not just at import: DEEPSEEK_MODEL is a module global that a
    # caller or test can rebind after import, and this is the last statement before the
    # name is baked into a client that will bill real money for an HTTP 400.
    reject_retired_model_names("core.llm_config._deepseek_llm", DEEPSEEK_MODEL=DEEPSEEK_MODEL)
    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(
        model=DEEPSEEK_MODEL,
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=llm_request_timeout_seconds(),
        max_retries=llm_max_retries(),
        # v4-flash defaults to thinking ENABLED; this generic helper serves fast
        # chat-style callers, so pin it off explicitly.
        extra_body={"thinking": {"type": "disabled"}},
    )
    try:
        from core.turn_observations import install_observer
        model = install_observer(model, configured_model=DEEPSEEK_MODEL)
    except Exception:
        # Observation is best-effort; observer_installed() stays False and the gate
        # HOLDs rather than reporting a number it cannot back.
        pass
    return model


def _ollama_llm(temperature: float, num_predict: int, num_ctx: int,
                top_p: float = 0.9, top_k=None):
    from langchain_ollama import ChatOllama
    kwargs = dict(
        client_kwargs={"timeout": llm_request_timeout_seconds()},
        async_client_kwargs={"timeout": llm_request_timeout_seconds()},
        model=MODEL_NAME, base_url=OLLAMA_BASE_URL, temperature=temperature,
        top_p=top_p, num_predict=num_predict, num_ctx=num_ctx,
    )
    if top_k is not None:
        kwargs["top_k"] = top_k
    return ChatOllama(**kwargs)


def get_react_llm(low_latency: bool = False):
    """LLM for agent reasoning and response generation (low temperature).

    low_latency=True routes to the non-thinking mode of deepseek-v4-flash — used for
    greetings, direct answers and single-observation syntheses that gain nothing
    from chain-of-thought. The default reserves the (slower, pricier) thinking mode
    for genuine multi-evidence synthesis.
    """
    if LLM_PROVIDER == "deepseek":
        from uk_rent_agent.llm.router import ModelRouter
        return ModelRouter().create("responder", low_latency=low_latency)
    return _ollama_llm(temperature=0.1, num_predict=4000, num_ctx=8192, top_p=0.9)


def get_classification_llm():
    """LLM for tool-selection voting (higher temperature for diversity)."""
    if LLM_PROVIDER == "deepseek":
        from uk_rent_agent.llm.router import ModelRouter
        return ModelRouter().create("intent")
    return _ollama_llm(temperature=0.7, num_predict=50, num_ctx=4096, top_p=0.95, top_k=40)


def get_planning_llm():
    """LLM for search planning (higher temperature for creative query generation)."""
    if LLM_PROVIDER == "deepseek":
        from uk_rent_agent.llm.router import ModelRouter
        return ModelRouter().create("planner")
    return _ollama_llm(temperature=0.8, num_predict=2000, num_ctx=8192, top_p=0.9)
