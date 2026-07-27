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

These tests pin the source side. The env side is pinned by the RUNTIME GUARD:
``uk_rent_agent.llm.router.reject_retired_model_names`` refuses a known-dead name at every
point where one can reach a provider, so a stale override fails loudly instead of
producing a process that answers ``/health`` happily and 400s on every real turn.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Names the provider no longer accepts. IMPORTED, not restated: this set used to be
# defined here, which made the test the source of truth for a rule only product code can
# enforce — and a copy in a test is a copy that drifts. Add to the set in router.py when a
# model is retired; never remove an entry, a name retired once is never valid again.
#
# `is_retired_model_name` comes with it so the *.env.example scan below and the runtime
# guard share one definition of "dead", including the whitespace/quote/case normalisation.
from uk_rent_agent.llm.router import RETIRED_MODEL_NAMES, is_retired_model_name

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
    place to refuse. WAS xfail while the guard was only a docstring; the guard now exists
    (``reject_retired_model_names``), so the contract is enforced rather than described.
    """
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    from uk_rent_agent.llm import router as router_mod
    importlib.reload(router_mod)

    assert getattr(router_mod, "reject_retired_model_names", None) is not None, (
        "the guard IS the contract — without it ModelRouter has nothing to refuse with")
    with pytest.raises(ValueError, match="retired"):
        router_mod.ModelRouter()


# --------------------------------------------------------------------------- #
# The guard: one source of truth, and it is product code.                     #
# --------------------------------------------------------------------------- #

def test_the_retired_set_lives_in_product_code_and_never_shrinks():
    from uk_rent_agent.llm import router as router_mod

    # frozenset equality, not identity: earlier tests in this file reload the router
    # module, which rebinds the object without changing the contract.
    assert RETIRED_MODEL_NAMES == router_mod.RETIRED_MODEL_NAMES
    assert {"deepseek-chat", "deepseek-reasoner"} <= set(router_mod.RETIRED_MODEL_NAMES), (
        "an entry was removed — a name the provider retired is never valid again")
    # An unactionable refusal ("that name is dead") just moves the outage to the next
    # guess, so every retired name owes a successor.
    for name in router_mod.RETIRED_MODEL_NAMES:
        assert router_mod.RETIRED_MODEL_SUCCESSORS.get(name), f"{name} has no successor"


def test_this_file_keeps_no_private_copy_of_the_retired_set():
    """SOURCE GUARD against the regression this fix undoes.

    The set was defined HERE before, so the test could pass while the product enforced
    nothing. Re-inlining it would restore exactly that gap, silently.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert "from uk_rent_agent.llm.router import RETIRED_MODEL_NAMES" in src
    assert not re.search(r"^RETIRED_MODEL_NAMES\s*=", src, re.M), (
        "a second definition of the retired set can drift from the one the product "
        "enforces — import it from uk_rent_agent.llm.router instead")


def test_retired_model_error_is_a_value_error():
    """Callers that already catch ValueError keep working; RetiredModelError only lets a
    caller that WANTS to distinguish a dead-config error from a bad argument do so."""
    from uk_rent_agent.llm import router as router_mod
    assert issubclass(router_mod.RetiredModelError, ValueError)


@pytest.mark.parametrize(
    "var", ["DEEPSEEK_MODEL", "DEEPSEEK_CHAT_MODEL", "DEEPSEEK_REASONER_MODEL",
            "DEEPSEEK_PRO_MODEL"])
@pytest.mark.parametrize("dead", sorted(RETIRED_MODEL_NAMES))
def test_every_model_env_var_is_refused_not_only_the_one_that_broke_prod(
        monkeypatch, var, dead):
    """Every previous fix in this repo addressed the single instance that was observed.

    DEEPSEEK_MODEL is the var that caused the outage; the other three reach the provider by
    exactly the same route and must be refused on the same terms.
    """
    monkeypatch.setenv(var, dead)
    from uk_rent_agent.llm.router import ModelRouter
    with pytest.raises(ValueError, match="retired"):
        ModelRouter()


@pytest.mark.parametrize(
    "raw", ["deepseek-chat", " deepseek-chat ", '"deepseek-chat"', "'deepseek-chat'",
            "DeepSeek-Chat", "DEEPSEEK-CHAT\n"])
def test_a_mangled_env_value_is_still_refused(monkeypatch, raw):
    """Matching only the bare lowercase form would let the worst-formatted deployment
    through the guard. ``app/.env.example`` writes ``DEEPSEEK_MODEL="deepseek-chat"`` —
    python-dotenv strips those quotes, docker-compose list-form env does not.
    """
    monkeypatch.setenv("DEEPSEEK_MODEL", raw)
    from uk_rent_agent.llm.router import ModelRouter
    with pytest.raises(ValueError, match="retired"):
        ModelRouter()


def test_the_guard_still_lets_a_live_name_through(monkeypatch):
    """Guard the guard: refusing everything would pass every test above."""
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.delenv("DEEPSEEK_CHAT_MODEL", raising=False)
    from uk_rent_agent.llm.router import ModelRouter
    assert ModelRouter().chat_model == "deepseek-v4-pro"


def test_the_refusal_names_the_var_the_dead_value_and_the_successor(monkeypatch):
    """An operator reading this in a boot log must not have to go and find the fix.

    /health could not see the 400, so the error text is the entire diagnostic surface.
    """
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    from uk_rent_agent.llm.router import ModelRouter
    with pytest.raises(ValueError) as excinfo:
        ModelRouter()
    msg = str(excinfo.value)
    assert "DEEPSEEK_MODEL" in msg, "the env var to change is not named"
    assert "deepseek-chat" in msg, "the dead value is not quoted back"
    assert "deepseek-v4-flash" in msg, "the successor is not named"


def test_a_patched_route_table_cannot_smuggle_a_retired_name(monkeypatch):
    """``__init__`` is not the only way a name reaches ChatOpenAI: the eval configs
    monkeypatch ``ModelRouter.route`` (``model_router_override``), so ``create()`` — the
    actual construction boundary — checks again.
    """
    from uk_rent_agent.llm import router as router_mod
    r = router_mod.ModelRouter()
    monkeypatch.setattr(
        r, "route",
        lambda purpose, **kw: router_mod.ModelRoute("deepseek-reasoner", 0.0, 10))
    with pytest.raises(ValueError, match="retired"):
        r.create("judge")


# --------------------------------------------------------------------------- #
# The two paths that do NOT go through the router.                            #
# --------------------------------------------------------------------------- #
# A router-only guard would be DECORATIVE for these. ``core/llm_interface._call_deepseek``
# drives the raw ``openai`` SDK and ``core/llm_config._deepseek_llm`` builds its own
# ``ChatOpenAI`` — the same two bypasses that made ``install_observer`` undercount
# (tests/test_all_llm_calls_are_observed.py, PR #30's three-entry allowlist).

def test_the_raw_openai_sdk_path_refuses_before_touching_the_provider(monkeypatch):
    """_call_deepseek swallows EVERY provider error into ``return None`` plus a print —
    which is how a full day of HTTP 400s produced no alarm. A dead model name is a config
    defect, so the guard sits OUTSIDE that try/except and must propagate.
    """
    import openai

    from core import llm_interface

    monkeypatch.setattr(llm_interface, "DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    # If the guard ever regresses, this fails loudly instead of spending money.
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: pytest.fail(
        "a retired model name reached the provider client"))

    with pytest.raises(ValueError, match="retired"):
        llm_interface._call_deepseek("hello")


def test_the_second_chat_client_path_refuses_at_construction(monkeypatch):
    """``_deepseek_llm`` reads the module global, which a caller can rebind after import,
    so the import-time check alone does not cover it."""
    from core import llm_config

    monkeypatch.setattr(llm_config, "DEEPSEEK_MODEL", "deepseek-reasoner")
    with pytest.raises(ValueError, match="retired"):
        llm_config._deepseek_llm(0.1, 100)


def test_a_stale_env_var_kills_STARTUP_rather_than_serving_400s():
    """Startup-time refusal, proven end-to-end in a fresh interpreter.

    ``app/app.py`` imports ``core.llm_interface`` -> ``core.llm_config`` at module scope,
    so this import failing is the web app failing to boot. That is the point: the 2026-07-24
    outage was a process that started cleanly, passed /health, and 400d every real turn for
    a day. Loud at boot beats silent in production.

    Subprocess rather than ``importlib.reload``: a failed reload leaves the poisoned
    ``DEEPSEEK_MODEL`` bound in the live module for every later test in the session.
    """
    env = dict(os.environ)
    env["DEEPSEEK_MODEL"] = "deepseek-chat"
    env["DEEPSEEK_API_KEY"] = env.get("DEEPSEEK_API_KEY", "dummy")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO / "src"), str(_REPO / "app")] +
        ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    proc = subprocess.run(
        [sys.executable, "-c", "import core.llm_config"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=300)

    assert proc.returncode != 0, (
        "importing core.llm_config with a retired DEEPSEEK_MODEL succeeded — the process "
        "would boot, pass /health, and 400 on every real turn")
    err = proc.stderr
    assert "RetiredModelError" in err, err[-2000:]
    for expected in ("DEEPSEEK_MODEL", "deepseek-chat", "deepseek-v4-flash"):
        assert expected in err, f"boot failure does not mention {expected}:\n{err[-2000:]}"


# --------------------------------------------------------------------------- #
# SOURCE GUARD — tomorrow's bypass, not just today's.                         #
# --------------------------------------------------------------------------- #

_CHAT_CLIENT_CTORS = {"ChatOpenAI", "OpenAI", "AsyncOpenAI"}
_GUARD = "reject_retired_model_names"


def _called_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _provider_client_files() -> dict[str, dict[str, list[int]]]:
    """``{rel: {"clients": [lineno], "guards": [lineno]}}``, from the AST.

    Derived, not listed: a hand-maintained list of call sites is the thing that goes stale.
    Guards are counted as CALLS, never as the presence of the name — an import that is
    never invoked is exactly this repo's recurring defect, a value produced and then never
    consumed, and a substring check would score it as covered.
    """
    out: dict[str, dict[str, list[int]]] = {}
    for base in ("app", "src"):
        for path in sorted((_REPO / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            rel = path.relative_to(_REPO).as_posix()
            for node in ast.walk(tree):
                name = _called_name(node)
                if name in _CHAT_CLIENT_CTORS:
                    out.setdefault(rel, {}).setdefault("clients", []).append(node.lineno)
                elif name == _GUARD:
                    out.setdefault(rel, {}).setdefault("guards", []).append(node.lineno)
    return out


def test_every_file_that_builds_a_provider_client_also_refuses_retired_names():
    """Wiring the three known paths fixes today; this is what stops tomorrow's.

    The defect class in this repo is "the value was produced and nobody consumed it", and
    every previous fix addressed the single instance that had been observed. A new client
    constructor added without a retired-name check fails here rather than in production.
    """
    offenders = []
    for rel, kinds in sorted(_provider_client_files().items()):
        if kinds.get("clients") and not kinds.get("guards"):
            offenders.append(f"{rel}:{kinds['clients'][0]}")
    assert not offenders, (
        "provider client built without a retired-name check — route it through ModelRouter, "
        f"or call uk_rent_agent.llm.router.{_GUARD} on the model name first:\n  "
        + "\n  ".join(offenders))


def test_the_construction_site_scan_still_sees_all_three_known_paths():
    """Guard the guard: a scan that matches nothing passes the test above forever.

    These are the same three files as ``_CLIENT_CONSTRUCTION_ALLOWLIST`` in
    tests/test_all_llm_calls_are_observed.py — the observation guard and this one cover the
    identical set of provider boundaries, which is why the coverage claim is traced rather
    than asserted.
    """
    found = _provider_client_files()
    for rel in ("src/uk_rent_agent/llm/router.py", "app/core/llm_config.py",
                "app/core/llm_interface.py"):
        assert found.get(rel, {}).get("clients"), (
            f"the client-construction scan no longer sees {rel}")
        assert found.get(rel, {}).get("guards"), f"{rel} does not CALL {_GUARD}"


# --------------------------------------------------------------------------- #
# The OTHER injection surface: the file a human copies by hand.               #
# --------------------------------------------------------------------------- #
# The runtime guard covers the process. It does not cover ``app/.env.example``, which used
# to ship ``DEEPSEEK_MODEL="deepseek-chat"`` — so the documented onboarding step
# (``cp app/.env.example app/.env``) CONFIGURED the 2026-07-24 outage. The guard turns that
# into a loud boot failure rather than a silent 400, which is better but is not a fix: a new
# developer following the README simply cannot start the app and is given no reason why.
#
# An example file is a second write path into the very same env vars, and it is the one no
# runtime check can reach, because the damage is done before the process exists.

# Directories whose contents are not ours to police (vendored deps, virtualenvs, caches).
_NON_SOURCE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    "site-packages", ".mypy_cache", ".pytest_cache",
})

# `KEY=value`, `export KEY=value`, and the commented-out form `# KEY=value`. The commented
# form counts: a line a human is invited to uncomment is a value a human will end up with.
# Prose comments do not match, because a bare sentence has no `IDENT=` in it — which is
# what lets app/.env.example explain the retirement in words without tripping its own test.
_ENV_EXAMPLE_ASSIGNMENT = re.compile(
    r"""^\s*#?\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$""")


def _env_example_files() -> list[Path]:
    """Every example env file in the tree, DISCOVERED rather than listed by name.

    ``rglob('*.env.example')`` matches both ``app/.env.example`` and the repo-root
    ``.env.example`` (the leading ``*`` matches the empty string), so an example file added
    next to some future service is covered the moment it lands. A hand-maintained list of
    paths is exactly the thing that goes stale, which is the whole reason this file exists.

    Filesystem discovery rather than ``git ls-files``: the offline suite runs inside a
    container that mounts the worktree without its git dir, so shelling out to git here
    would make the guard silently unrunnable in the only place it is ever run.
    """
    return [p for p in sorted(_REPO.rglob("*.env.example"))
            if not (_NON_SOURCE_DIRS & set(p.parts))]


def _env_example_values(text: str):
    """Yield ``(lineno, key, value)`` as a human copying the file would read them."""
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _ENV_EXAMPLE_ASSIGNMENT.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw[:1] in ('"', "'"):
            closing = raw.find(raw[0], 1)
            value = raw[1:closing] if closing != -1 else raw[1:]
        else:
            # Unquoted values may carry a trailing `# comment`.
            value = raw.split("#", 1)[0].strip()
        yield lineno, key, value


def test_no_env_example_freezes_a_retired_model_name():
    """SOURCE GUARD, class-level: no example env file may ship a retired model name.

    This is what stops the NEXT retirement from being frozen into the onboarding path.
    ``RETIRED_MODEL_NAMES`` guarded runtime only; adding a name to that set now
    automatically fails here too if any example file still hands it to a new developer.
    """
    offenders = []
    for path in _env_example_files():
        rel = path.relative_to(_REPO).as_posix()
        for lineno, key, value in _env_example_values(
                path.read_text(encoding="utf-8", errors="replace")):
            if is_retired_model_name(value):
                offenders.append(f"{rel}:{lineno}  {key}={value!r}")
    assert not offenders, (
        "example env file ships a RETIRED model name — `cp <file> .env` would configure a "
        "provider that answers every request with HTTP 400, which /health cannot see. "
        "Successor: deepseek-v4-flash. Offenders:\n  " + "\n  ".join(offenders))


def test_the_env_example_scan_is_not_vacuous(tmp_path):
    """Guard the guard, three ways, because a scan that matches nothing passes forever.

    Discovery, extraction, and detection are each checked separately: the test above would
    stay green if the glob stopped finding files, if the parser stopped yielding values, or
    if the comparison stopped firing.
    """
    found = {p.relative_to(_REPO).as_posix() for p in _env_example_files()}
    assert {".env.example", "app/.env.example"} <= found, (
        f"the example-env discovery no longer sees both known files: {sorted(found)}")

    # Extraction: the variable this whole file exists for is actually being read.
    values = dict((k, v) for _, k, v in _env_example_values(
        (_REPO / "app/.env.example").read_text(encoding="utf-8")))
    assert values.get("DEEPSEEK_MODEL"), (
        "the example-env parser no longer extracts DEEPSEEK_MODEL from app/.env.example")

    # Detection: the same parser + predicate on a synthetic file that IS poisoned. Without
    # this, a parser bug that silently yielded nothing would look like a clean repo.
    poisoned = tmp_path / "app.env.example"
    poisoned.write_text(
        '# uncomment for the legacy pool\n'
        '# DEEPSEEK_REASONER_MODEL=deepseek-reasoner\n'
        'DEEPSEEK_MODEL="deepseek-chat"  # trailing comment\n'
        'OLLAMA_MODEL="gemma3:27b-cloud"\n', encoding="utf-8")
    caught = [(lineno, key) for lineno, key, value
              in _env_example_values(poisoned.read_text(encoding="utf-8"))
              if is_retired_model_name(value)]
    assert caught == [(2, "DEEPSEEK_REASONER_MODEL"), (3, "DEEPSEEK_MODEL")], caught


def test_the_example_env_boots_the_app_it_documents(tmp_path):
    """`cp app/.env.example app/.env` must produce a process that STARTS.

    Not the same assertion as "no retired name": this loads every example value through
    python-dotenv exactly as core/llm_config does, then runs the real runtime guard over
    the result. It is the end-to-end version — the onboarding path and the guard checked
    together rather than each against its own idea of the other.
    """
    from dotenv import dotenv_values

    from uk_rent_agent.llm.router import MODEL_ENV_VARS, reject_retired_model_names

    for path in _env_example_files():
        loaded = dotenv_values(str(path))
        rel = path.relative_to(_REPO).as_posix()
        # No exception == this example file boots clean.
        reject_retired_model_names(
            f"{rel} (as copied to .env)",
            **{var: loaded[var] for var in MODEL_ENV_VARS if loaded.get(var)})


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
