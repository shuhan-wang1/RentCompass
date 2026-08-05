#!/bin/sh
# Post-Experiment-A phase: analyse A, freeze C's case list, launch B + the
# thinking-baseline feasibility probe. Run from the repo root.
set -eu
H=evaluation/results/_harness
R=evaluation/results

python3 $H/analyze.py \
  --runs $R/fc_loop_routing_ab/shard0/runs.jsonl $R/fc_loop_routing_ab/shard1/runs.jsonl \
  --arm-base baseline_all_pro --arm-test routed_models \
  --out $R/fc_loop_routing_ab/analysis_A.json --n-boot 2000 --seed 20260804

python3 $H/select_c_cases.py \
  --runs $R/fc_loop_routing_ab/shard0/runs.jsonl $R/fc_loop_routing_ab/shard1/runs.jsonl \
  --out $R/parallel_tools_ab/case_selection.json

echo "phase2 analysis done"
