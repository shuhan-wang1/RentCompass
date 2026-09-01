"""`USE_MCP_TOOLS` has exactly one parser, and it is the validated one.

C3's R1-M3 finding, app-side half. `app/app.py` decided whether to start the MCP
tool client by re-reading the raw environment with its own spelling rule::

    os.environ.get("USE_MCP_TOOLS", "0").lower() not in ("0", "false", "no")

`config._bool_strict` accepts {1,true,yes,on} / {0,false,no,off} and REFUSES anything
else by name. The two disagreed on every spelling outside the app's three: `off` and
`''` read false in the `Config` object and TRUE in app.py. That is not a cosmetic
drift — `Config` rejects `manager_v1_specialists=1` together with `use_mcp_tools=1`
because `MCPToolClient` exposes neither specialist capability verb, so the graph
build dies with a bare RuntimeError on the first turn of a pool that has already
passed `/ready`. A switch app.py parses for itself is a switch that validation never
saw: config accepted the pair, and the app then enabled the very thing config
rejected.

The app now reads `_runtime_config.use_mcp_tools`. Config is the only parser, so an
unrecognised spelling is refused BY NAME before the server binds, and the forbidden
pair cannot be assembled in this module at all.

The behavioural checks import `app` in a SUBPROCESS: the module is imported once per
process and its startup decisions are frozen at import, so the only honest way to ask
"what would this environment have built?" is to build it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
APP_PY = _ROOT / "app" / "app.py"

_PROBE = textwrap.dedent(
    """
    import json, sys, traceback
    sys.path.insert(0, {src!r})
    sys.path.insert(0, {app!r})
    try:
        import app as appmod
    except BaseException as exc:
        print("PROBE" + json.dumps({{"imported": False,
                                     "error_type": type(exc).__name__,
                                     "error": str(exc)}}))
        sys.exit(0)
    print("PROBE" + json.dumps({{
        "imported": True,
        "config_use_mcp": appmod._runtime_config.use_mcp_tools,
        "mcp_started": appmod.agent_tool_provider is not appmod.tool_registry,
    }}))
    """
).format(src=str(_ROOT / "src"), app=str(_ROOT / "app"))


def _probe(tmp_path: Path, **env_overrides):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "CONVERSATION_DB_PATH": str(tmp_path / "conversations.sqlite3"),
        "PROPERTY_SOURCE": "csv",
        "ALLOW_LEGACY_CLIENT_USER_ID": "1",
        "CANARY_LOG_PATH": "off",
        "PYTHONIOENCODING": "utf-8",
    }
    env.update(env_overrides)
    result = subprocess.run([sys.executable, "-c", _PROBE], env=env, text=True,
                            capture_output=True, timeout=300)
    line = [l for l in result.stdout.splitlines() if l.startswith("PROBE")]
    assert line, (result.stdout[-2000:], result.stderr[-2000:])
    import json
    return json.loads(line[-1][len("PROBE"):])


# --------------------------------------------------------------------------- #
# Source guard — the parser itself                                            #
# --------------------------------------------------------------------------- #

def test_app_does_not_parse_the_switch_itself():
    """Scans CODE, not comments: the comment at the call site quotes the old rule
    on purpose, and a guard that cannot tell an explanation from an implementation
    would push the next author into deleting the explanation."""
    code = "\n".join(
        line for line in APP_PY.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_runtime_config.use_mcp_tools" in code
    assert 'USE_MCP_TOOLS' not in code, (
        "a second parser for a capability-boundary switch is the defect, not the "
        "style: app.py must not read this variable at all")
    assert '("0", "false", "no")' not in code


# --------------------------------------------------------------------------- #
# Behaviour — the spellings that used to disagree                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spelling", ["off", "", "false", "0", "no"])
def test_a_false_spelling_never_starts_mcp(tmp_path, spelling):
    """`off` and `''` are the two that read FALSE in config and TRUE in app.py."""
    probe = _probe(tmp_path, USE_MCP_TOOLS=spelling)

    assert probe["imported"] is True, probe
    assert probe["config_use_mcp"] is False
    assert probe["mcp_started"] is False, (
        f"USE_MCP_TOOLS={spelling!r} is false to the validator; the app must agree")


def test_the_forbidden_pair_cannot_be_assembled_in_the_app(tmp_path):
    """specialists + MCP must die at config load, before anything binds — not at
    graph build on the first turn of a pool already serving traffic."""
    probe = _probe(tmp_path, AGENT_ARCH="manager_v1", MANAGER_V1_SPECIALISTS="1",
                   USE_MCP_TOOLS="on")

    assert probe["imported"] is False, probe
    assert probe["error_type"] == "ValueError"
    assert "USE_MCP_TOOLS=0" in probe["error"]


@pytest.mark.parametrize("spelling", ["maybe", "2"])
def test_an_unrecognised_spelling_is_refused_by_name_not_guessed(tmp_path, spelling):
    """app.py's old rule read `maybe` and `2` as TRUE. Config refuses both."""
    probe = _probe(tmp_path, USE_MCP_TOOLS=spelling)

    assert probe["imported"] is False, probe
    assert probe["error_type"] == "ValueError"
    assert "USE_MCP_TOOLS" in probe["error"]
    assert "capability boundary" in probe["error"]
