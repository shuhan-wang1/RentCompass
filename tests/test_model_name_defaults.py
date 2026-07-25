"""No configuration path may DEFAULT to a retired provider model name.

Why this exists (2026-07-25). DeepSeek retired ``deepseek-chat`` and
``deepseek-reasoner`` on 2026-07-24; both now return HTTP 400 with
``The supported API model names are deepseek-v4-pro or deepseek-v4-flash``.

``core/llm_config.py`` and ``uk_rent_agent/llm/router.py`` had already been migrated, but
``app/config.py`` still carried ``deepseek-chat`` as its ``os.getenv`` default, feeding
``core/llm_interface.py``. The outage that surfaced this was NOT caused by that default —
it was caused by a stale ``DEEPSEEK_MODEL=deepseek-chat`` in the deployment env, which
overrode three correct defaults at once. The lesson generalises: a retired name is
dangerous in a default AND in an override, and neither is visible from ``/health``, so the
public pool served 400s for a day without a single alarm.

These tests pin the source side. The env side cannot be pinned from a unit test, so
``test_no_retired_name_survives_an_env_override`` documents the one thing code CAN do about
it: fail loudly rather than pass a known-dead name to the provider.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

# Names the provider no longer accepts. Add to this set when a model is retired; never
# remove an entry — a name that was retired once is never valid again.
RETIRED_MODEL_NAMES = frozenset({"deepseek-chat", "deepseek-reasoner"})

# Every config path whose default feeds a real provider call.
_CONFIG_SOURCES = (
    "app/config.py",
    "app/core/llm_config.py",
    "src/uk_rent_agent/llm/router.py",
)

_REPO = Path(__file__).resolve().parent.parent

# `os.getenv("X", "<default>")` / `os.environ.get('X', '<default>')`, capturing the default.
_GETENV_DEFAULT = re.compile(
    r"""os\.(?:getenv|environ\.get)\s*\(\s*['"][^'"]+['"]\s*,\s*['"]([^'"]+)['"]""")


@pytest.mark.parametrize("rel", _CONFIG_SOURCES)
def test_no_getenv_default_is_a_retired_model_name(rel):
    """A retired name must never be the value you get when the env is unset."""
    src = (_REPO / rel).read_text(encoding="utf-8")
    offenders = [d for d in _GETENV_DEFAULT.findall(src) if d in RETIRED_MODEL_NAMES]
    assert not offenders, (
        f"{rel} defaults to retired model name(s) {sorted(set(offenders))}. "
        f"The provider returns HTTP 400 for these. Supported: deepseek-v4-flash / "
        f"deepseek-v4-pro.")


def test_resolved_defaults_are_not_retired(monkeypatch):
    """With every model env var unset, nothing resolves to a retired name.

    Complements the source scan: this catches a retired name that arrives by some route
    the regex cannot see (a constant, an alias table, a fallback chain).
    """
    for var in ("DEEPSEEK_MODEL", "DEEPSEEK_CHAT_MODEL", "DEEPSEEK_REASONER_MODEL",
                "DEEPSEEK_PRO_MODEL"):
        monkeypatch.delenv(var, raising=False)

    import config as app_config
    importlib.reload(app_config)
    assert app_config.DEEPSEEK_MODEL not in RETIRED_MODEL_NAMES

    from core import llm_config
    importlib.reload(llm_config)
    assert llm_config.DEEPSEEK_MODEL not in RETIRED_MODEL_NAMES

    from uk_rent_agent.llm import router as router_mod
    importlib.reload(router_mod)
    r = router_mod.ModelRouter()
    for attr in ("chat_model", "reasoner_model", "pro_model"):
        assert getattr(r, attr) not in RETIRED_MODEL_NAMES, attr

    # Every route the router can hand out, not just the three attributes.
    for purpose in ("intent", "classification", "memory", "judge", "planner", "critic",
                    "responder", "synthesis", "pro", "anything-else"):
        for kwargs in ({}, {"complex_task": True}, {"low_latency": True}):
            route = r.route(purpose, **kwargs)
            assert route.model not in RETIRED_MODEL_NAMES, (purpose, kwargs)


def test_no_retired_name_survives_an_env_override(monkeypatch):
    """A stale env override must not silently reach the provider.

    This is the failure that actually happened: DEEPSEEK_MODEL=deepseek-chat in the
    deployment env overrode three correct defaults, and the only symptom was a 400 at
    request time — invisible to /health, so the public pool was broken for a day.

    The router is the single place every model name flows through, so it is the right
    place to refuse. Marked xfail until that guard exists: the assertion below is the
    contract, not a description of today's behaviour.
    """
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    from uk_rent_agent.llm import router as router_mod
    importlib.reload(router_mod)

    guard = getattr(router_mod, "reject_retired_model_names", None)
    if guard is None:
        pytest.xfail("router has no retired-name guard yet — see the module docstring")
    with pytest.raises(ValueError, match="retired"):
        router_mod.ModelRouter()


# --------------------------------------------------------------------------- #
# The generalisation: one env var, one default.
# --------------------------------------------------------------------------- #
# DEEPSEEK_MODEL had TWO literal defaults that disagreed — 'deepseek-chat' in
# app/config.py and 'deepseek-v4-flash' in app/core/llm_config.py. That disagreement is
# what made the 2026-07-25 outage possible AND what hid it: an audit comparing the
# deployed value against "the" code default finds a match against whichever copy happens
# to agree, and reports no override. This test is the invariant that would have caught it
# before it shipped.
#
# Only LITERAL string defaults are compared. Computed defaults (str(REPO_ROOT / ...)) are
# skipped deliberately: two modules can spell the same computed path differently — one
# inline, one via a local — and comparing source text there yields false positives, which
# is exactly what a first pass of this audit produced for SEARCH_LISTING_CACHE_PATH (both
# resolve to REPO_ROOT/.runtime/listing_cache.sqlite3).

_ENV_LITERAL_DEFAULT = re.compile(
    r"""os\.(?:getenv|environ\.get)\s*\(\s*['"]([A-Z0-9_]+)['"]\s*,\s*['"]([^'"]*)['"]""")

# Vars whose defaults are allowed to differ, each with the reason. Keep this EMPTY unless a
# divergence is genuinely intended; an entry here is a standing invitation to the same bug.
_ALLOWED_DIVERGENCE: dict[str, str] = {}


def _scan_literal_env_defaults():
    found: dict[str, set[tuple[str, str]]] = {}
    for base in ("app", "src"):
        for path in sorted((_REPO / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_REPO).as_posix()
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for var, default in _ENV_LITERAL_DEFAULT.findall(line):
                    found.setdefault(var, set()).add((default, f"{rel}:{lineno}"))
    return found


def test_no_env_var_has_two_disagreeing_literal_defaults():
    """One env var, one default. Two copies WILL drift, and the drift is invisible."""
    offenders = {}
    for var, sites in _scan_literal_env_defaults().items():
        values = {d for d, _ in sites}
        if len(values) > 1 and var not in _ALLOWED_DIVERGENCE:
            offenders[var] = sorted(sites)

    assert not offenders, "env vars with disagreeing literal defaults:\n" + "\n".join(
        f"  {var}:\n" + "\n".join(f"      {d!r} at {loc}" for d, loc in sites)
        for var, sites in sorted(offenders.items()))


def test_the_scan_actually_sees_the_model_var():
    """Guard the guard: a regex that silently matches nothing would pass the test above.

    If DEEPSEEK_MODEL stops being read with a literal default the scan has lost its grip on
    the very variable this file exists for, and the invariant above is vacuous.
    """
    found = _scan_literal_env_defaults()
    assert "DEEPSEEK_MODEL" in found, "the literal-default scan no longer sees DEEPSEEK_MODEL"
    assert len(found) >= 10, f"scan found only {len(found)} env defaults — regex likely broken"
