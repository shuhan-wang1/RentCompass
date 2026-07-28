#!/usr/bin/env bash
# Cut a release: put this box on the latest reviewed commit and deploy it.
#
#   cd /home/shuhan/uk_rent_recommendation
#   bash deploy/release.sh
#
# That is the whole procedure. One sudo password prompt (the pin file is
# root-owned); everything else is unattended.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS INSTEAD OF "just make update.sh deploy the latest commit"
# ---------------------------------------------------------------------------
# `deploy/update.sh` refuses to deploy anything but the exact sha named in
# `/etc/rentcompass/deploy.env` — a file OUTSIDE version control, so that no
# commit to this repo can change what production runs. That gate is not
# bureaucracy; it is the reason a bad merge cannot reach the public site on its
# own. This script does NOT weaken it, remove it, or route around it. update.sh
# still enforces `HEAD == DEPLOY_PINNED_SHA` exactly as before.
#
# What was actually broken was the *procedure* around the gate. The re-pin was
# three manual steps documented in deploy/monitoring/README.md, and on 2026-07-28
# a merged search fix sat unshipped for hours while the same bug was re-reported,
# because nobody had performed them. A release step people skip is not a safety
# property — it is an outage waiting for a busy day.
#
# So: the gate keeps deciding what MAY ship; this script performs the deliberate
# act of advancing the pin, with the checks that decision deserves:
#
#   1. the target is the tip of the REMOTE mainline (never a local commit, never
#      an uncommitted tree) — everything there went through a PR with required
#      CI and branch protection;
#   2. that commit's CI conclusion is queried; a FAILING one aborts;
#   3. the tracked working tree must be clean;
#   4. you are shown old-pin -> new-pin and must confirm (unless --yes);
#   5. only then is the pin advanced, and update.sh does the rest — including
#      refusing to report success unless the pool answers with that exact sha.
#
# If the deploy fails after the pin moved, the script tells you the pin is ahead
# of what is running and prints the one command that puts it back.
#
# ---------------------------------------------------------------------------
# OPTIONS
# ---------------------------------------------------------------------------
#   --ref <branch|sha>   release this instead of the mainline tip (a rollback to
#                        an older release, a hotfix tag)
#   --yes                do not ask for confirmation (for cron/CI)
#   --no-fetch           use the refs already on disk
#   --allow-failing-ci   release even though the target's CI checks FAILED
#   --dry-run            do everything except re-pin and deploy; print the plan
#   --                   everything after this is passed through to update.sh
#                        (e.g. `bash deploy/release.sh -- --both --drain`)
#
# Every external command is injectable so this can be rehearsed with no docker,
# no root and no network (see deploy/test_release_assertions.sh).
set -euo pipefail

# Resolve this script's own path BEFORE cd'ing away — `--help` reads $0, and a
# relative $0 stops resolving the moment the working directory changes.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

REPO_DIR="${RELEASE_REPO_DIR:-/home/shuhan/uk_rent_recommendation}"
cd "$REPO_DIR"

GIT_CMD="${RELEASE_GIT_CMD:-git}"
GH_CMD="${RELEASE_GH_CMD:-gh}"
SUDO_CMD="${RELEASE_SUDO_CMD:-sudo}"
UPDATE_CMD="${RELEASE_UPDATE_CMD:-bash deploy/update.sh}"
PIN_ENV_FILE="${DEPLOY_PIN_ENV:-/etc/rentcompass/deploy.env}"
# REMOTE on purpose: a local branch can hold commits that never saw a PR.
TRACK_REF="${RELEASE_TRACK_REF:-origin/telemetry/v2-layer-b}"

say()  { printf '==> %s\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

REF=""; ASSUME_YES=0; NO_FETCH=0; ALLOW_FAILING_CI=0; DRY_RUN=0; PASSTHROUGH=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)              REF="${2:-}"; shift 2 ;;
    --yes|-y)           ASSUME_YES=1; shift ;;
    --no-fetch)         NO_FETCH=1; shift ;;
    --allow-failing-ci) ALLOW_FAILING_CI=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --)                 shift; PASSTHROUGH=("$@"); break ;;
    -h|--help)          sed -n '2,/^set -euo/p' "$SELF" | sed '$d'; exit 0 ;;
    *)                  die "unknown argument: $1  (try --help; use -- to pass options to update.sh)" ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. Resolve the target
# ---------------------------------------------------------------------------
if [ "$NO_FETCH" -eq 0 ]; then
  say "Fetching origin..."
  $GIT_CMD fetch origin --prune >/dev/null 2>&1 \
    || warn "git fetch failed — falling back to the refs already on disk"
fi

WANT="${REF:-$TRACK_REF}"
TARGET="$($GIT_CMD rev-parse --verify -q "${WANT}^{commit}" || true)"
[ -n "$TARGET" ] || die "cannot resolve '$WANT' to a commit — check the ref name, and that it has been fetched"
TARGET_SHORT="$($GIT_CMD rev-parse --short "$TARGET")"

if [ -n "$REF" ]; then
  # An explicit --ref may legitimately sit off mainline (rollback, hotfix tag).
  # Say so rather than letting it pass as reviewed code.
  $GIT_CMD merge-base --is-ancestor "$TARGET" "$TRACK_REF" 2>/dev/null \
    || warn "'$REF' is NOT an ancestor of $TRACK_REF — it may never have been through a PR"
fi

# ---------------------------------------------------------------------------
# 2. Refuse a dirty tree BEFORE anything moves
# ---------------------------------------------------------------------------
# The legacy pool builds this very working tree, so an uncommitted edit would be
# baked into production; and `git checkout` would either refuse or silently carry
# the edits onto the new commit.
if ! $GIT_CMD diff --quiet HEAD 2>/dev/null; then
  echo "!! Tracked working tree is DIRTY — refusing to release uncommitted changes:" >&2
  $GIT_CMD status --porcelain --untracked-files=no >&2
  echo "!! Commit or discard the tracked changes above, then re-run." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. CI verdict for the target
# ---------------------------------------------------------------------------
# Advancing the pin is the moment this repo's review process becomes production,
# so "did this commit's checks pass" carries the weight. Three outcomes, kept
# deliberately distinct: PASSING proceeds; FAILING aborts; UNKNOWN (no gh, not
# authenticated, no checks reported) warns and proceeds — a missing CLI must not
# make the site un-releasable during an incident.
check_ci() {
  local sha="$1" out bad
  command -v "${GH_CMD%% *}" >/dev/null 2>&1 || {
    warn "'$GH_CMD' not found — cannot verify CI for $TARGET_SHORT. Proceeding UNVERIFIED."; return 0; }
  out=$($GH_CMD api "repos/{owner}/{repo}/commits/$sha/check-runs" \
          --jq '.check_runs[] | select(.status == "completed") | .conclusion' 2>/dev/null) || {
    warn "Could not read CI status for $TARGET_SHORT (gh api failed). Proceeding UNVERIFIED."; return 0; }
  if [ -z "$out" ]; then
    warn "No completed CI checks reported for $TARGET_SHORT. Proceeding UNVERIFIED."; return 0
  fi
  bad=$(grep -cE '^(failure|timed_out|cancelled|action_required|startup_failure)$' <<<"$out" || true)
  if [ "$bad" -gt 0 ]; then
    [ "$ALLOW_FAILING_CI" -eq 1 ] \
      || die "CI on $TARGET_SHORT has $bad failing check(s) — refusing to release it. Pass --allow-failing-ci to override."
    warn "CI on $TARGET_SHORT has $bad failing check(s) — proceeding under --allow-failing-ci"
    return 0
  fi
  say "CI on $TARGET_SHORT is green ($(grep -c . <<<"$out") completed check(s)) ✅"
}
check_ci "$TARGET"

# ---------------------------------------------------------------------------
# 4. Show the plan; confirm
# ---------------------------------------------------------------------------
OLD_PIN=""
if [ -r "$PIN_ENV_FILE" ]; then
  # shellcheck source=/dev/null
  DEPLOY_PINNED_SHA=""; . "$PIN_ENV_FILE"; OLD_PIN="${DEPLOY_PINNED_SHA:-}"
else
  warn "pin file '$PIN_ENV_FILE' is not readable — it will be created/replaced"
fi
HEAD_NOW="$($GIT_CMD rev-parse HEAD)"

echo
say "Release plan"
printf '    source     %s  (%s)\n' "$TARGET_SHORT" "${REF:+--ref $REF}${REF:-$TRACK_REF tip}"
printf '    HEAD       %s -> %s\n' "$($GIT_CMD rev-parse --short HEAD)" "$TARGET_SHORT"
printf '    pin        %s -> %s\n' "${OLD_PIN:0:7}${OLD_PIN:+ }${OLD_PIN:-<unset>}" "$TARGET"
printf '    then       %s %s\n' "$UPDATE_CMD" "${PASSTHROUGH[*]:-}"
echo

if [ "$OLD_PIN" = "$TARGET" ] && [ "$HEAD_NOW" = "$TARGET" ]; then
  say "Pin and HEAD are already $TARGET_SHORT — nothing to advance; handing straight to update.sh"
elif [ "$DRY_RUN" -eq 1 ]; then
  say "--dry-run: stopping here. Nothing was checked out, re-pinned or deployed."
  exit 0
elif [ "$ASSUME_YES" -eq 0 ]; then
  printf 'Proceed? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) die "aborted — nothing was changed" ;; esac
fi

# ---------------------------------------------------------------------------
# 5. Check out the target
# ---------------------------------------------------------------------------
if [ "$HEAD_NOW" != "$TARGET" ]; then
  say "Checking out $TARGET_SHORT"
  $GIT_CMD checkout --detach "$TARGET" >/dev/null 2>&1 || die "could not check out $TARGET_SHORT"
fi

# ---------------------------------------------------------------------------
# 6. Advance the pin (the one privileged step)
# ---------------------------------------------------------------------------
# Written through a temp file + `sudo tee` rather than an in-place `sudo sed`, so
# a half-written pin file cannot survive a failure: the gate reading a truncated
# DEPLOY_PINNED_SHA would refuse every deploy, including the rollback.
repin() {
  local sha="$1" tmp; tmp=$(mktemp)
  if [ -r "$PIN_ENV_FILE" ] && grep -q '^DEPLOY_PINNED_SHA=' "$PIN_ENV_FILE"; then
    awk -v v="$sha" '/^DEPLOY_PINNED_SHA=/ { print "DEPLOY_PINNED_SHA=" v; next } { print }' \
      "$PIN_ENV_FILE" > "$tmp"
  else
    [ -r "$PIN_ENV_FILE" ] && cat "$PIN_ENV_FILE" > "$tmp"
    printf 'DEPLOY_PINNED_SHA=%s\n' "$sha" >> "$tmp"
  fi
  if [ -w "$PIN_ENV_FILE" ]; then
    cat "$tmp" > "$PIN_ENV_FILE"
  else
    say "Re-pinning $PIN_ENV_FILE (this is the sudo password prompt)"
    $SUDO_CMD tee "$PIN_ENV_FILE" < "$tmp" >/dev/null || { rm -f "$tmp"; die "could not write $PIN_ENV_FILE"; }
  fi
  rm -f "$tmp"
}

if [ "$OLD_PIN" != "$TARGET" ]; then
  repin "$TARGET"
  say "Pin advanced: ${OLD_PIN:-<unset>} -> $TARGET"
else
  say "Pin already $TARGET_SHORT — left alone"
fi

# ---------------------------------------------------------------------------
# 7. Hand off to update.sh, which enforces the gate and verifies the result
# ---------------------------------------------------------------------------
echo
say "Handing off to $UPDATE_CMD"
# rc is captured on the SAME command, not from `$?` after an `if`: a bare `if
# cmd; then ... fi` returns 0 when the condition is false, so reading `$?` after
# `fi` reports success for a deploy that just failed. That is the precise shape
# of the defect this whole file exists to stop, one level up.
rc=0
$UPDATE_CMD ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || rc=$?
[ "$rc" -eq 0 ] && exit 0

# The pin now names a commit that is NOT running. Leaving that silent would make
# the next --status read "in sync" while production is on something else.
echo
warn "DEPLOY FAILED after the pin was advanced to $TARGET_SHORT."
warn "The pin now names a commit that is NOT running. Either fix and re-run:"
warn "    bash deploy/update.sh"
if [ -n "$OLD_PIN" ] && [ "$OLD_PIN" != "$TARGET" ]; then
  warn "  ...or put the pin back where it was:"
  warn "    sudo sed -i 's/^DEPLOY_PINNED_SHA=.*/DEPLOY_PINNED_SHA=$OLD_PIN/' $PIN_ENV_FILE"
  warn "    git checkout --detach $OLD_PIN"
fi
exit "$rc"
