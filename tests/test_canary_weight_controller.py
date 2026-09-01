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


def _fixture(
    tmp_path: Path,
    *,
    arch: str = "manager_v1",
    specialists: str = "1",
    mcp: str = "0",
    legacy_sends_specialists: bool = True,
):
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
# The DEPLOYED legacy pool predates X-Agent-Specialists: `app` is the standing
# rollback escape hatch and must not be recreated, so it runs an older commit and
# omits the header entirely. LEGACY_SENDS_SPECIALISTS=0 reproduces that pool.
if [ "$arch" = legacy ] && [ "${LEGACY_SENDS_SPECIALISTS:-1}" != 1 ]; then
  printf 'HTTP/1.1 200 OK\r\nX-Agent-Arch: %s\r\nX-Agent-Version: %s\r\nX-RentCompass-Pool: %s\r\n\r\n' \
    "$arch" "$sha" "$pool"
  exit 0
fi
printf 'HTTP/1.1 200 OK\r\nX-Agent-Arch: %s\r\nX-Agent-Version: %s\r\nX-Agent-Specialists: %s\r\nX-RentCompass-Pool: %s\r\n\r\n' \
  "$arch" "$sha" "$specialists" "$pool"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_probe = tmp_path / "fakeprobe"
    fake_probe.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$ANSWER_PROBE_CALLS"\n'
        'echo "stub answer-probe verdict"\n'
        'exit "${ANSWER_PROBE_RC:-0}"\n',
        encoding="utf-8",
    )
    fake_probe.chmod(0o755)
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
        # No test may drive a real turn against a pool, so the answer probe is
        # replaced by a recording stub. Scenarios that need it to fail set
        # ANSWER_PROBE_RC=1; the "policy" tests pass --skip-answer-probe instead.
        "CANARY_ANSWER_PROBE_CMD": str(fake_probe),
        "ANSWER_PROBE_CALLS": str(tmp_path / "probe-calls"),
        # Belt and braces: even if a case defeats the probe injection, these are
        # the addresses it would use. Port 1 is unbound and refuses instantly, so
        # no test can drive a real turn against the live pools on 5001/5002.
        "CANARY_LEGACY_ANSWER_URL": "http://127.0.0.1:1",
        "CANARY_CANDIDATE_ANSWER_URL": "http://127.0.0.1:1",
        "CANARY_ANSWER_PROBE_TIMEOUT": "5",
        "LEGACY_SHA": legacy_sha,
        "CANDIDATE_SHA": candidate_sha,
        "CANDIDATE_ARCH": arch,
        "CANDIDATE_SPECIALISTS": specialists,
        "LEGACY_SENDS_SPECIALISTS": "1" if legacy_sends_specialists else "0",
        "ROUTE_CONF": str(route),
        # The active-drain marker deploy/update.sh owns. Absent unless a case
        # writes it, so `--stage maintenance` is refused by default exactly as it
        # is for a human typing it outside a deploy.
        "RENTCOMPASS_MAINTENANCE_MARKER": str(tmp_path / "maintenance-marker"),
    }
    env.pop("CANARY_ALLOW_FLIP", None)
    env.pop("RENTCOMPASS_DEPLOY_LOCK_HELD", None)
    return route, env


def _open_maintenance_window(env: dict, rollout_id: str) -> dict:
    """What deploy/update.sh does before its drain: hold the lock, mark the window."""
    Path(env["RENTCOMPASS_MAINTENANCE_MARKER"]).write_text(rollout_id, encoding="utf-8")
    return {**env, "RENTCOMPASS_DEPLOY_LOCK_HELD": "1"}


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


# ---------------------------------------------------------------------------
# K4 (C1): 100% is not a routine weight
# ---------------------------------------------------------------------------
# `release.sh --both --drain` -> `update.sh` -> `switch_pool.sh --to fc` bottoms out
# in `--weight 100`, and the controller's `case ... 0|5|20|50|100` accepted it with
# no policy stop. On a host whose root .env selects manager_v1 that made one
# `bash deploy/release.sh` a public cutover.

def _probe_calls(tmp_path: Path) -> str:
    calls = tmp_path / "probe-calls"
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_weight_100_is_refused_at_the_default_flip_stage(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    result = _run(env, "--weight", "100", "--rollout-id", "sneaky-flip",
                  "--skip-answer-probe")
    assert result.returncode != 0
    assert route.read_bytes() == before
    output = result.stdout + result.stderr
    assert "CANARY_ALLOW_FLIP=1" in output
    assert "50% is the highest authorised rollout stage" in output
    assert "docs/canary_runbook.md" in output


def test_weight_100_is_refused_at_any_other_stage(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    for stage in ("c3", "internal", "rollback"):
        result = _run(env, "--weight", "100", "--rollout-id", f"r-{stage}",
                      "--stage", stage, "--skip-answer-probe")
        assert result.returncode != 0, stage
        assert route.read_bytes() == before
        assert "docs/canary_runbook.md" in (result.stdout + result.stderr)


def test_weight_100_is_accepted_only_with_an_explicit_allow_flip(tmp_path):
    route, env = _fixture(tmp_path)
    env["CANARY_ALLOW_FLIP"] = "1"
    result = _run(env, "--weight", "100", "--rollout-id", "gated-flip",
                  "--stage", "flip", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = route.read_text(encoding="utf-8")
    assert "# rentcompass-canary-weight: 100" in rendered
    assert "# rentcompass-rollout-stage: flip" in rendered
    assert "default candidate;" in rendered


def test_maintenance_stage_allows_the_deploy_drain_without_allow_flip(tmp_path):
    """update.sh's drain is the one non-flip caller of 100: temporary, and restored."""
    route, env = _fixture(tmp_path)
    assert env.get("CANARY_ALLOW_FLIP") is None
    drain = _open_maintenance_window(env, "deploy-maintenance-abc1234")
    result = _run(drain, "--weight", "100", "--rollout-id", "deploy-maintenance-abc1234",
                  "--stage", "maintenance", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = route.read_text(encoding="utf-8")
    assert "# rentcompass-canary-weight: 100" in rendered
    assert "# rentcompass-rollout-stage: maintenance" in rendered
    assert "temporary drain" in result.stdout


def test_maintenance_stage_is_not_a_permanent_flip_a_human_can_type(tmp_path):
    """`--stage maintenance` was the one path to 100% that never met CANARY_ALLOW_FLIP,
    with no TTL and no marker: a self-chosen rollout id parked the candidate on all
    public traffic permanently. It is now an ACTIVE-DRAIN window — the deploy lock,
    update.sh's machine rollout-id shape, and a marker naming this exact drain."""
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    marker = Path(env["RENTCOMPASS_MAINTENANCE_MARKER"])

    # 1. A bare human invocation: no deploy lock, no marker, any id it likes.
    result = _run(env, "--weight", "100", "--rollout-id", "perma-cutover-2026",
                  "--stage", "maintenance", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode != 0
    assert "machine-only" in (result.stdout + result.stderr)
    assert route.read_bytes() == before

    # 2. The lock alone is not enough: the id must be the drain's own shape, so the
    #    drain's turns are always filterable out of a real stage window.
    locked = {**env, "RENTCOMPASS_DEPLOY_LOCK_HELD": "1"}
    result = _run(locked, "--weight", "100", "--rollout-id", "perma-cutover-2026",
                  "--stage", "maintenance", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode != 0
    assert "deploy-maintenance-" in (result.stdout + result.stderr)
    assert route.read_bytes() == before

    # 3. Right shape, but no deploy is actually draining: no marker on disk.
    assert not marker.exists()
    result = _run(locked, "--weight", "100", "--rollout-id", "deploy-maintenance-abc1234",
                  "--stage", "maintenance", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode != 0
    assert "drain marker" in (result.stdout + result.stderr)
    assert route.read_bytes() == before

    # 4. A marker from a DIFFERENT drain cannot authorise this one.
    marker.write_text("deploy-maintenance-9999999", encoding="utf-8")
    result = _run(locked, "--weight", "100", "--rollout-id", "deploy-maintenance-abc1234",
                  "--stage", "maintenance", "--allow-public-candidate",
                  "--skip-answer-probe")
    assert result.returncode != 0
    assert "different rollout id" in (result.stdout + result.stderr)
    assert route.read_bytes() == before


def test_documented_rollout_weights_are_unaffected_by_the_flip_policy(tmp_path):
    for weight, stage in (("5", "c1"), ("20", "c2"), ("50", "c3")):
        route, env = _fixture(tmp_path / f"stage-{stage}")
        result = _run(env, "--weight", weight, "--rollout-id", f"r-{stage}",
                      "--skip-answer-probe")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"# rentcompass-rollout-stage: {stage}" in route.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# K4 (C2): readiness proves identity; only a turn proves the pool can answer
# ---------------------------------------------------------------------------

def test_every_nonzero_exposure_probes_both_pools_for_a_real_answer(tmp_path):
    route, env = _fixture(tmp_path)
    result = _run(env, "--weight", "20", "--rollout-id", "answer-probe-on")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _probe_calls(tmp_path).splitlines()
    assert len(calls) == 2, calls
    assert any("--expect-arch legacy" in line for line in calls)
    assert any("--expect-arch manager_v1" in line and "--expect-specialists 1" in line
               for line in calls)
    assert "answered a real turn" in result.stdout


def test_a_pool_that_cannot_answer_blocks_exposure_and_changes_no_routing(tmp_path):
    route, env = _fixture(tmp_path)
    before = route.read_bytes()
    env["ANSWER_PROBE_RC"] = "1"
    result = _run(env, "--weight", "5", "--rollout-id", "dead-model")
    assert result.returncode != 0
    assert route.read_bytes() == before
    assert "cannot answer a real turn" in (result.stdout + result.stderr)


def test_the_answer_probe_can_be_skipped_but_says_so(tmp_path):
    route, env = _fixture(tmp_path)
    env["ANSWER_PROBE_RC"] = "1"          # would fail if it ran at all
    result = _run(env, "--weight", "5", "--rollout-id", "skip", "--skip-answer-probe")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _probe_calls(tmp_path) == ""
    assert "was NOT proven able to answer" in result.stdout


def test_emergency_rollback_never_waits_on_an_answer_probe(tmp_path):
    route, env = _fixture(tmp_path)
    env["ANSWER_PROBE_RC"] = "1"
    result = _run(env, "--weight", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _probe_calls(tmp_path) == ""


def test_lowering_the_weight_is_always_possible_even_with_a_broken_candidate(tmp_path):
    """The probe's only test used to be `WEIGHT != 0`, so 50 -> 5 ran it — against
    a candidate that is broken, which is the entire reason for the de-escalation.
    It failed, routing stayed at 50%, and weight 0 was the only reachable move."""
    route, env = _fixture(tmp_path)
    ok = _run(env, "--weight", "50", "--rollout-id", "r-c3")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "# rentcompass-canary-weight: 50" in route.read_text(encoding="utf-8")

    env["ANSWER_PROBE_RC"] = "1"          # the candidate can no longer answer
    calls_before = len(_probe_calls(tmp_path).splitlines())
    down = _run(env, "--weight", "5", "--rollout-id", "r-deescalate")
    assert down.returncode == 0, down.stdout + down.stderr
    assert "# rentcompass-canary-weight: 5" in route.read_text(encoding="utf-8")
    assert "the answer probe is SKIPPED" in down.stdout
    assert len(_probe_calls(tmp_path).splitlines()) == calls_before  # nothing probed

    # ...but RAISING exposure from the same broken candidate is still refused.
    up = _run(env, "--weight", "20", "--rollout-id", "r-reescalate")
    assert up.returncode != 0
    assert "# rentcompass-canary-weight: 5" in route.read_text(encoding="utf-8")
    assert "cannot answer a real turn" in (up.stdout + up.stderr)


def test_an_injected_answer_probe_can_never_pass_for_the_real_one(tmp_path):
    """`CANARY_ANSWER_PROBE_CMD=true` exits 0 in silence, so the gate could be
    turned off with no trace at all — unlike --skip-answer-probe, which shouts."""
    route, env = _fixture(tmp_path)
    result = _run(env, "--weight", "5", "--rollout-id", "injected")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the answer probe is INJECTED" in result.stdout
    assert env["CANARY_ANSWER_PROBE_CMD"] in result.stdout
    assert "NOT deploy/probe_pool_answer.py" in result.stdout

    empty = _run({**env, "CANARY_ANSWER_PROBE_CMD": ""},
                 "--weight", "5", "--rollout-id", "empty-probe")
    assert empty.returncode != 0
    assert "set but empty" in (empty.stdout + empty.stderr)


def test_the_deployed_legacy_pool_sends_no_specialist_header_and_is_still_accepted(tmp_path):
    """`X-Agent-Specialists` does not exist on the commit the legacy pool runs (it
    is the standing rollback escape hatch and must not be recreated). verify_local
    has always exempted it; the probe must apply the identical rule, or every
    weight > 0 is refused on the pool the exemption exists for."""
    route, env = _fixture(tmp_path, legacy_sends_specialists=False)
    result = _run(env, "--weight", "20", "--rollout-id", "legacy-no-header")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# rentcompass-canary-weight: 20" in route.read_text(encoding="utf-8")
    # ...and the probe was asked for exactly the expectation verify_local uses.
    calls = _probe_calls(tmp_path).splitlines()
    assert any("--expect-arch legacy" in line and "--expect-specialists 0" in line
               for line in calls), calls


# ---------------------------------------------------------------------------
# C4: switch_pool.sh reads the SAME candidate identity as everything else
# ---------------------------------------------------------------------------

def test_switch_pool_reads_candidate_identity_from_the_root_env(tmp_path):
    """A manager_v1 host used to fail `switch_pool.sh --to fc` on an arch mismatch:
    the script read undocumented SWITCH_CANDIDATE_* while every other component read
    CANARY_AGENT_ARCH / CANARY_MANAGER_V1_SPECIALISTS from the root .env."""
    route, env = _fixture(tmp_path)          # root .env selects manager_v1/1
    calls = tmp_path / "weight-calls"
    fake = tmp_path / "fake-weight"
    fake.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    switch_env = {
        **env,
        "CALLS": str(calls),
        "SWITCH_ROUTE_CONF": str(route),
        "SWITCH_WEIGHT_SCRIPT": str(fake),
        "SWITCH_ENV_FILE": env["CANARY_ENV_FILE"],
    }
    switch_env.pop("SWITCH_CANDIDATE_ARCH", None)
    switch_env.pop("SWITCH_CANDIDATE_SPECIALISTS", None)
    result = subprocess.run(
        ["bash", "deploy/switch_pool.sh", "--to", "fc", "--allow-public-fc",
         "--rollout-id", "r-maint", "--stage", "maintenance"],
        cwd=ROOT, env=switch_env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--weight 100 --allow-public-candidate --rollout-id r-maint --stage maintenance"
    ]

    # The documented override still wins, and a mismatched pair still fails closed.
    bad = subprocess.run(
        ["bash", "deploy/switch_pool.sh", "--to", "fc", "--allow-public-fc"],
        cwd=ROOT,
        env={**switch_env, "SWITCH_CANDIDATE_ARCH": "manager_v1",
             "SWITCH_CANDIDATE_SPECIALISTS": "0"},
        text=True, capture_output=True, check=False,
    )
    assert bad.returncode != 0
    assert "candidate identity must be" in (bad.stdout + bad.stderr)


def test_switch_pool_normalises_boolean_spellings_of_the_specialist_switch(tmp_path):
    route, env = _fixture(tmp_path, arch="manager_v1", specialists="true")
    calls = tmp_path / "weight-calls"
    fake = tmp_path / "fake-weight"
    fake.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "deploy/switch_pool.sh", "--to", "legacy"],
        cwd=ROOT,
        env={**env, "CALLS": str(calls), "SWITCH_ROUTE_CONF": str(route),
             "SWITCH_WEIGHT_SCRIPT": str(fake),
             "SWITCH_ENV_FILE": env["CANARY_ENV_FILE"]},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
