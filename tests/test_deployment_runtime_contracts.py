from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import subprocess


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


def test_release_repairs_runtime_state_before_source_or_pin_moves():
    release = _text("deploy/release.sh")
    preflight = _text("deploy/preflight_runtime_permissions.sh")

    maintenance = release.index("$RUNTIME_MAINTENANCE_CMD --repair")
    assert maintenance < release.index("Checking out $TARGET_SHORT")
    assert maintenance < release.index('repin "$TARGET"')
    assert "source, pin and containers were not changed" in release
    assert 'RELEASE_RUNTIME_MAINTENANCE_CMD:-bash deploy/preflight_runtime_permissions.sh' in release

    for root in (
        ".runtime",
        "chroma_db",
        "chroma_db_area",
        "app/chroma_db_agent_memory",
        "app/data",
    ):
        assert root in preflight
    assert 'find -P "$root" -xdev' in preflight
    assert "chown --no-dereference" in preflight
    assert "chmod u+rwX" in preflight
    assert "--repair" in preflight


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
    assert (
        "ARG PYTHON_IMAGE=python@sha256:"
        "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
    ) in dockerfile
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
    assert workflow.count("AGENT_MEMORY_DB_PATH:") == 2
    assert workflow.count(
        "python -m pip install --require-hashes -r requirements-bootstrap.lock"
    ) == 3
    assert workflow.count(
        "python -m pip install --require-hashes -r requirements-production.lock"
    ) == 3
    assert workflow.count(
        "python -m pip install --require-hashes -r requirements-ci.lock"
    ) == 2
    assert "--require-hashes -r requirements-supply.lock" in workflow
    assert workflow.count("python -m pip check") == 3
    dockerfile = _text("Dockerfile")
    assert "constraints-production.txt" in dockerfile
    assert "--require-hashes -r requirements-bootstrap.lock" in dockerfile
    assert "--require-hashes -r requirements-production.lock" in dockerfile
    assert "--no-deps --no-build-isolation ." in dockerfile
    assert "python -m pip check" in dockerfile
    assert "pip install --constraint constraints-production.txt" not in workflow
    assert "python scripts/audit_installed_dependencies.py" in workflow
    assert "--skip-editable" not in workflow
    assert '"chromadb"' not in _text("pyproject.toml")
    assert 'RELEASE_TRACK_REF:-origin/main' in release


def test_production_hash_lock_covers_the_exact_reviewed_closure():
    constraints = {
        line.split("==", 1)[0].lower().replace("_", "-")
        for line in _text("constraints-production.txt").splitlines()
        if line and not line.startswith("#")
    }
    lock = _text("requirements-production.lock")
    blocks = {}
    for block in re.split(r"(?m)(?=^[A-Za-z0-9_.-]+==)", lock):
        first = block.splitlines()[0] if block.splitlines() else ""
        if "==" not in first:
            continue
        name = first.split("==", 1)[0].lower().replace("_", "-")
        blocks[name] = block
    locked = set(blocks)
    assert locked == constraints - {"pip", "wheel"}
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in lock
    assert "torch==2.13.0+cpu \\" in lock
    assert all("--hash=sha256:" in block for block in blocks.values())


def test_legacy_memory_retirement_requires_both_pools_on_chromadb_free_pin():
    script = _text("deploy/retire_legacy_agent_memory.sh")
    assert "for pool in legacy fc" in script
    assert 'find_spec("chromadb") is None' in script
    assert 'sha" = "$DEPLOY_PINNED_SHA' in script
    assert "--expected-source-count" in script
    assert "--expected-source-digest" in script
    assert "--confirm-no-legacy-processes" in script
    assert "agent_memory.sqlite3" in script


def test_mutating_deploy_entrypoints_share_a_nonblocking_lock(tmp_path):
    for path in (
        "deploy/release.sh",
        "deploy/update.sh",
        "deploy/switch_pool.sh",
        "deploy/retire_legacy_agent_memory.sh",
    ):
        source = _text(path)
        assert "RENTCOMPASS_DEPLOY_LOCK_HELD" in source
        assert "RENTCOMPASS_DEPLOY_LOCK_FILE" in source
        assert "flock -n 9" in source

    lock_path = tmp_path / "deploy.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["bash", "deploy/release.sh", "--no-fetch", "--dry-run"],
            cwd=ROOT,
            env={
                **os.environ,
                "RELEASE_REPO_DIR": str(ROOT),
                "RENTCOMPASS_DEPLOY_LOCK_FILE": str(lock_path),
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode != 0
    assert "another release/update/switch/retirement operation" in result.stderr
