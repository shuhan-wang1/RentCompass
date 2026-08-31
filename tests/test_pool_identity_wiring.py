"""The legacy `app` pool must be ABLE to state its commit — without ever being able to
refuse to start because it cannot.

`app` is the only rollback target. Two independent properties have to hold at once, and
they pull in opposite directions:

  1. WIRED    — compose must hand `app` an APP_CANDIDATE_SHA, the same knob app-fc gets
                via FC_CANARY_SHA, so X-Agent-Version can carry a real sha and
                `deploy/switch_pool.sh --to legacy` stops needing
                --allow-unidentified-target.
  2. FAIL-SAFE — that wiring must DEFAULT, never require. FC_CANARY_IMAGE/FC_CANARY_SHA
                use `:?`, so a missing value makes every `docker compose` command fail.
                Reproducing that on the rollback target would break the exact path taken
                in an emergency, so `app` uses `:-` with an EMPTY default, which the app
                reads as indistinguishable from unset (today's `unknown`).

These are source/contract guards on purpose: the failure mode being prevented is a value
that is configured, plumbed, and then never actually reaches X-Agent-Version. Each test
below links two files, so a rename or an operator change on either side fails the suite
rather than silently unplugging pool identity.
"""

from __future__ import annotations

import os
import re
import sys

import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMPOSE = os.path.join(_ROOT, "docker-compose.yml")
_ENV_EXAMPLE = os.path.join(_ROOT, ".env.example")
_APP_PY = os.path.join(_ROOT, "app", "app.py")
_ASGI_PY = os.path.join(_ROOT, "src", "uk_rent_agent", "web", "asgi.py")
_SWITCH_POOL = os.path.join(_ROOT, "deploy", "switch_pool.sh")
_MONITOR = os.path.join(_ROOT, "deploy", "monitoring", "rentcompass-monitor.sh")

# The one header ops provenance hangs off. Compared case-insensitively because the app
# sets it title-cased and the shell probes grep it lower-cased.
_IDENTITY_HEADER = "x-agent-version"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _compose() -> dict:
    return yaml.safe_load(_read(_COMPOSE))


def _service_env(service: str) -> dict:
    env = _compose()["services"][service]["environment"]
    assert isinstance(env, dict), f"{service}.environment is not a mapping"
    return env


# ---------------------------------------------------------------------------
# 1. WIRED — the rollback target is handed an identity variable at all
# ---------------------------------------------------------------------------

def test_legacy_app_service_declares_app_candidate_sha():
    """FAILS ON OLD BEHAVIOUR: before this change only app-fc got APP_CANDIDATE_SHA, so
    the legacy pool answered `x-agent-version: unknown` and rollback had to defeat
    switch_pool.sh's provenance check."""
    assert "APP_CANDIDATE_SHA" in _service_env("app"), (
        "the `app` service (legacy pool = the ONLY rollback target) sets no "
        "APP_CANDIDATE_SHA, so it can never state which commit it runs"
    )


def test_legacy_identity_uses_the_same_container_env_name_as_fc():
    """Both pools must populate the SAME variable the app reads — one code path, not two."""
    assert "APP_CANDIDATE_SHA" in _service_env("app-fc")
    assert 'os.getenv("APP_CANDIDATE_SHA")' in _read(_APP_PY), (
        "app.py no longer reads APP_CANDIDATE_SHA; the compose wiring in both pools is dead"
    )


# ---------------------------------------------------------------------------
# 2. FAIL-SAFE — defaulting substitution, never a required one, on `app`
# ---------------------------------------------------------------------------

def test_legacy_identity_var_defaults_instead_of_hard_failing():
    """`:?` on the rollback target would make a MISSING sha block the rollback itself."""
    raw = _service_env("app")["APP_CANDIDATE_SHA"]
    m = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)(:?[-?])(.*)\}", raw)
    assert m, f"expected a single ${{VAR:-default}} substitution, got {raw!r}"
    var, operator, default = m.group(1), m.group(2), m.group(3)
    assert operator in ("-", ":-"), (
        f"`app` uses the REQUIRED operator {operator!r} for {var} — a missing value would "
        "make every `docker compose` command fail, including the one that restarts the "
        "escape hatch. Use `:-` with an empty default."
    )
    assert default == "", (
        f"default for {var} is {default!r}; it must be empty so the app's own "
        "unset-handling (falling back to the git probe / 'unknown') stays in charge"
    )


def test_no_required_substitution_anywhere_in_the_rollback_target_service():
    """Broader form of the above: nothing in the `app` service may be `:?`-required, or a
    missing root-.env value takes the escape hatch down with it."""
    app_service = yaml.safe_dump(_compose()["services"]["app"])
    offenders = re.findall(r"\$\{([A-Z_][A-Z0-9_]*):?\?", app_service)
    assert offenders == [], (
        f"`app` requires {offenders} via `:?` — the rollback target must be startable "
        "with an empty root .env"
    )


def test_fc_pool_identity_stays_required():
    """The inverse guard: fc is the FORWARD target, and a forward switch onto a pool that
    cannot name its commit must stay impossible. Do not "harmonise" this to `:-`."""
    raw = _service_env("app-fc")["APP_CANDIDATE_SHA"]
    assert re.match(r"\$\{FC_CANARY_SHA:?\?", raw), (
        f"app-fc's APP_CANDIDATE_SHA is {raw!r}; it must stay `:?`-required so the fc pool "
        "cannot run unpinned"
    )


# ---------------------------------------------------------------------------
# 3. FAIL-SAFE, demonstrated: the empty default behaves exactly like unset
# ---------------------------------------------------------------------------

def _app_py_resolution_expression() -> str:
    """The RHS of app.py's module-level `APP_CANDIDATE_SHA = …`, read from source rather
    than retyped, so this test exercises the REAL resolution rule."""
    m = re.search(r"^APP_CANDIDATE_SHA\s*=\s*(.+)$", _read(_APP_PY), re.MULTILINE)
    assert m, "could not find the module-level APP_CANDIDATE_SHA assignment in app/app.py"
    return m.group(1).strip()


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_empty_compose_default_is_indistinguishable_from_unset(env_value, monkeypatch):
    """Compose's `${LEGACY_APP_SHA:-}` puts an EMPTY string in the container when the
    operator has not set it. Empty must resolve to the same fallback as unset — otherwise
    adding the wiring would make the CURRENT (unset) state worse by stamping "" as the
    pool's identity."""
    if env_value is None:
        monkeypatch.delenv("APP_CANDIDATE_SHA", raising=False)
    else:
        monkeypatch.setenv("APP_CANDIDATE_SHA", env_value)
    resolved = eval(  # noqa: S307 — expression comes from this repo's own source
        _app_py_resolution_expression(),
        {"os": os, "_startup_git_sha": lambda: "FALLBACK"},
    )
    assert resolved == "FALLBACK", (
        f"APP_CANDIDATE_SHA={env_value!r} resolved to {resolved!r} instead of the "
        "unset fallback — an empty compose default would ship as the pool's identity"
    )


def test_a_real_sha_survives_resolution_verbatim(monkeypatch):
    """And once ops does set it, it must reach the constant untouched (no truncation)."""
    sha = "c9e60c2d1ba3fadf41c731f094abdc94ba712bfd"
    monkeypatch.setenv("APP_CANDIDATE_SHA", sha)
    resolved = eval(  # noqa: S307
        _app_py_resolution_expression(),
        {"os": os, "_startup_git_sha": lambda: "FALLBACK"},
    )
    assert resolved == sha and len(resolved) == 40


# ---------------------------------------------------------------------------
# 4. The value must actually REACH the header, on every route ops probes
# ---------------------------------------------------------------------------

def test_identity_header_is_stamped_on_all_flask_routes():
    """One after_request hook covers every Flask response (chat, turn and CRUD alike)."""
    sys.path[:0] = [p for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app"))
                    if p not in sys.path]
    os.environ.setdefault("USE_MCP_TOOLS", "0")
    os.environ.setdefault("PROPERTY_SOURCE", "csv")
    import app as appmod

    hooks = [f.__name__ for f in appmod.app.after_request_funcs.get(None, [])]
    assert "_canary_headers" in hooks, (
        "the app-wide after_request hook that stamps X-Agent-* is gone; identity would "
        "cover only whichever endpoints remember to set it"
    )
    src = _read(_APP_PY)
    assert 'response.headers["X-Agent-Version"] = APP_CANDIDATE_SHA' in src


def test_identity_header_is_stamped_on_the_starlette_health_route():
    """/health is served by Starlette, NOT Flask, so it bypasses after_request. It is also
    the ONLY endpoint switch_pool.sh and the monitor probe, so this is the path that
    matters most. Regression guard for the d62628c fix."""
    import types
    from uk_rent_agent.web import asgi

    sha = "c9e60c2d1ba3fadf41c731f094abdc94ba712bfd"
    fake = types.ModuleType("uk_rent_agent._legacy_web_app")
    fake.AGENT_ARCH = "legacy"
    fake.APP_CANDIDATE_SHA = sha
    fake.MANAGER_V1_SPECIALISTS = False
    sys.modules["uk_rent_agent._legacy_web_app"] = fake
    try:
        headers = asgi._canary_identity()
    finally:
        sys.modules.pop("uk_rent_agent._legacy_web_app", None)
    assert headers.get("X-Agent-Version") == sha
    assert headers.get("X-Agent-Arch") == "legacy"
    assert headers.get("X-Agent-Specialists") == "0"
    # ...and the length switch_pool.sh's provenance check demands.
    assert len(headers["X-Agent-Version"]) == 40


def test_ops_probes_grep_the_header_the_app_actually_sets():
    """Cross-file anti-drift guard. A rename on either side silently reverts the pool to
    'unidentified' with nothing failing, which is this codebase's recurring defect shape."""
    for path in (_APP_PY, _ASGI_PY):
        assert _IDENTITY_HEADER in _read(path).lower(), f"{path} stops setting the header"
    for path in (_SWITCH_POOL, _MONITOR):
        assert _IDENTITY_HEADER in _read(path).lower(), f"{path} stops reading the header"


# ---------------------------------------------------------------------------
# 5. The documented value must satisfy the gate it exists to satisfy
# ---------------------------------------------------------------------------

def test_switch_pool_still_requires_a_full_40_char_sha():
    """Pins the assumption the next two assertions depend on."""
    assert re.search(r"\$\{#(?:t_)?sha\}\s*-ne\s*40", _read(_SWITCH_POOL)), (
        "switch_pool.sh no longer length-checks the sha at 40; re-check what provenance "
        "now means before relaxing the docs"
    )


def test_env_example_documents_the_legacy_var_as_a_full_sha():
    """A 7-char short sha would set the header and STILL fail the 40-char check, i.e. look
    fixed while rollback still needs --allow-unidentified-target."""
    var = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*):?-.*\}",
                       _service_env("app")["APP_CANDIDATE_SHA"]).group(1)
    example = _read(_ENV_EXAMPLE)
    assert var in example, f"{var} is not documented in .env.example"
    block = example[example.index(var):]
    assert "40" in block[:1200], (
        f"{var} is documented without saying it must be the FULL 40-char sha"
    )


def test_env_example_does_not_make_the_legacy_var_look_mandatory():
    """The whole point is that ops can leave it out. Keep it commented out, like FC_*."""
    mentions = [ln for ln in _read(_ENV_EXAMPLE).splitlines() if "LEGACY_APP_SHA" in ln]
    assert mentions, "LEGACY_APP_SHA vanished from .env.example"
    for line in mentions:
        assert line.lstrip().startswith("#"), (
            f"uncommented {line.strip()!r} in .env.example implies it is required"
        )


# ---------------------------------------------------------------------------
# 6. ONE source of candidate identity across the whole deploy path
# ---------------------------------------------------------------------------

_UPDATE = os.path.join(_ROOT, "deploy", "update.sh")
_SET_WEIGHT = os.path.join(_ROOT, "deploy", "set_canary_weight.sh")


def test_every_deploy_component_reads_the_same_candidate_identity_variables():
    """switch_pool.sh used to read undocumented SWITCH_CANDIDATE_ARCH /
    SWITCH_CANDIDATE_SPECIALISTS, which nothing else in the repo sets, while
    update.sh, set_canary_weight.sh and the monitor all read CANARY_AGENT_ARCH /
    CANARY_MANAGER_V1_SPECIALISTS from the root .env. On a manager_v1 host that
    disagreement made `switch_pool.sh --to fc` fail on an arch mismatch against a
    pool that was correct."""
    for path in (_SWITCH_POOL, _UPDATE, _SET_WEIGHT, _MONITOR):
        source = _read(path)
        assert "CANARY_AGENT_ARCH" in source, f"{path} stops reading CANARY_AGENT_ARCH"
        assert "CANARY_MANAGER_V1_SPECIALISTS" in source, (
            f"{path} stops reading CANARY_MANAGER_V1_SPECIALISTS"
        )


def test_switch_pool_keeps_the_legacy_override_explicit_and_documented():
    """SWITCH_CANDIDATE_* survives as an override for rehearsals, but only as a
    fallback ON TOP of the shared variables — never as the sole source."""
    source = _read(_SWITCH_POOL)
    assert 'CANDIDATE_ARCH="${SWITCH_CANDIDATE_ARCH:-$(env_value CANARY_AGENT_ARCH fc_loop)}"' in source
    assert (
        'CANDIDATE_SPECIALISTS_RAW="${SWITCH_CANDIDATE_SPECIALISTS:-'
        '$(env_value CANARY_MANAGER_V1_SPECIALISTS 0)}"'
    ) in source
    assert "SWITCH_CANDIDATE_ARCH / SWITCH_CANDIDATE_SPECIALISTS" in source


def test_both_routing_modes_gate_the_maintenance_stage_identically():
    """`--stage maintenance` is the one route to 100% candidate traffic that never
    meets CANARY_ALLOW_FLIP, so it must be provably a DRAIN in BOTH routing modes.
    set_canary_weight.sh owns the weighted host; switch_pool.sh's single-upstream
    branch owns the pre-weighted one, and it is the branch no harness can exercise
    (it only fires for a CONF under /etc/nginx). Divergence here would mean
    migrating a host between routing modes changes what a release may do — the
    exact class of hole this pairing exists to close. Pinned by source, then, and
    deliberately: the alternative is no coverage at all."""
    for path in (_SET_WEIGHT, _SWITCH_POOL):
        source = _read(path)
        gate = source[source.index("maintenance)"):]
        gate = gate[: gate.index("note ")]
        assert 'RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" == 1' in gate, path
        assert "^deploy-maintenance-[0-9a-f]{7,}$" in gate, path
        assert 'MAINTENANCE_MARKER="$(maintenance_marker_path)"' in gate, path
        assert '-r "$MAINTENANCE_MARKER"' in gate, path
        assert '"$ROLLOUT_ID"' in gate, path
    # ...and deploy/update.sh is the only thing that can satisfy the marker gate,
    # because it is the only writer.
    update = _read(_UPDATE)
    assert "open_maintenance_window" in update
    assert "close_maintenance_window" in update
    assert "rentcompass-maintenance-drain" in update
    for path in (_SET_WEIGHT, _SWITCH_POOL):
        assert "rentcompass-maintenance-drain" in _read(path), path


def test_the_candidate_identity_whitelist_is_intact_everywhere_it_appears():
    """fc_loop:0(:0) and manager_v1:1(:0) are the only accepted pairs; a
    compatibility-shell manager_v1 canary must stay refusable at every entry point."""
    assert "fc_loop:0|manager_v1:1" in _read(_SWITCH_POOL)
    for path in (_UPDATE, os.path.join(_ROOT, "deploy", "release.sh")):
        assert "fc_loop:0:0|manager_v1:1:0" in _read(path), path
