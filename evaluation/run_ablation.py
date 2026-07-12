"""RentCompass ablation orchestrator.

    python -m evaluation.run_ablation --study both --offline --smoke

Drives the SAME case subset under multiple configs (via ``run_benchmark.CaseRunner``)
and emits side-by-side comparisons with a SHARED, hard cost budget across the whole
ablation (``--max-cost-usd``; checkpoint + ``--resume``).

Two studies:

* **model** (Phase-4 A/B): ``baseline_all_strong`` vs ``routed_models`` — tokens,
  strong-model (reasoner) call count, total model calls, estimated cost, e2e latency
  (mean/p50/p95), grounded/money-grounded rate, task completion, critic repairs, plus
  the DELTAS (strong-call / token / cost reduction, latency & grounding & completion
  change). Writes ``results/ablation_model.{csv,json}``.
* **retrieval** (Phase-5 A/B): ``serial_retrieval`` vs ``parallel_retrieval`` with
  ``--repeat >= 3`` — retrieval-stage latency, e2e latency, p50/p95, tool success
  rate, final grounding, and dropped-result / race anomalies, reported as mean +/- spread
  across repeats. Writes ``results/ablation_retrieval.{csv,json}``.

OFFLINE (``--offline``, default): fake model + stubbed/fixture tools. This VALIDATES
the orchestration end-to-end and produces MECHANICS numbers only — because the model is
faked, token volume/cost/strong-call deltas are identical across configs by construction,
so the real Phase-4/5 deltas are PENDING a LIVE run. LIVE (``--live``) fills them in.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

RETRIEVAL_NODES = {"execute_tool", "dispatch_searches", "search_worker", "gather_searches"}

MODEL_STUDY_CONFIGS = ["baseline_all_strong", "routed_models"]
RETRIEVAL_STUDY_CONFIGS = ["serial_retrieval", "parallel_retrieval"]


# --------------------------------------------------------------------------- #
# Helpers reused from run_benchmark
# --------------------------------------------------------------------------- #
def _imports():
    for p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from evaluation.run_benchmark import (
        CaseRunner, load_cases, select_cases, _bootstrap_env, _percentile,
    )
    from evaluation.configs.loader import load_config, apply_config
    from evaluation.metrics import pricing as pricing_mod
    return dict(CaseRunner=CaseRunner, load_cases=load_cases, select_cases=select_cases,
                _bootstrap_env=_bootstrap_env, _percentile=_percentile,
                load_config=load_config, apply_config=apply_config,
                pricing_mod=pricing_mod)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def _reasoner_model_name() -> str:
    try:
        from uk_rent_agent.llm.router import ModelRouter
        return getattr(ModelRouter(), "reasoner_model", "deepseek-reasoner")
    except Exception:
        return "deepseek-reasoner"


def _is_strong(model: Optional[str], reasoner: str) -> bool:
    m = (model or "").lower()
    return m == reasoner.lower() or "reasoner" in m


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _spread(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


# --------------------------------------------------------------------------- #
# Per-config aggregation over RunResults
# --------------------------------------------------------------------------- #
def _agg(runs: List, reasoner: str, percentile) -> dict:
    llm = [e for r in runs for e in r.model_usage]
    tin = sum((e.get("input_tokens") or 0) for e in llm)
    tout = sum((e.get("output_tokens") or 0) for e in llm)
    strong = sum(1 for e in llm if _is_strong(e.get("model"), reasoner))
    total_calls = len(llm)
    cost = sum((r.cost_usd or 0.0) for r in runs)
    lat = [r.turn_latency_ms for r in runs if r.turn_latency_ms is not None]
    rstage = [sum(v for k, v in (r.node_latencies or {}).items()
                  if k in RETRIEVAL_NODES and v is not None)
              for r in runs]
    grounded = sum(r.grounding.get("grounded_claims", 0) for r in runs)
    gtot = sum(r.grounding.get("total_verifiable_claims", 0) for r in runs)
    money_g = sum(r.grounding.get("money_grounded", 0) for r in runs)
    money_t = sum(r.grounding.get("money_total", 0) for r in runs)
    completed = sum(1 for r in runs if r.verdict.get("task_completed"))
    triggers = sum(len(r.critic_verdicts) for r in runs)
    repairs = sum(1 for r in runs for v in r.critic_verdicts
                  if v.get("stage") == "regenerated")
    tool_events = [e for r in runs for e in r.tool_call_events]
    tool_ok = sum(1 for e in tool_events if e.get("success"))
    n = len(runs)

    def ratio(num, den):
        return {"num": num, "den": den, "display": f"{num}/{den}",
                "rate": (num / den if den else None)}

    return {
        "n_runs": n,
        "total_input_tokens": tin,
        "total_output_tokens": tout,
        "total_tokens": tin + tout,
        "strong_model_calls": strong,
        "total_model_calls": total_calls,
        "estimated_cost_usd": cost,
        "e2e_latency_ms": {"mean": _mean(lat), "p50": percentile(lat, 0.5),
                           "p95": percentile(lat, 0.95), "spread": _spread(lat), "n": len(lat)},
        "retrieval_stage_latency_ms": {"mean": _mean(rstage), "p50": percentile(rstage, 0.5),
                                       "p95": percentile(rstage, 0.95), "spread": _spread(rstage)},
        "grounded_rate": ratio(grounded, gtot),
        "money_grounded_rate": ratio(money_g, money_t),
        "task_completion": ratio(completed, n),
        "critic_triggers": triggers,
        "critic_repairs": repairs,
        "critic_repair_success": ratio(repairs, triggers),
        "tool_success_rate": ratio(tool_ok, len(tool_events)),
    }


def _pct_reduction(base, new):
    if base in (None, 0):
        return None
    return (base - new) / base


# --------------------------------------------------------------------------- #
# Driver: run one config over the subset, honouring the shared budget
# --------------------------------------------------------------------------- #
async def _drive_config(mods, cfg_name, selected, mode, repeat, state_root, events_log,
                        budget, done_ids, raw_path, max_cost) -> List:
    cfg = mods["load_config"](cfg_name)
    runs: List = []
    # Reload prior runs for this config (resume).
    if raw_path.exists():
        from evaluation.run_benchmark import _runresult_from_dict
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                if d.get("config") == cfg.name:
                    runs.append(_runresult_from_dict(d))

    with mods["apply_config"](cfg):
        runner = mods["CaseRunner"](mode=mode, cfg=cfg, state_root=state_root,
                                    events_log=events_log, judge=False)
        for r in range(1, repeat + 1):
            for case in selected:
                run_id = f"{case.get('case_id')}#r{r}#{cfg.name}"
                if run_id in done_ids:
                    continue
                if mode == "live" and budget["done"] > 0:
                    est = budget["cost"] / max(budget["done"], 1)
                    if budget["cost"] + est > max_cost:
                        budget["stopped"] = (
                            f"cost cap reached: stopped before {run_id} "
                            f"(cumulative ${budget['cost']:.4f}, est next ${est:.4f}, "
                            f"cap ${max_cost})")
                        print(budget["stopped"])
                        return runs
                print(f"[ablation] {run_id} ({mode})", flush=True)
                rr = await runner.run(case, r)
                runs.append(rr)
                done_ids.add(run_id)
                budget["cost"] += (rr.cost_usd or 0.0)
                budget["done"] += 1
                with raw_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rr.to_dict(), ensure_ascii=False, default=str) + "\n")
                if budget["cost"] > max_cost:
                    budget["stopped"] = (f"cost cap reached after {run_id} "
                                         f"(cumulative ${budget['cost']:.4f} > cap ${max_cost})")
                    print(budget["stopped"])
                    return runs
    return runs


# --------------------------------------------------------------------------- #
# Study: model routing A/B
# --------------------------------------------------------------------------- #
async def _study_model(mods, selected, mode, repeat, out, state_root, events_log,
                       budget, done_ids, max_cost, timestamp) -> dict:
    reasoner = _reasoner_model_name()
    percentile = mods["_percentile"]
    per_config: Dict[str, dict] = {}
    for cfg_name in MODEL_STUDY_CONFIGS:
        raw = out / f"_ablation_raw_model_{cfg_name}.jsonl"
        runs = await _drive_config(mods, cfg_name, selected, mode, repeat, state_root,
                                   events_log, budget, done_ids, raw, max_cost)
        per_config[cfg_name] = _agg(runs, reasoner, percentile)
        if budget.get("stopped"):
            break

    base = per_config.get("baseline_all_strong")
    routed = per_config.get("routed_models")
    deltas = {}
    if base and routed:
        deltas = {
            "strong_call_reduction_pct": _pct_reduction(base["strong_model_calls"],
                                                         routed["strong_model_calls"]),
            "token_reduction_pct": _pct_reduction(base["total_tokens"], routed["total_tokens"]),
            "cost_reduction_pct": _pct_reduction(base["estimated_cost_usd"],
                                                 routed["estimated_cost_usd"]),
            "latency_mean_change_ms": (
                (routed["e2e_latency_ms"]["mean"] or 0) - (base["e2e_latency_ms"]["mean"] or 0)),
            "grounding_change": _rate_delta(routed["grounded_rate"], base["grounded_rate"]),
            "completion_change": _rate_delta(routed["task_completion"], base["task_completion"]),
            "note": ("Volume-driven: deepseek-chat and deepseek-reasoner share the SAME "
                     "per-token rate, so cost/token reductions reflect fewer/shorter "
                     "reasoner generations, not a cheaper rate."),
        }
    result = {
        "study": "model_routing_ab",
        "mode": mode,
        "offline_mechanics_only": mode == "offline",
        "configs": MODEL_STUDY_CONFIGS,
        "reasoner_model": reasoner,
        "n_cases": len(selected),
        "repeat": repeat,
        "per_config": per_config,
        "deltas_routed_vs_baseline": deltas,
        "stopped_reason": budget.get("stopped"),
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "caveat_offline": ("OFFLINE run: model is faked, so token/cost/strong-call numbers "
                           "are identical across configs by construction. Real Phase-4 deltas "
                           "are PENDING a LIVE run with a valid DeepSeek key."),
    }
    (out / "ablation_model.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_model_csv(out / "ablation_model.csv", per_config)
    return result


def _rate_delta(a: dict, b: dict) -> Optional[float]:
    ra, rb = a.get("rate"), b.get("rate")
    if ra is None or rb is None:
        return None
    return ra - rb


def _write_model_csv(path: Path, per_config: Dict[str, dict]) -> None:
    metrics = ["n_runs", "total_input_tokens", "total_output_tokens", "total_tokens",
               "strong_model_calls", "total_model_calls", "estimated_cost_usd"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric"] + list(per_config.keys()))
        for m in metrics:
            w.writerow([m] + [per_config[c].get(m) for c in per_config])
        for m, sub in [("e2e_latency_mean_ms", "mean"), ("e2e_latency_p50_ms", "p50"),
                       ("e2e_latency_p95_ms", "p95")]:
            w.writerow([m] + [per_config[c]["e2e_latency_ms"].get(sub) for c in per_config])
        for m in ["grounded_rate", "money_grounded_rate", "task_completion",
                  "critic_repair_success", "tool_success_rate"]:
            w.writerow([m] + [per_config[c][m]["display"] for c in per_config])


# --------------------------------------------------------------------------- #
# Study: retrieval concurrency A/B
# --------------------------------------------------------------------------- #
async def _study_retrieval(mods, selected, mode, repeat, out, state_root, events_log,
                           budget, done_ids, max_cost, timestamp) -> dict:
    percentile = mods["_percentile"]
    reasoner = _reasoner_model_name()
    per_config: Dict[str, dict] = {}
    raw_runs_by_cfg: Dict[str, List] = {}
    for cfg_name in RETRIEVAL_STUDY_CONFIGS:
        raw = out / f"_ablation_raw_retrieval_{cfg_name}.jsonl"
        runs = await _drive_config(mods, cfg_name, selected, mode, repeat, state_root,
                                   events_log, budget, done_ids, raw, max_cost)
        raw_runs_by_cfg[cfg_name] = runs
        per_config[cfg_name] = _agg(runs, reasoner, percentile)
        if budget.get("stopped"):
            break

    anomalies = _detect_race_anomalies(raw_runs_by_cfg)
    result = {
        "study": "retrieval_concurrency_ab",
        "mode": mode,
        "offline_mechanics_only": mode == "offline",
        "configs": RETRIEVAL_STUDY_CONFIGS,
        "n_cases": len(selected),
        "repeat_count": repeat,
        "per_config": per_config,
        "race_anomalies": anomalies,
        "stopped_reason": budget.get("stopped"),
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "caveat_offline": ("OFFLINE run: fake tools return instantly, so serial-vs-parallel "
                           "latency differences are negligible; this run PROVES the scheduling "
                           "path works and detects dropped/raced results. Real retrieval-stage "
                           "latency deltas are PENDING a LIVE run."),
    }
    (out / "ablation_retrieval.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_retrieval_csv(out / "ablation_retrieval.csv", per_config)
    return result


def _detect_race_anomalies(raw_by_cfg: Dict[str, List]) -> dict:
    """Compare serial vs parallel per (case_id, repeat): differing tool-call counts or
    completion status = a dropped/raced result. Zero anomalies == parallel is safe."""
    serial = {(_rid(r)): r for r in raw_by_cfg.get("serial_retrieval", [])}
    parallel = {(_rid(r)): r for r in raw_by_cfg.get("parallel_retrieval", [])}
    common = set(serial) & set(parallel)
    tool_count_mismatch = 0
    completion_mismatch = 0
    for k in common:
        s, p = serial[k], parallel[k]
        if len(s.tools_called) != len(p.tools_called):
            tool_count_mismatch += 1
        if s.verdict.get("task_completed") != p.verdict.get("task_completed"):
            completion_mismatch += 1
    return {
        "compared_pairs": len(common),
        "tool_count_mismatch": tool_count_mismatch,
        "completion_mismatch": completion_mismatch,
        "dropped_or_raced_results": tool_count_mismatch + completion_mismatch,
    }


def _rid(r) -> str:
    return f"{r.case_id}#r{r.repeat}"


def _write_retrieval_csv(path: Path, per_config: Dict[str, dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric"] + list(per_config.keys()))
        for label, key, sub in [
            ("retrieval_stage_latency_mean_ms", "retrieval_stage_latency_ms", "mean"),
            ("retrieval_stage_latency_spread_ms", "retrieval_stage_latency_ms", "spread"),
            ("e2e_latency_mean_ms", "e2e_latency_ms", "mean"),
            ("e2e_latency_p50_ms", "e2e_latency_ms", "p50"),
            ("e2e_latency_p95_ms", "e2e_latency_ms", "p95"),
            ("e2e_latency_spread_ms", "e2e_latency_ms", "spread"),
        ]:
            w.writerow([label] + [per_config[c][key].get(sub) for c in per_config])
        for m in ["tool_success_rate", "grounded_rate", "task_completion"]:
            w.writerow([m] + [per_config[c][m]["display"] for c in per_config])
        w.writerow(["n_runs"] + [per_config[c]["n_runs"] for c in per_config])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def _run(args) -> int:
    mods = _imports()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    events_log = out / "ablation_events.jsonl"
    state_root = Path(tempfile.mkdtemp(prefix="rc_ablation_state_"))
    mods["_bootstrap_env"](state_root, events_log)

    if not args.resume and events_log.exists():
        events_log.unlink()

    cases = mods["load_cases"]()
    # Default to the smoke subset when no selector is given (keeps live cost bounded).
    smoke = args.smoke or not (args.smoke or args.limit or args.category)
    selected = mods["select_cases"](cases, smoke=smoke, limit=args.limit,
                                    category=args.category)
    if not selected:
        print("No cases selected.")
        return 1

    mode = "offline" if args.offline else "live"
    timestamp = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")

    ckpt_path = out / "_ablation_checkpoint.json"
    done_ids = set()
    budget = {"cost": 0.0, "done": 0, "stopped": None}
    if args.resume and ckpt_path.exists():
        ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
        done_ids = set(ck.get("done_run_ids", []))
        budget["cost"] = float(ck.get("cost", 0.0))
        budget["done"] = int(ck.get("done", 0))
    elif not args.resume:
        # fresh: clear prior raw shards so aggregates don't double-count
        for shard in out.glob("_ablation_raw_*.jsonl"):
            shard.unlink()

    model_repeat = args.repeat
    retrieval_repeat = max(args.repeat, 3)   # Phase-5 requires >= 3

    outputs = {}
    if args.study in ("model", "both"):
        outputs["model"] = await _study_model(
            mods, selected, mode, model_repeat, out, state_root, events_log,
            budget, done_ids, args.max_cost_usd, timestamp)
        _save_ckpt(ckpt_path, done_ids, budget)
    if args.study in ("retrieval", "both") and not budget.get("stopped"):
        outputs["retrieval"] = await _study_retrieval(
            mods, selected, mode, retrieval_repeat, out, state_root, events_log,
            budget, done_ids, args.max_cost_usd, timestamp)
        _save_ckpt(ckpt_path, done_ids, budget)

    print("\n=== ablation done ===")
    if "model" in outputs:
        pc = outputs["model"]["per_config"]
        for c, m in pc.items():
            print(f"  [model] {c:<20} tokens={m['total_tokens']} "
                  f"strong_calls={m['strong_model_calls']} cost=${m['estimated_cost_usd']:.4f} "
                  f"lat_mean={m['e2e_latency_ms']['mean']}")
    if "retrieval" in outputs:
        pc = outputs["retrieval"]["per_config"]
        an = outputs["retrieval"]["race_anomalies"]
        for c, m in pc.items():
            print(f"  [retr]  {c:<20} rstage_mean={m['retrieval_stage_latency_ms']['mean']} "
                  f"e2e_mean={m['e2e_latency_ms']['mean']} n={m['n_runs']}")
        print(f"  [retr]  race_anomalies dropped/raced={an['dropped_or_raced_results']} "
              f"(compared {an['compared_pairs']} pairs)")
    if budget.get("stopped"):
        print(f"  STOPPED: {budget['stopped']}")
    print(f"  cumulative_cost=${budget['cost']:.4f} cap=${args.max_cost_usd}")

    with contextlib.suppress(Exception):
        shutil.rmtree(state_root, ignore_errors=True)
    return 0


def _save_ckpt(path: Path, done_ids, budget) -> None:
    path.write_text(json.dumps({
        "done_run_ids": sorted(done_ids), "cost": budget["cost"],
        "done": budget["done"], "stopped": budget.get("stopped"),
    }, indent=2), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evaluation.run_ablation",
                                description="RentCompass ablation orchestrator.")
    p.add_argument("--study", choices=["model", "retrieval", "both"], default="both")
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--repeat", type=int, default=1,
                   help="repeat per case (retrieval study is floored to >=3)")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--offline", action="store_true", help="fake-LLM, unbilled (default)")
    grp.add_argument("--live", action="store_true", help="real DeepSeek (PAID)")
    p.add_argument("--max-cost-usd", type=float, default=15.0,
                   help="hard shared cost cap across the WHOLE ablation")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--out", default="evaluation/results")
    p.add_argument("--timestamp", default=None)
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.live:
        args.offline = True
    if args.live and args.offline:
        raise SystemExit("choose --offline or --live, not both")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
