#!/usr/bin/env bash
# Is the monitor that guards production the monitor that is in git?
#
# THE DEFECT THIS EXISTS FOR. rentcompass-monitor.sh lives in three places, and on
# 2026-07-27 all three were different builds:
#
#   A  deploy/monitoring/rentcompass-monitor.sh  (git — nominally the source of truth)
#   B  /usr/local/bin/rentcompass-monitor.sh     (the stable copy the systemd timer runs)
#   C  $MON_REPO/deploy/monitoring/…             (the pinned production tree)
#
# B differing from A is a REAL exposure: the improvements that stop the monitor screaming
# a false alarm every five minutes, and that let it see the 2026-07-24 provider-outage
# class, exist as an untracked root-owned file. Rebuild the box, or re-run the README's
# install from the pinned tree, and production silently regresses. Nothing compared them.
#
# WHY A SCRIPT AND NOT ONLY A TEST. CI cannot see /usr/local/bin, and the file is
# root-owned, so a test alone can never be the whole guard. This script runs where both
# copies are actually visible — on the box — and is cheap enough to be the last line of
# the install procedure and of any post-reboot check. tests/test_monitor_install_provenance.py
# keeps the manifest honest so that what this script compares against is trustworthy;
# the monitor's own `src=` status token makes the running build readable from the log
# even when nobody runs either.
#
# Read-only: hashes files, reads systemd properties. Installs nothing, so it is safe to
# run from a non-root shell (the installed copy is world-readable).
#
# Exit 0 = every comparison it could make agreed. Exit 1 = drift. Exit 2 = bad usage.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/rentcompass-monitor.sh"
MANIFEST="$HERE/rentcompass-monitor.sha256"
INSTALLED="${MON_INSTALLED_PATH:-/usr/local/bin/rentcompass-monitor.sh}"
UNIT="${MON_UNIT:-rentcompass-monitor.service}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

drift=0
skipped=0

hash_of() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }
short()   { printf '%.12s' "$1"; }

[ -r "$SRC" ] || { red "FATAL: no repo copy at $SRC"; exit 2; }

# --write-manifest is the ONLY supported way to update the expected hash. It exists so
# that regenerating is a named operation with one obvious spelling, rather than a
# remembered `sha256sum` incantation — an editor who has to look it up is an editor who
# skips it, and a skipped regeneration is the stale manifest this whole file guards.
# Format is deliberately `sha256sum -c`-compatible, so the manifest is also verifiable
# with stock tooling and no knowledge of this script.
if [ "${1:-}" = "--write-manifest" ]; then
  ( cd "$HERE" && sha256sum rentcompass-monitor.sh > rentcompass-monitor.sha256 ) || exit 2
  grn "wrote $MANIFEST:"
  cat "$MANIFEST"
  echo "commit it — tests/test_monitor_install_provenance.py fails while it is stale."
  exit 0
fi
if [ -n "${1:-}" ]; then
  red "usage: $(basename "$0") [--write-manifest]"; exit 2
fi

[ -r "$MANIFEST" ] || { red "FATAL: no manifest at $MANIFEST"; exit 2; }

expected="$(awk '$2 ~ /rentcompass-monitor\.sh$/ {print $1; exit}' "$MANIFEST")"
[ -n "$expected" ] || { red "FATAL: $MANIFEST names no hash for rentcompass-monitor.sh"; exit 2; }

# ── 1. the manifest must describe the repo copy ────────────────────────────────
# If this fails the manifest is stale and every comparison below is meaningless, so it
# is checked first. tests/test_monitor_install_provenance.py asserts the same thing in
# CI, which is what stops a stale manifest reaching the box at all.
repo_hash="$(hash_of "$SRC")"
if [ "$repo_hash" = "$expected" ]; then
  grn "OK   manifest describes the repo copy (src=$(short "$repo_hash"))"
else
  red "DRIFT  manifest is STALE: it says $(short "$expected") but the repo copy is $(short "$repo_hash")"
  red "       fix: bash $HERE/check_install_drift.sh --write-manifest   (then commit)"
  drift=1
fi

# ── 2. the installed copy must be the repo copy ────────────────────────────────
# This is the comparison that had no owner. Note it is the INSTALLED copy vs GIT, not vs
# the pinned production tree: the pinned tree is deliberately allowed to be older, which
# is why the override.conf in step 3 exists at all.
if [ -r "$INSTALLED" ]; then
  inst_hash="$(hash_of "$INSTALLED")"
  if [ "$inst_hash" = "$repo_hash" ]; then
    grn "OK   installed copy matches git ($INSTALLED)"
  else
    red "DRIFT  $INSTALLED is src=$(short "$inst_hash"), git is src=$(short "$repo_hash")"
    red "       production is NOT running the committed monitor. Install command:"
    red "         sudo install -m 0755 $SRC $INSTALLED"
    drift=1
  fi
else
  ylw "SKIP installed copy not readable at $INSTALLED (not this box, or not installed yet)"
  skipped=$((skipped + 1))
fi

# ── 3. systemd must actually be running the copy we just checked ──────────────
# The trap this catches: the TRACKED unit's ExecStart names the pinned production tree,
# and only an untracked override.conf redirects it to /usr/local/bin. Re-copy the tracked
# unit without the drop-in and the timer silently reverts to a months-old monitor whose
# hash nobody ever compared. Hashing the right file proves nothing if the timer runs a
# different one.
if command -v systemctl >/dev/null 2>&1 && systemctl cat "$UNIT" >/dev/null 2>&1; then
  execstart="$(systemctl show -p ExecStart --value "$UNIT" 2>/dev/null \
               | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1)"
  if [ -z "$execstart" ]; then
    ylw "SKIP could not read ExecStart from $UNIT"
    skipped=$((skipped + 1))
  elif [ "$execstart" = "$INSTALLED" ]; then
    grn "OK   $UNIT ExecStart resolves to $execstart"
  else
    red "DRIFT  $UNIT runs $execstart, not the copy checked above ($INSTALLED)"
    red "       the override.conf that redirects ExecStart is missing or was overwritten:"
    red "         sudo systemctl cat $UNIT | head -40"
    drift=1
  fi
else
  ylw "SKIP systemd unit $UNIT not present (not this box)"
  skipped=$((skipped + 1))
fi

# ── 4. what the running copy last reported about itself ────────────────────────
# Advisory, never a drift verdict: the log is rotated daily and a fresh box has none.
# It is printed because it is the one piece of evidence that survives without anybody
# running this script — see the monitor's PROVENANCE header.
LOG="${MON_LOG:-/var/log/rentcompass/monitor.log}"
if [ -r "$LOG" ]; then
  last_src="$(grep -o 'src=[0-9a-f]\{12\}' "$LOG" 2>/dev/null | tail -1 | cut -d= -f2)"
  if [ -n "$last_src" ]; then
    if [ "$last_src" = "$(short "$repo_hash")" ]; then
      grn "OK   last status line in $LOG reports src=$last_src"
    else
      ylw "NOTE last status line reports src=$last_src, git is src=$(short "$repo_hash")"
      ylw "     (a run from before the install would look like this; check the timestamp)"
    fi
  else
    ylw "NOTE $LOG has no src= token yet — pre-provenance monitor, or no run since install"
  fi
fi

echo
if [ "$drift" -ne 0 ]; then
  red "DRIFT DETECTED — the monitor guarding production is not the monitor in git."
  exit 1
fi
if [ "$skipped" -gt 0 ]; then
  grn "no drift in what was checkable ($skipped comparison(s) skipped — see SKIP above)"
else
  grn "no drift: git, manifest, the installed copy and systemd all agree."
fi
exit 0
