from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "set_canary_weight.sh"
ROUTE_TEMPLATE = ROOT / "deploy" / "nginx" / "rentcompass-canary-routing.conf"


def _compose_env(service: str) -> dict:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"][service]["environment"]


def _fixture(tmp_path: Path, *, arch: str = "manager_v1", specialists: str = "1", mcp: str = "0"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    route = tmp_path / "route.conf"
    route.write_text(ROUTE_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    legacy_sha = "a" * 40
    candidate_sha = "b" * 40
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"LEGACY_APP_SHA={legacy_sha}",
                f"FC_CANARY_SHA={candidate_sha}",
                f"CANARY_AGENT_ARCH={arch}",
                f"CANARY_MANAGER_V1_SPECIALISTS={specialists}",
                f"CANARY_USE_MCP_TOOLS={mcp}",
                "CANARY_COHORT_SALT=test-stable-v1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    fake_curl = tmp_path / "fakecurl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
url="${@: -1}"
cookie=""
for ((i=1; i<=$#; i++)); do
  if [ "${!i}" = "-H" ]; then j=$((i+1)); case "${!j}" in Cookie:*) cookie="${!j}" ;; esac; fi
done
case "$url" in
  *5001*) arch=legacy; sha="$LEGACY_SHA"; specialists=0; pool=none ;;
  *5002*) arch="$CANDIDATE_ARCH"; sha="$CANDIDATE_SHA"; specialists="$CANDIDATE_SPECIALISTS"; pool=none ;;
  *)
    weight="$(sed -n 's/^# rentcompass-canary-weight: //p' "$ROUTE_CONF" | head -1)"
    if [ "$weight" = 0 ]; then pool=legacy
    elif [ "$weight" = 100 ]; then pool=candidate
    elif [[ "$cookie" == *-1 ]]; then pool=candidate
    else pool=legacy
    fi
    if [ "$pool" = legacy ]; then arch=legacy; sha="$LEGACY_SHA"; specialists=0
    else arch="$CANDIDATE_ARCH"; sha="${PUBLIC_CANDIDATE_SHA:-$CANDIDATE_SHA}"; specialists="$CANDIDATE_SPECIALISTS"
    fi ;;
esac
printf 'HTTP/1.1 200 OK\r\nX-Agent-Arch: %s\r\nX-Agent-Version: %s\r\nX-Agent-Specialists: %s\r\nX-RentCompass-Pool: %s\r\n\r\n' \
  "$arch" "$sha" "$specialists" "$pool"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "CANARY_REPO_DIR": str(ROOT),
        "CANARY_ENV_FILE": str(env_file),
        "CANARY_ROUTE_CONF": str(route),
        "CANARY_TEST_CMD": "true",
        "CANARY_RELOAD_CMD": "true",
        "CANARY_WRITE_CMD": "tee",
        "CANARY_MOVE_CMD": "mv",
        "CANARY_CURL_CMD": str(fake_curl),
        "CANARY_PUBLIC_URL": "http://public/ready",
        "CANARY_LEGACY_URL": "http://127.0.0.1:5001/ready",
        "CANARY_CANDIDATE_URL": "http://127.0.0.1:5002/ready",
        "CANARY_PROBE_COUNT": "4",
        "RENTCOMPASS_DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        "LEGACY_SHA": legacy_sha,
        "CANDIDATE_SHA": candidate_sha,
        "CANDIDATE_ARCH": arch,
        "CANDIDATE_SPECIALISTS": specialists,
        "ROUTE_CONF": str(route),
    }
    return route, env


def _run(env: dict, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_candidate_compose_defaults_off_and_isolates_specialist_checkpoints():
    env = _compose_env("app-fc")
    assert env["AGENT_ARCH"] == "${CANARY_AGENT_ARCH:-fc_loop}"
    assert env["MANAGER_V1_SPECIALISTS"] == "${CANARY_MANAGER_V1_SPECIALISTS:-0}"
    assert env["USE_MCP_TOOLS"] == "${CANARY_USE_MCP_TOOLS:-0}"
    assert "${CANARY_AGENT_ARCH:-fc_loop}" in env["CHECKPOINT_DB_PATH"]
    assert "${CANARY_MANAGER_V1_SPECIALISTS:-0}" in env["CHECKPOINT_DB_PATH"]
    assert env["CONVERSATION_DB_PATH"] == "/app/.runtime/conversations.sqlite3"


def test_nginx_contract_prefers_session_then_conversation_header_and_overwrites_rollout_headers():
    route = ROUTE_TEMPLATE.read_text(encoding="utf-8")
    assert "map $cookie_session $rentcompass_session_cohort_key" in route
    assert "map $http_x_conversation_id $rentcompass_conversation_cohort_key" in route
    assert route.index("$cookie_session") < route.index("$http_x_conversation_id")
    assert "fallback:$binary_remote_addr:$http_user_agent" in route
    assert "rentcompass-canary-weight: 0" in route

    for name in ("rentcompass.co.uk.conf", "rentcompass.co.uk.ssl.conf"):
        nginx = (ROOT / "deploy" / "nginx" / name).read_text(encoding="utf-8")
        assert "proxy_pass http://127.0.0.1:$rentcompass_backend_port" in nginx
        assert "proxy_set_header X-Request-ID      $request_id;" in nginx
        for header in (
            "X-RentCompass-Rollout-ID",
            "X-RentCompass-Rollout-Stage",
            "X-RentCompass-Rollout-Weight",
            "X-RentCompass-Assigned-Pool",
            "X-RentCompass-Traffic-Source",
        ):
            assert f"proxy_set_header {header}" in nginx
        assert "rentcompass-canary.log rentcompass_canary" in nginx


def test_weight_20_generates_stable_split_and_proves_both_pools(tmp_path):
    route, env = _fixture(tmp_path)
    result = _run(env, "--weight", "20", "--rollout-id", "manager-r1")
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = route.read_text(encoding="utf-8")
    assert "# rentcompass-canary-weight: 20" in rendered
    assert "# rentcompass-rollout-id: manager-r1" in rendered
    assert "# rentcompass-rollout-stage: c2" in rendered
    assert 'split_clients "test-stable-v1|${rentcompass_cohort_key}"' in rendered
    assert "20% candidate;" in rendered
    assert "default manager_v1" not in rendered  # identity is verified, never embedded as routing data


def test_nonzero_weight_requires_rollout_id_and_only_documented_weights_are_accepted(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    assert _run(env, "--weight", "5").returncode != 0
    assert _run(env, "--weight", "10", "--rollout-id", "bad-weight").returncode != 0
    assert route.read_bytes() == before


def test_switch_pool_is_a_zero_and_hundred_percent_compatibility_wrapper(tmp_path):
    route, env = _fixture(tmp_path)
    calls = tmp_path / "weight-calls"
    fake = tmp_path / "fake-weight"
    fake.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    switch_env = {
        **env,
        "CALLS": str(calls),
        "SWITCH_ROUTE_CONF": str(route),
        "SWITCH_WEIGHT_SCRIPT": str(fake),
    }
    legacy = subprocess.run(
        ["bash", "deploy/switch_pool.sh", "--to", "legacy"],
        cwd=ROOT, env=switch_env, text=True, capture_output=True, check=False,
    )
    candidate = subprocess.run(
        [
            "bash", "deploy/switch_pool.sh", "--to", "fc",
            "--allow-public-fc", "--rollout-id", "r-flip", "--stage", "flip",
        ],
        cwd=ROOT, env=switch_env, text=True, capture_output=True, check=False,
    )
    assert legacy.returncode == candidate.returncode == 0
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "--weight 0"
    assert lines[1] == (
        "--weight 100 --allow-public-candidate --rollout-id r-flip --stage flip"
    )


def test_compatibility_switch_requires_specialist_identity_header():
    switch = (ROOT / "deploy" / "switch_pool.sh").read_text(encoding="utf-8")
    assert "^x-agent-specialists:" in switch
    assert "specialists mismatch" in switch
    assert 'WANT_SPECIALISTS="${SPECIALISTS[$TARGET]}"' in switch


def test_generated_weighted_route_parses_with_real_nginx_when_available(tmp_path):
    nginx = shutil.which("nginx")
    if nginx is None:
        return
    route, env = _fixture(tmp_path)
    result = _run(env, "--weight", "20", "--rollout-id", "nginx-parse")
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = tmp_path / "nginx"
    prefix.mkdir()
    main = tmp_path / "nginx.conf"
    main.write_text(
        "\n".join(
            (
                f"error_log {tmp_path / 'error.log'};",
                f"pid {tmp_path / 'nginx.pid'};",
                "events { worker_connections 16; }",
                "http {",
                f"  include {route};",
                "  access_log off;",
                "  server { listen 127.0.0.1:18081;",
                "    location / { proxy_pass http://127.0.0.1:$rentcompass_backend_port; }",
                "  }",
                "}",
            )
        ),
        encoding="utf-8",
    )
    checked = subprocess.run(
        [nginx, "-t", "-p", str(prefix), "-c", str(main)],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_nginx_test_failure_restores_previous_route_byte_for_byte(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    test_cmd = tmp_path / "test-nginx"
    test_cmd.write_text(
        "#!/usr/bin/env bash\ngrep -q '^# rentcompass-canary-weight: 5$' \"$ROUTE_CONF\" && exit 1\nexit 0\n",
        encoding="utf-8",
    )
    test_cmd.chmod(0o755)
    env["CANARY_TEST_CMD"] = str(test_cmd)

    result = _run(env, "--weight", "5", "--rollout-id", "manager-r2")
    assert result.returncode != 0
    assert route.read_bytes() == before
    assert "previous route restored" in (result.stdout + result.stderr)


def test_unsafe_candidate_config_fails_before_mutation(tmp_path):
    cases = (("manager_v1", "0", "0"), ("manager_v1", "1", "1"), ("fc_loop", "0", "1"))
    for arch, specialists, mcp in cases:
        route, env = _fixture(
            tmp_path / f"case-{arch}-{specialists}-{mcp}",
            arch=arch,
            specialists=specialists,
            mcp=mcp,
        )
        before = route.read_bytes()
        result = _run(env, "--weight", "5", "--rollout-id", "unsafe")
        assert result.returncode != 0
        assert route.read_bytes() == before


def test_nonzero_exposure_never_allows_an_unidentified_rollback_target(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    result = _run(
        env, "--weight", "5", "--rollout-id", "unsafe-pin",
        "--allow-unidentified-target",
    )
    assert result.returncode != 0
    assert route.read_bytes() == before


def test_post_reload_sha_mismatch_restores_previous_route(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    env["PUBLIC_CANDIDATE_SHA"] = "c" * 40
    result = _run(env, "--weight", "20", "--rollout-id", "wrong-public-sha")
    assert result.returncode != 0
    assert route.read_bytes() == before
    assert "previous route restored" in (result.stdout + result.stderr)


def test_zero_percent_is_emergency_rollback_and_needs_no_candidate(tmp_path):
    route, env = _fixture(tmp_path)
    env["CANARY_CANDIDATE_URL"] = "http://unreachable:5002/ready"
    result = _run(env, "--weight", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# rentcompass-canary-weight: 0" in route.read_text(encoding="utf-8")
    assert "emergency rollback active" in result.stdout


def test_ui_explicitly_promotes_conversation_id_to_header_for_cookie_less_clients():
    ui = (ROOT / "app" / "unified-ui.html").read_text(encoding="utf-8")
    assert "headers['X-Conversation-ID'] = cohortConversationId" in ui
    assert "conversationId: pane.conversationId" in ui
    assert "delete finalOptions.conversationId" in ui
