from __future__ import annotations

import sqlite3
import sys
import threading
from types import SimpleNamespace

from uk_rent_agent.config import Config
from uk_rent_agent.web import asgi


class _Store:
    def __init__(self, db_path, *, dead: int = 0):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._dead = dead

    def background_job_counts(self):
        return {"pending": 1, "leased": 0, "dead": self._dead}


def _install_runtime(monkeypatch, tmp_path, *, dead: int = 0):
    module = SimpleNamespace(
        AGENT_ARCH="fc_loop",
        APP_CANDIDATE_SHA="a" * 40,
        DEEPSEEK_STRICT=True,
        conversation_store=_Store(tmp_path / "conversations.sqlite3", dead=dead),
        rag_coordinator=None,
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)
    monkeypatch.setenv("APP_IMAGE_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv("PROMPT_VERSION", "2.1.0")
    monkeypatch.setenv("PROMPT_SCHEMA_SHA", "c" * 64)
    monkeypatch.setenv("ROUTING_MODE", "blue_green_shared_conversation_store")
    monkeypatch.setenv("RELEASE_METADATA_REQUIRED", "1")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    # Keep readiness tests hermetic: an operator may have a real SearXNG on
    # localhost, which must not make an offline unit test depend on host state.
    monkeypatch.setenv("SEARXNG_URL", "")
    monkeypatch.setattr(
        asgi,
        "_check_agent_memory",
        lambda: {
            "status": "ok",
            "required": True,
            "backend": "sqlite",
            "records": 0,
        },
    )
    return module


def _config(tmp_path):
    return Config(
        project_root=tmp_path,
        flask_secret_key="test",
        checkpoint_path=tmp_path / "runtime" / "checkpoints.sqlite3",
        enable_checkpointer=False,
    )


def test_ready_keeps_optional_rag_and_searx_degradation_non_blocking(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path)

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["conversation_db"]["status"] == "ok"
    assert body["checks"]["agent_memory"]["status"] == "ok"
    assert body["checks"]["rag"] == {
        "status": "degraded",
        "required": False,
        "detail": "embedding RAG unavailable",
    }
    assert body["checks"]["searxng"]["status"] == "disabled"
    assert body["release"]["source_sha"] == "a" * 40
    assert body["release"]["image_digest"] == "sha256:" + "b" * 64


def test_ready_fails_closed_when_required_release_metadata_is_invalid(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("APP_IMAGE_DIGEST", "unknown")

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 503
    assert body["status"] == "not_ready"
    assert "release_metadata" in body["failed"]
    assert "image_digest" in body["checks"]["release_metadata"]["missing_or_invalid"]


def test_ready_fails_closed_when_declared_prompt_version_differs_from_runtime(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMPT_VERSION", "2.0.0")

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 503
    assert body["status"] == "not_ready"
    problems = body["checks"]["release_metadata"]["missing_or_invalid"]
    assert "prompt.runtime_specs.en.prompt_version" in problems
    assert "prompt.runtime_specs.zh.prompt_version" in problems


def test_release_prompt_version_is_semantic_not_a_source_qualified_identifier(
    monkeypatch, tmp_path
):
    """APP_SOURCE_SHA owns revision identity; PROMPT_VERSION must match PromptSpec."""
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMPT_VERSION", "2.1.0@" + "a" * 7)

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 503
    problems = body["checks"]["release_metadata"]["missing_or_invalid"]
    assert "prompt.runtime_specs.en.prompt_version" in problems
    assert "prompt.runtime_specs.zh.prompt_version" in problems


def test_dead_durable_background_jobs_are_visible_degradation(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path, dead=2)

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 200, body
    assert body["checks"]["background_jobs"]["status"] == "degraded"
    assert body["checks"]["background_jobs"]["counts"]["dead"] == 2
    assert "background_jobs" in body["degraded"]


def test_searx_readiness_uses_healthz_not_search(monkeypatch):
    seen = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _open(url, *, timeout):
        seen.append((url, timeout))
        return _Response()

    monkeypatch.setenv("SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(asgi.urllib.request, "urlopen", _open)

    assert asgi._check_searx()["status"] == "ok"
    assert seen == [("http://searxng:8080/healthz", 1.5)]


def test_agent_memory_failure_is_required_and_fails_readiness(
    monkeypatch, tmp_path
):
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        asgi,
        "_check_agent_memory",
        lambda: {
            "status": "fail",
            "required": True,
            "detail": "injected migration failure",
        },
    )

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 503
    assert "agent_memory" in body["failed"]
