from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_searx_is_loopback_only_and_healthcheck_does_not_search():
    compose = _text("docker-compose.yml")
    assert '"127.0.0.1:8080:8080"' in compose
    assert '"8080:8080"' not in compose
    health = compose.split("searxng:", 1)[1].split("  app:", 1)[0]
    assert "/healthz" in health
    assert "/search?" not in health
    assert compose.count("\n    depends_on:") == 1  # only SearXNG -> Valkey


def test_blue_green_contract_shares_conversations_but_not_checkpoints():
    compose = _text("docker-compose.yml")
    assert compose.count('CONVERSATION_DB_PATH: "/app/.runtime/conversations.sqlite3"') == 2
    assert 'CHECKPOINT_DB_PATH: "/app/.runtime/checkpoints.sqlite3"' in compose
    assert 'CHECKPOINT_DB_PATH: "/app/.runtime/checkpoints_fc.sqlite3"' in compose
    assert compose.count('ROUTING_MODE: "blue_green_shared_conversation_store"') == 2


def test_app_containers_are_non_root_read_only_and_probe_readiness():
    compose = _text("docker-compose.yml")
    assert compose.count('user: "1000:1000"') == 2
    assert compose.count("read_only: true") >= 4
    assert compose.count("cap_drop: [ALL]") >= 4
    assert compose.count("http://localhost:5001/ready") == 2


def test_nginx_discards_client_supplied_forwarding_chains():
    for path in (
        "deploy/nginx/rentcompass.co.uk.conf",
        "deploy/nginx/rentcompass.co.uk.ssl.conf",
    ):
        nginx = _text(path)
        assert "proxy_set_header X-Forwarded-For   $remote_addr;" in nginx
        assert "$proxy_add_x_forwarded_for" not in nginx


def test_local_release_metadata_can_degrade_but_production_enables_strict_gate():
    compose = _text("docker-compose.yml")
    update = _text("deploy/update.sh")
    assert 'RELEASE_METADATA_REQUIRED: "${RELEASE_METADATA_REQUIRED:-0}"' in compose
    assert 'set_env_var RELEASE_METADATA_REQUIRED "1"' in update


def test_image_is_multistage_and_runtime_is_non_root():
    dockerfile = _text("Dockerfile")
    assert " AS builder" in dockerfile
    assert " AS runtime" in dockerfile
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile
    assert "\nUSER app\n" in dockerfile
    assert "APP_PROJECT_ROOT=/app" in dockerfile
    runtime = dockerfile.split(" AS runtime", 1)[1]
    assert "build-essential" not in runtime


def test_ci_required_check_names_and_python_version_match_release_gate():
    workflow = _text(".github/workflows/ci.yml")
    release = _text("deploy/release.sh")
    for name in (
        "Tests (Python 3.12)",
        "Compose smoke",
        "Eval smoke",
        "Supply chain gates",
    ):
        assert f"name: {name}" in workflow
        assert name in release
    assert 'python-version: "3.12"' in workflow
    assert "fetch-depth: 0" in workflow
    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "gitleaks/gitleaks-action@v2" not in workflow
    assert "--randomly-seed=1009" in workflow
    assert "--randomly-seed=2027" in workflow
    assert "/tests/seed-1009/" in workflow
    assert "/tests/seed-2027/" in workflow
    assert "--skip-editable" in workflow
    assert 'RELEASE_TRACK_REF:-origin/main' in release
