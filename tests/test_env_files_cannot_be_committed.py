"""No env-shaped file may ever be stageable in this PUBLIC repo — backups included.

Found 2026-07-26 by reading the deploy tree, not by a test. Two files were sitting in
`/home/shuhan/uk_rent_recommendation` (production):

  * `./.env.bak-pre-042c477` — held the **current production SEARXNG_SECRET** in plaintext.
  * `app/.env.bak-pre-v4flash` — held `DEEPSEEK_MODEL="deepseek-chat"`, the value the
    provider retired on 2026-07-24, which silently broke both pools for a full day.

`.gitignore` matched `.env`, `.env.local` and `**/.env` — the live files, and nothing else.
Neither backup matched any pattern, so both were untracked but **not ignored**: one
`git add -A` from being committed to a public repository. That is not a hypothetical failure
mode here — commit `af65e40` is a documented `git add -A` contamination of this same repo,
and PR #8 exists because `deploy/searxng-settings.yml.example` once carried a real-looking
64-hex secret that turned the secret scan red.

Verified at the time: `git log --all -S<secret>` was empty and neither path had ever been
tracked, so the exposure was prospective and no rotation was needed. The files were archived
out of the tree and the pattern hole was closed.

This is the same defect class as the other seven instances (a value produced, left where a
reader could find it, never asserted on) with one difference: the "value" is a secret, and
the reader would have been the public. So the fix is a SOURCE GUARD, not a cleanup — the
cleanup removes today's two files, this file stops the ninth.

**The same hole, a second artifact.** `.gitignore` had exactly one rule for evidence trees,
`.runtime/`, which with its trailing slash matches that one directory and nothing else — so
the six `.runtime-*/` per-experiment trees currently sitting untracked in the production tree
matched nothing either. That is not a new risk, it is the *already-realised* one: af65e40 swept
a 3469-line untracked results package in, and the response was to add
`/evaluation/results/schema_compaction_ab_2026-07-22/` — the instance, by name. Naming
instances is how this repo reached seven of one class. Both artifact families are covered here
because they are one hole in one file: every rule matched an exact live path, and every
operator-created sibling of that path was stageable.

Note on method: `git` is NOT available to these tests. The suite runs in the
`uk-rent-agent:bench-git` image with the worktree bind-mounted at `/patched`, and a git
worktree's `.git` is a *file* pointing at a host path that does not exist in the container,
so `git check-ignore` fails there with "not a git repository". `_ignored()` below is
therefore a deliberately small re-implementation of gitignore matching — last-pattern-wins
with negation, which is the real rule — covering only the simple `name*` / `**/name*` shapes
this block uses. It is NOT a general gitignore engine and must not be reused as one. The
patterns were additionally confirmed against real `git check-ignore` on the host when the
block was written.
"""
from __future__ import annotations

import fnmatch
import pathlib
import re
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
GITIGNORE = REPO / ".gitignore"

# Env files that are TRACKED on purpose. Nothing else env-shaped may be committable.
_TRACKED_EXAMPLES = {".env.example", "app/.env.example"}

# Filenames an operator plausibly creates. Each MUST be ignored. The dated and sha-suffixed
# forms are the ones that actually occurred; the rest are the same habit spelled differently.
_MUST_BE_IGNORED = (
    ".env",
    ".env.local",
    ".env.bak",
    ".env.bak-pre-042c477",          # the real file, with the live SEARXNG_SECRET
    ".env.save",
    ".env.orig",
    ".env.old",
    ".env.2026-07-26",
    ".env~",
    "app/.env",
    "app/.env.bak-pre-v4flash",      # the real file, with the retired DEEPSEEK_MODEL
    "app/.env.production",
    "deploy/.env.backup",
)

# Evidence/runtime trees an operator creates per experiment. All six `.runtime-*` names below
# are real: they are sitting untracked in the production tree as of 2026-07-26.
_RUNTIME_MUST_BE_IGNORED = (
    ".runtime/x.jsonl",
    ".runtime-fpon/x.jsonl",
    ".runtime-fpoff/x.jsonl",
    ".runtime-hardening/x.jsonl",
    ".runtime-legctl/x.jsonl",
    ".runtime-fccontrol/x.jsonl",
    ".runtime-fccompact/x.jsonl",
    ".runtime-anything-later/x.json",
)

# Evidence packages that ARE committed provenance (HANDOFF §6) and must stay tracked. The
# `.runtime*` scoping exists so that broadening the rule above cannot silently drop these.
_COMMITTED_PROVENANCE = (
    "evaluation/results/live_routed_98",
    "evaluation/results/phase2_ab_2026-07-19",
    "evaluation/results/REPORT.md",
)

# Anything that looks like a secret an operator would paste into an env file. The 64-hex form
# is what SEARXNG_SECRET actually is, and what PR #8 had to replace in a committed example.
_SECRET_SHAPED = re.compile(
    r"""(?xi)
    \b[0-9a-f]{32,}\b                       # 32+ hex: SEARXNG_SECRET is 64
    | \bsk-[A-Za-z0-9_-]{16,}\b             # OpenAI / DeepSeek style key
    | \b(?:AIza|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b
    """
)

# Placeholders that are the POINT of an example file, so must never be flagged.
_PLACEHOLDER = re.compile(
    r"(?i)\b(?:your|example|placeholder|changeme|change_me|replace|dummy|xxx+|<[^>]+>)\b"
)


def _rules() -> list[tuple[str, bool]]:
    """(pattern, negated) in file order. Blank lines and comments dropped."""
    rules: list[tuple[str, bool]] = []
    for raw in GITIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((line[1:] if negated else line, negated))
    return rules


def _matches(pattern: str, path: str) -> bool:
    """True if `pattern` matches `path`, for the shapes this repo's .gitignore uses.

    The rule that is easy to get wrong, and that this helper got wrong on its first draft:
    a pattern naming a DIRECTORY ignores everything beneath it. `.runtime/` does not merely
    match the name `.runtime`, it excludes `.runtime/x.jsonl` too. Matching only the basename
    reports `.runtime/x.jsonl` as un-ignored, which is false — and a guard that misreports
    what git does is worse than no guard, since it invites exactly the "we asserted on it"
    confidence this defect class thrives on. So bare patterns are matched against every path
    COMPONENT, not just the last one.
    """
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if pattern.startswith("**/"):
        pattern = pattern[3:]            # `**/x` == bare `x`: matches at any depth
    parts = path.split("/")

    if "/" in pattern:                   # anchored, relative to the repo root
        anchored = pattern.lstrip("/").split("/")
        if len(anchored) > len(parts):
            return False
        # An anchored directory prefix ignores everything beneath it.
        return all(fnmatch.fnmatch(p, a) for p, a in zip(parts[: len(anchored)], anchored))

    # A trailing slash means "directory", so it can only match a component that HAS children
    # here — i.e. not the final path segment.
    candidates = parts[:-1] if dir_only else parts
    return any(fnmatch.fnmatch(p, pattern) for p in candidates)


def _ignored(path: str) -> bool:
    """Last matching rule wins — gitignore's actual precedence, negation included."""
    verdict = False
    for pattern, negated in _rules():
        if _matches(pattern, path):
            verdict = not negated
    return verdict


@pytest.mark.parametrize("path", _MUST_BE_IGNORED)
def test_every_env_variant_is_ignored(path):
    """Fails on the pre-2026-07-26 .gitignore for every `.bak`/dated/`~` form.

    Before the fix the block was `.env` + `.env.local` + `**/.env`, so `.env.bak-pre-042c477`
    — the file that actually held the production secret — matched nothing.
    """
    assert _ignored(path), (
        f"{path!r} is not ignored, so it can be staged into a PUBLIC repo. "
        "Operator env backups are exactly how a live secret leaks; see this module's docstring."
    )


@pytest.mark.parametrize("path", sorted(_TRACKED_EXAMPLES))
def test_tracked_examples_are_not_ignored(path):
    """The broad pattern must not swallow the example files, which are tracked on purpose."""
    assert not _ignored(path), (
        f"{path!r} became ignored. It is tracked deliberately — a negation was dropped or "
        "the env block was narrowed. Keep `!.env.example` / `!**/.env.example`."
    )
    assert (REPO / path).is_file(), f"{path} is declared tracked but is missing from the tree"


def test_no_env_backup_is_sitting_in_the_tree():
    """The cleanup half: catch the next backup someone leaves behind.

    Ignored or not, a plaintext production secret inside the repo tree is a hazard — it is
    one `cp` from being restored over a live config, which is how the retired DEEPSEEK_MODEL
    would come back.
    """
    strays = sorted(
        str(p.relative_to(REPO))
        for p in REPO.rglob(".env*")
        if p.is_file()
        and str(p.relative_to(REPO)) not in _TRACKED_EXAMPLES
        and ".git" not in p.parts
        and "node_modules" not in p.parts
    )
    # A live `.env` / `app/.env` is legitimate in a deployment tree; a *backup* is not.
    backups = [s for s in strays if not s.endswith(("/.env", ".env"))]
    assert not backups, (
        f"env backups present in the tree: {backups}. Archive them outside the repo "
        "(see /home/shuhan/fp-results/env-archive-2026-07-26/WHY.txt for the precedent)."
    )


@pytest.mark.parametrize("path", _RUNTIME_MUST_BE_IGNORED)
def test_every_runtime_evidence_tree_is_ignored(path):
    """Fails on the pre-fix .gitignore for every `.runtime-*` sibling.

    `.runtime/` matched one directory. af65e40 is the realised version of this: a `git add -A`
    swept an untracked 3469-line results package in, and the fix named that one directory.
    """
    assert _ignored(path), (
        f"{path!r} is not ignored. Evidence trees are retained on disk and never committed "
        "(HANDOFF §6); af65e40 is what happens when one is stageable."
    )


@pytest.mark.parametrize("path", _COMMITTED_PROVENANCE)
def test_committed_provenance_stays_tracked(path):
    """The evidence rule must stay scoped to `.runtime*` and not swallow committed packages."""
    assert not _ignored(path), (
        f"{path!r} became ignored. These are committed provenance — the evidence rule was "
        "broadened too far. Keep it scoped to `.runtime*`."
    )
    assert (REPO / path).exists(), f"{path} is declared committed provenance but is absent"


def _git_available() -> bool:
    """git is absent for the containerised suite; see this module's docstring."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=REPO,
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _git_available(), reason="no usable git (containerised worktree)")
def test_subset_matcher_agrees_with_real_git():
    """Guard the guard: `_ignored()` must not drift from what git actually does.

    Every other test here is only as good as the matcher. When git IS reachable — a developer
    host, or CI with a real checkout — cross-check the matcher against the authority instead of
    trusting a re-implementation. This caught a real bug in the first draft: bare patterns were
    matched against the basename only, so `.runtime/x.jsonl` was reported un-ignored when git
    excludes it via the `.runtime/` directory rule.
    """
    paths = (
        list(_MUST_BE_IGNORED)
        + list(_RUNTIME_MUST_BE_IGNORED)
        + sorted(_TRACKED_EXAMPLES)
        + list(_COMMITTED_PROVENANCE)
        + ["evaluation/results/schema_compaction_ab_2026-07-22/a.json",
           "app/main.py", "README.md", "app/core/agent_loop.py"]
    )
    mismatches = []
    for p in paths:
        real = subprocess.run(
            ["git", "check-ignore", "-q", p], cwd=REPO, capture_output=True,
        ).returncode == 0
        if _ignored(p) != real:
            mismatches.append(f"{p}: git={'ignored' if real else 'kept'} "
                              f"matcher={'ignored' if _ignored(p) else 'kept'}")
    assert not mismatches, (
        "_ignored() disagrees with git — fix the matcher, do NOT relax the expectations:\n  "
        + "\n  ".join(mismatches)
    )


@pytest.mark.parametrize("path", sorted(_TRACKED_EXAMPLES))
def test_tracked_examples_carry_no_real_secret(path):
    """Keep the secret scan green. PR #8 exists because an example carried a 64-hex secret."""
    f = REPO / path
    if not f.is_file():
        pytest.skip(f"{path} absent")
    offenders = [
        line.strip()
        for line in f.read_text(encoding="utf-8").splitlines()
        if _SECRET_SHAPED.search(line) and not _PLACEHOLDER.search(line)
    ]
    assert not offenders, (
        f"{path} contains secret-shaped values with no placeholder marker: {offenders}. "
        "Use an obvious placeholder; a committed 64-hex literal is what turned the scan red "
        "on PRs #6 and #7."
    )
