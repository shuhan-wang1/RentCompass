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
    # The legacy SimpleNamespace in these storage-focused tests intentionally does not import
    # and initialize the full agent. Dedicated tests below cover the three new required checks.
    for check_name in (
        "_check_runtime_configuration", "_check_tool_registry", "_check_agent_graph",
    ):
        monkeypatch.setattr(
            asgi,
            check_name,
            (lambda *_args, **_kwargs: {"status": "ok", "required": True}),
        )
    return module


def _config(tmp_path):
    return Config(
        project_root=tmp_path,
        agent_arch="fc_loop",
        deepseek_strict=True,
        llm_provider="ollama",
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


def test_release_metadata_accepts_experimental_manager_identity(monkeypatch, tmp_path):
    module = _install_runtime(monkeypatch, tmp_path)
    module.AGENT_ARCH = "manager_v1"

    result = asgi._check_release_metadata(asgi._release_manifest())

    assert result["status"] == "ok"
    assert "arch" not in result["missing_or_invalid"]


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

    result = asgi._check_searx()
    assert result["status"] == "degraded"
    assert result["process_health"] == "ok"
    assert result["search_result_capability"] == "unknown"
    assert seen == [("http://searxng:8080/healthz", 1.5)]


def test_required_registry_failure_makes_readiness_503(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        asgi,
        "_check_tool_registry",
        lambda: {"status": "fail", "required": True, "detail": "13 tools"},
    )

    body, code = asgi._readiness(_config(tmp_path))
    assert code == 503
    assert "tool_registry" in body["failed"]



def test_deepseek_credentials_are_required_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("READINESS_REQUIRE_LLM", raising=False)
    config = Config(
        project_root=tmp_path,
        agent_arch="fc_loop",
        llm_provider="deepseek",
        flask_secret_key="test",
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        enable_checkpointer=False,
    )

    result = asgi._check_llm_configuration(config)

    assert result["status"] == "fail"


def test_required_graph_failure_makes_readiness_503(monkeypatch, tmp_path):
    _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        asgi,
        "_check_agent_graph",
        lambda _config: {
            "status": "fail", "required": True, "detail": "factory unavailable",
        },
    )

    body, code = asgi._readiness(_config(tmp_path))

    assert code == 503
    assert "agent_graph" in body["failed"]


def test_external_provider_checks_are_explicitly_unknown_without_network(tmp_path):
    config = _config(tmp_path)
    otm = asgi._check_onthemarket(config)
    tfl = asgi._unprobed_provider("Transport for London", ["calculate_commute"])

    assert otm["status"] == "degraded"
    assert otm["capability"] == "unknown"
    assert "not probed" in otm["detail"]
    assert tfl["capability"] == "unknown"
    assert tfl["required"] is False


def test_real_fourteen_tool_registry_passes_required_readiness(monkeypatch, tmp_path):
    from core.tool_system import create_tool_registry

    monkeypatch.setenv("IDEMPOTENCY_DB", str(tmp_path / "idempotency.sqlite3"))
    module = SimpleNamespace(tool_registry=create_tool_registry())
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)

    result = asgi._check_tool_registry()

    assert result == {
        "status": "ok",
        "required": True,
        "tool_count": 14,
        "strict_schemas": "valid",
        "runtime_constraints": "valid",
    }


def test_graph_factory_validation_is_offline_and_accepts_ollama_injection(
    monkeypatch, tmp_path
):
    class _Provider:
        def list_specs(self):
            return []

    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            return {}

    def _factory(tool_registry, *, agent_llm=None):
        return _Graph()

    module = SimpleNamespace(
        agent_graph=None,
        agent_tool_provider=_Provider(),
        build_agent_graph=_factory,
        create_initial_state=lambda **_kwargs: {},
        _configured_fc_agent_llm=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)

    result = asgi._check_agent_graph(_config(tmp_path))

    assert result["status"] == "ok"
    assert result["state"] == "factory_compiled"
    assert result["arch"] == "fc_loop"
    assert result["provider"] == "ollama"


def test_manager_graph_factory_validation_uses_fc_ollama_injection(monkeypatch, tmp_path):
    class _Provider:
        def list_specs(self):
            return []

    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            return {}

    def _factory(tool_registry, *, agent_llm=None):
        assert agent_llm is probe
        return _Graph()

    probe = object()
    module = SimpleNamespace(
        agent_graph=None,
        agent_tool_provider=_Provider(),
        build_agent_graph=_factory,
        create_initial_state=lambda **_kwargs: {},
        _configured_fc_agent_llm=lambda: probe,
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)
    config = Config(
        project_root=tmp_path,
        agent_arch="manager_v1",
        llm_provider="ollama",
        flask_secret_key="test",
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        enable_checkpointer=False,
    )

    result = asgi._check_agent_graph(config)

    assert result["status"] == "ok"
    assert result["state"] == "factory_compiled"
    assert result["arch"] == "manager_v1"
    assert result["provider"] == "ollama"


def test_enabled_manager_specialists_require_factory_flag_support(monkeypatch, tmp_path):
    class _Provider:
        def list_specs(self):
            return []

    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            return {}

    def _legacy_factory(tool_registry, *, agent_llm=None):
        return _Graph()

    module = SimpleNamespace(
        agent_graph=None,
        agent_tool_provider=_Provider(),
        build_agent_graph=_legacy_factory,
        create_initial_state=lambda **_kwargs: {},
        _configured_fc_agent_llm=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)
    config = Config(
        project_root=tmp_path,
        agent_arch="manager_v1",
        manager_v1_specialists=True,
        llm_provider="ollama",
        flask_secret_key="test",
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        enable_checkpointer=False,
    )

    result = asgi._check_agent_graph(config)

    assert result["status"] == "fail"
    assert "RuntimeError" in result["detail"]



def test_graph_factory_exception_fails_readiness_without_leaking_error(monkeypatch, tmp_path):
    sentinel = "PRIVATE-FACTORY-DETAIL"

    class _Provider:
        def list_specs(self):
            return []

    def _factory(tool_registry, *, agent_llm=None):
        raise RuntimeError(sentinel)

    module = SimpleNamespace(
        agent_graph=None,
        agent_tool_provider=_Provider(),
        build_agent_graph=_factory,
        create_initial_state=lambda **_kwargs: {},
        _configured_fc_agent_llm=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)

    result = asgi._check_agent_graph(_config(tmp_path))

    assert result["status"] == "fail"
    assert result["required"] is True
    assert "RuntimeError" in result["detail"]

def test_runtime_configuration_mismatch_fails_closed(monkeypatch, tmp_path):
    config = _config(tmp_path)
    module = SimpleNamespace(
        _runtime_config=config,
        AGENT_ARCH="legacy",
        DEEPSEEK_STRICT=config.deepseek_strict,
    )
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", module)

    result = asgi._check_runtime_configuration(config)

    assert result["status"] == "fail"
    assert result["required"] is True
    assert "AGENT_ARCH mismatch" in result["detail"]


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


def test_conversation_readiness_rejects_an_existing_nonwritable_database(
    monkeypatch, tmp_path
):
    runtime = _install_runtime(monkeypatch, tmp_path)
    db_path = runtime.conversation_store.db_path
    real_access = asgi.os.access

    def _access(path, mode):
        if str(path) == str(db_path):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(asgi.os, "access", _access)

    result = asgi._check_conversation_db()

    assert result["status"] == "fail"
    assert result["required"] is True
    assert "database is not readable/writable" in result["detail"]


def test_checkpoint_readiness_rejects_an_existing_nonwritable_database(
    monkeypatch, tmp_path
):
    checkpoint = tmp_path / "runtime" / "checkpoints.sqlite3"
    checkpoint.parent.mkdir()
    checkpoint.touch()
    config = Config(
        project_root=tmp_path,
        flask_secret_key="test",
        checkpoint_path=checkpoint,
        enable_checkpointer=True,
    )
    real_access = asgi.os.access

    def _access(path, mode):
        if str(path) == str(checkpoint):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(asgi.os, "access", _access)

    result = asgi._check_checkpoint(config)

    assert result == {
        "status": "fail",
        "required": True,
        "detail": "checkpoint database is not readable/writable",
    }


def test_checkpoint_readiness_refuses_another_architectures_database(tmp_path):
    """F8: `/ready` must fail rather than let manager_v1 resume fc_loop checkpoints.

    Compose gives each pool a differently NAMED file, but a name is a convention:
    the `CHECKPOINT_PATH` fallback, an override, or the shared default all point two
    architectures at one file. Readiness now hands the runtime identity down, so the
    stamped file itself refuses the open."""
    from uk_rent_agent.agent import persistence

    checkpoint = tmp_path / "runtime" / "checkpoints.sqlite3"
    checkpoint.parent.mkdir()
    fc = Config(
        project_root=tmp_path,
        flask_secret_key="test",
        agent_arch="fc_loop",
        checkpoint_path=checkpoint,
        enable_checkpointer=True,
    )
    manager = Config(
        project_root=tmp_path,
        flask_secret_key="test",
        agent_arch="manager_v1",
        manager_v1_specialists=True,
        checkpoint_path=checkpoint,
        enable_checkpointer=True,
    )
    saved = dict(persistence._CHECKPOINTERS)
    persistence._CHECKPOINTERS.clear()
    try:
        first = asgi._check_checkpoint(fc)
        assert first["status"] == "ok"
        assert first["identity"] == {"agent_arch": "fc_loop",
                                     "manager_v1_specialists": "0"}

        second = asgi._check_checkpoint(manager)
    finally:
        persistence._CHECKPOINTERS.clear()
        persistence._CHECKPOINTERS.update(saved)

    assert second["status"] == "fail"
    assert second["required"] is True
    assert second["identity"] == {"agent_arch": "manager_v1",
                                  "manager_v1_specialists": "1"}
    assert "CheckpointIdentityError" in second["detail"]
    assert "different runtime" in second["detail"]
    # The USEFUL half of this message is its tail: the full path and the
    # remediation. A 200-character cap cut the path in half and dropped
    # "Point CHECKPOINT_DB_PATH at this runtime's own file ..." entirely, leaving
    # /ready showing a diagnosis with no fix — while the runbook quoted the whole
    # message, so readers had no idea it was truncated.
    assert str(checkpoint) in second["detail"]
    assert "Point CHECKPOINT_DB_PATH at" in second["detail"]
    assert second["detail"].rstrip().endswith("checkpoints.")
    assert second["path"] == str(checkpoint)
