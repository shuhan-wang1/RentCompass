#!/usr/bin/env bash
# Run canary_report with its arguments unchanged and couple only a real
# zero-tolerance verdict (exit 3) to the emergency 0% rollback verb.
#
# Exit contract:
#   0   report says proceed; traffic is NOT promoted automatically
#   1/2/64 (or any non-3 code) report status is returned unchanged; no mutation
#   3   zero-tolerance breach; rollback succeeded; breach status remains 3
#   70  zero-tolerance breach, but rollback itself FAILED (operator emergency)
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
