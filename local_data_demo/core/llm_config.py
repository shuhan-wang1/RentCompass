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

# Load .env from local_data_demo/ regardless of the current working directory.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()

# DeepSeek (OpenAI-compatible) -------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Ollama (local) ---------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "gemma3:27b-cloud")


def _deepseek_llm(temperature: float, max_tokens: int):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=DEEPSEEK_MODEL,
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _ollama_llm(temperature: float, num_predict: int, num_ctx: int,
                top_p: float = 0.9, top_k=None):
    from langchain_ollama import ChatOllama
    kwargs = dict(
        model=MODEL_NAME, base_url=OLLAMA_BASE_URL, temperature=temperature,
        top_p=top_p, num_predict=num_predict, num_ctx=num_ctx,
    )
    if top_k is not None:
        kwargs["top_k"] = top_k
    return ChatOllama(**kwargs)


def get_react_llm():
    """LLM for agent reasoning and response generation (low temperature)."""
    if LLM_PROVIDER == "deepseek":
        return _deepseek_llm(temperature=0.1, max_tokens=4000)
    return _ollama_llm(temperature=0.1, num_predict=4000, num_ctx=8192, top_p=0.9)


def get_classification_llm():
    """LLM for tool-selection voting (higher temperature for diversity)."""
    if LLM_PROVIDER == "deepseek":
        return _deepseek_llm(temperature=0.7, max_tokens=50)
    return _ollama_llm(temperature=0.7, num_predict=50, num_ctx=4096, top_p=0.95, top_k=40)


def get_planning_llm():
    """LLM for search planning (higher temperature for creative query generation)."""
    if LLM_PROVIDER == "deepseek":
        return _deepseek_llm(temperature=0.8, max_tokens=2000)
    return _ollama_llm(temperature=0.8, num_predict=2000, num_ctx=8192, top_p=0.9)
