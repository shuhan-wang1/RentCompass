#!/usr/bin/env bash
# Requirement 4: every file changed between the baseline product SHA and the capture
# commit must lie inside the PRE-REGISTERED evaluation-only allowlist. Any file outside
# it — above all anything under app/ or src/ — fails immediately: a measurement probe
# that touches a product path is no longer a probe.
set -uo pipefail
BASE=${BASE:-e7977e6}
CAP=${CAP:-HEAD}
REPO=${REPO:-/home/shuhan/telemetry-v2-layer-b}

ALLOW=(
  "evaluation/run_benchmark.py"
  "evaluation/results_package.py"
  "evaluation/benchmark/cases.jsonl"
  "evaluation/benchmark/schema.json"
)

cd "$REPO"
mapfile -t CHANGED < <(git diff --name-only "$BASE" "$CAP")
echo "changed files ($BASE -> $(git rev-parse --short "$CAP")):"
fail=0
for f in "${CHANGED[@]}"; do
  ok=0
  for a in "${ALLOW[@]}"; do [ "$f" = "$a" ] && ok=1 && break; done
  if [ "$ok" = 1 ]; then echo "  [allow] $f"; else echo "  [DENY ] $f"; fail=1; fi
done
echo
echo "product-path guard:"
if git diff --name-only "$BASE" "$CAP" | grep -qE '^(app/|src/)'; then
  echo "  FAIL — capture patch touches a PRODUCT path:"
  git diff --name-only "$BASE" "$CAP" | grep -E '^(app/|src/)' | sed 's/^/    /'
  fail=1
else
  echo "  OK — no app/ or src/ file changed"
fi
echo
[ "$fail" = 0 ] && echo "ALLOWLIST-PASS" || { echo "ALLOWLIST-FAIL"; exit 1; }
