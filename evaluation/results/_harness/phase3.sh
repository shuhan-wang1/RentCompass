#!/bin/sh
# Phase 3: (a) a second thinking-baseline probe restricted to the 20 multi-tool-batch cases
# — the deep tool loops where the HTTP-400 actually appeared — then (b) Experiment C.
# C is run ALONE (nothing else touching live tools) because its headline metric is latency.
set -eu
H=evaluation/results/_harness
R=evaluation/results

SEL=$(python3 -c "import json;print(','.join(json.load(open('$R/parallel_tools_ab/case_selection.json'))['qualifying_at_least_2_of_3']))")
echo "selected cases: $SEL"

./$H/dockrun.sh rc_eval_think2 $H/ab_runner.py \
  --experiment THINKPROBE2 --arms baseline_all_strong --repeats 1 \
  --case-ids "$SEL" \
  --out $R/fc_loop_routing_ab/thinking_probe2 \
  --cache-snapshot evaluation/benchmark/cache_snapshots/warm_v3.sqlite3 \
  --timeout-s 120 --gap-ms 300 --max-runs 30 --max-consecutive-failures 99 \
  --deadline 2026-08-04T21:00:00 > /tmp/rc_eval_cache/think2.out 2>&1 || true

echo "thinking probe 2 done at $(date '+%H:%M:%S')"

for i in 0 1; do
  nohup ./$H/dockrun.sh rc_eval_C$i $H/ab_runner.py \
    --experiment C --arms serial_tools,parallel_tools --repeats 3 \
    --case-ids "$SEL" --shard-index $i --shard-count 2 \
    --out $R/parallel_tools_ab/shard$i \
    --cache-snapshot evaluation/benchmark/cache_snapshots/warm_v3.sqlite3 \
    --timeout-s 120 --gap-ms 300 --max-runs 400 --max-consecutive-failures 10 \
    --deadline 2026-08-05T05:00:00 > /tmp/rc_eval_cache/C$i.out 2>&1 &
  echo "C shard $i launched"
done
