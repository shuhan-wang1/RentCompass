#!/usr/bin/env bash
# Run canary_report with its arguments unchanged and couple only a real
# zero-tolerance verdict (exit 3) to the emergency 0% rollback verb.
#
# Exit contract:
#   0   report says proceed; traffic is NOT promoted automatically
#   1/2/64 (or any non-3 code) report status is returned unchanged; no mutation
#   3   zero-tolerance breach; rollback succeeded; breach status remains 3
#   70  zero-tolerance breach, but rollback itself FAILED (operator emergency)
#
# 70 CAN ALSO MEAN "A DEPLOY IS RUNNING RIGHT NOW"
# ---------------------------------------------------------------------------
# `set_canary_weight.sh --weight 0` takes the deploy lock with `flock -n`, and a
# `release.sh`/`update.sh` drain holds that lock for minutes. An automated rollback
# that lands inside one therefore dies with "another release/update/switch/
# retirement operation is running" and this wrapper exits 70. That is fail-loud and
# correct — two processes must not rewrite the route at once — but 70 does NOT by
# itself prove the rollback verb is broken. Check for a running deploy first, then
# re-run `sudo bash deploy/set_canary_weight.sh --weight 0` by hand.
#
# MIXED TELEMETRY SCHEMA VERSIONS -> exit 2 (INSTRUMENTATION-HOLD)
# ---------------------------------------------------------------------------
# canary_report.py refuses a window that mixes telemetry_schema_version 2 and 3
# records: the two contracts are not comparable, so every rate in the report would
# have an unknown denominator. On the day a pool is rolled forward this is the
# normal state for a while — legacy still emitting v2 while the candidate already
# emits v3 — and the window HOLDs immediately.
#
# The correct handling is to move `--since` to AFTER both pools finished deploying,
# so the window contains one schema only. Do NOT relax the check, and do not read
# the HOLD as an SLO regression: it means the measurement is invalid, not that the
# candidate is bad. See docs/canary_runbook.md section 5.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${CANARY_GATE_REPO:-$(cd "$HERE/.." && pwd)}"
REPORT_PYTHON="${CANARY_GATE_PYTHON:-python}"
REPORT_SCRIPT="${CANARY_GATE_REPORT_SCRIPT:-$REPO/scripts/canary_report.py}"
REPORT_CMD="${CANARY_GATE_REPORT_CMD:-}"
WEIGHT_SCRIPT="${CANARY_GATE_WEIGHT_SCRIPT:-$HERE/set_canary_weight.sh}"

if [[ -n "$REPORT_CMD" ]]; then
  "$REPORT_CMD" "$@"
else
  "$REPORT_PYTHON" "$REPORT_SCRIPT" "$@"
fi
report_rc=$?

if [[ "$report_rc" != 3 ]]; then
  if [[ "$report_rc" == 0 ]]; then
    printf 'CANARY_GATE_PROCEED: report passed; traffic weight is unchanged (promotion is always explicit)\n'
  else
    printf 'CANARY_GATE_NO_CHANGE: report exit=%s; traffic weight is unchanged\n' "$report_rc" >&2
  fi
  exit "$report_rc"
fi

printf '\033[31mCANARY_ZERO_TOLERANCE_BREACH\033[0m: forcing candidate weight to 0\n' >&2
if [[ ! -x "$WEIGHT_SCRIPT" ]]; then
  printf '\033[31mROLLBACK_FAILED\033[0m: weight controller is not executable: %s\n' "$WEIGHT_SCRIPT" >&2
  exit 70
fi
if ! "$WEIGHT_SCRIPT" --weight 0; then
  printf '\033[31mROLLBACK_FAILED\033[0m: canary breach remains active; run immediately: %s --weight 0\n' "$WEIGHT_SCRIPT" >&2
  exit 70
fi
printf '\033[33mROLLBACK_COMPLETE\033[0m: candidate weight is 0; preserving gate exit 3\n' >&2
exit 3
