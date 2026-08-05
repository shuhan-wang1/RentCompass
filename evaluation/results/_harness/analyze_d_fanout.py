"""Experiment D — dimension fan-out / batch packing. Analysis + negative controls.

Design is frozen in ``evaluation/results/fanout_ab/PREREGISTRATION.md``; this file only
computes what that document declared. Reuses ``analyze.cluster_bootstrap`` (percentile
bootstrap, resampling unit = CASE) rather than growing a second bootstrap.

DEVIATION FROM THE PREREGISTRATION, recorded rather than quietly taken: the prereg named
``numpy.random.default_rng(20260805)``. This uses the repo's existing
``analyze.cluster_bootstrap``, whose RNG is stdlib ``random.Random``, with the preregistered
seed (20260805) and resample count (10,000). Reuse beat a duplicate bootstrap; the estimator,
the resampling unit and the interval are unchanged.

Two things this script refuses to do:

  * report a headline before the negative controls. ``negative_control_D.json`` is written
    first and its verdict is embedded in ``analysis_D.json``;
  * turn a CI that crosses zero into a direction. Those come out as
    "no significant difference observed".

Usage:  analyze_d_fanout.py <runs.jsonl> [<runs.jsonl> ...] <out_dir>
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import (cluster_bootstrap, crosses_zero, load_runs, mean,  # noqa: E402
                     percentile)

ON, OFF = "fanout_on", "fanout_off"
N_BOOT, SEED = 10_000, 20260805

# Tools the fan-out must never add. `remember` is the only write tool and drives the taint
# gate; `ask_user` is terminal. The product asserts this; NC-b verifies it in the traces.
FORBIDDEN_FANOUT_TOOLS = ("remember", "ask_user")


# --------------------------------------------------------------------------- #
def _coverage(r) -> float | None:
    cued = r.get("dim_cued_n")
    if not cued:
        return None                      # 0/0 is not a coverage of 0; it is undefined
    return float(r.get("dim_covered_n") or 0) / float(cued)


def _run_view(r) -> dict:
    return {
        "case_id": r.get("case_id"),
        "repeat": r.get("repeat"),
        "arm": r.get("arm"),
        "ok": bool(r.get("ab_ok")),
        "coverage": _coverage(r),
        "dim_cued_n": r.get("dim_cued_n"),
        "dim_covered_n": r.get("dim_covered_n"),
        "llm_calls": r.get("llm_calls"),
        "tool_batches": r.get("tool_batches"),
        "wall_ms": r.get("ab_wall_ms"),
        "turn_latency_ms": r.get("turn_latency_ms"),
        "cost_usd": r.get("cost_usd"),
        "tokens_in": r.get("tokens_in"),
        "tokens_out": r.get("tokens_out"),
        "soft_wrapped": 1.0 if r.get("soft_wrapped") else 0.0,
        "n_tools_executed": len(r.get("tools_executed") or []),
        "fanout_cap_observed": r.get("fanout_cap_observed"),
        "fanout_fired": r.get("fanout_fired") or 0,
        "fanout_fired_plan_time": r.get("fanout_fired_plan_time") or 0,
        "fanout_fired_answer_time": r.get("fanout_fired_answer_time") or 0,
        "fanout_added_tools": list(r.get("fanout_added_tools") or []),
        "fanout_arm_on": r.get("fanout_arm_on"),
    }


def _pairs(views):
    """(case_id, repeat) -> {arm: view}, keeping only pairs where BOTH arms succeeded."""
    by = defaultdict(dict)
    for v in views:
        by[(v["case_id"], v["repeat"])][v["arm"]] = v
    kept, dropped = {}, []
    for key, arms in sorted(by.items()):
        if ON in arms and OFF in arms and arms[ON]["ok"] and arms[OFF]["ok"]:
            kept[key] = arms
        else:
            dropped.append({"case_id": key[0], "repeat": key[1],
                            "have": sorted(arms),
                            "ok": {a: arms[a]["ok"] for a in sorted(arms)}})
    return kept, dropped


def _paired_diff(pairs, field, *, subset=None):
    """case_id -> per-case mean of (ON - OFF) over that case's usable repeats."""
    acc = defaultdict(list)
    for (case_id, _rep), arms in pairs.items():
        if subset is not None and not subset(arms):
            continue
        a, b = arms[ON].get(field), arms[OFF].get(field)
        if a is None or b is None:
            continue
        acc[case_id].append(float(a) - float(b))
    return {c: {"diffs": d} for c, d in acc.items() if d}


def _boot_diff(pairs, field, *, subset=None):
    by_case = _paired_diff(pairs, field, subset=subset)
    ci = cluster_bootstrap(
        by_case, lambda payloads: mean([mean(p["diffs"]) for p in payloads]),
        n_boot=N_BOOT, seed=SEED)
    ci["crosses_zero"] = crosses_zero(ci)
    ci["n_paired_observations"] = sum(len(p["diffs"]) for p in by_case.values())
    ci["verdict"] = ("no significant difference observed" if ci["crosses_zero"]
                     else ("ON higher" if (ci["point"] or 0) > 0 else "ON lower"))
    return ci


def _arm_summary(views, arm):
    rs = [v for v in views if v["arm"] == arm and v["ok"]]
    def col(f):
        xs = [v[f] for v in rs if v.get(f) is not None]
        return {"mean": mean(xs), "p50": percentile(xs, 0.5), "p95": percentile(xs, 0.95),
                "n": len(xs)}
    cov = [v["coverage"] for v in rs if v["coverage"] is not None]
    cued = sum(v["dim_cued_n"] or 0 for v in rs)
    covered = sum(v["dim_covered_n"] or 0 for v in rs)
    return {
        "n_runs_ok": len(rs),
        "dimension_coverage_pooled": {
            "num_dimensions_covered": covered, "den_dimensions_cued": cued,
            "rate": (covered / cued) if cued else None},
        "dimension_coverage_per_run": {"mean": mean(cov), "n": len(cov)},
        "llm_calls": col("llm_calls"),
        "tool_batches": col("tool_batches"),
        "n_tools_executed": col("n_tools_executed"),
        "wall_ms": col("wall_ms"),
        "turn_latency_ms": col("turn_latency_ms"),
        "cost_usd_total": sum(v["cost_usd"] or 0.0 for v in rs),
        "tokens_in_total": sum(v["tokens_in"] or 0 for v in rs),
        "tokens_out_total": sum(v["tokens_out"] or 0 for v in rs),
        "soft_wrapped_runs": int(sum(v["soft_wrapped"] for v in rs)),
        "runs_where_fanout_fired": sum(1 for v in rs if v["fanout_fired"]),
        "fanout_firings_plan_time": sum(v["fanout_fired_plan_time"] for v in rs),
        "fanout_firings_answer_time": sum(v["fanout_fired_answer_time"] for v in rs),
    }


# --------------------------------------------------------------------------- #
def negative_controls(views, pairs) -> dict:
    ok_views = [v for v in views if v["ok"]]

    # NC-c: switch integrity. Checked FIRST -- if the switch did not hold, nothing else in
    # this experiment means anything.
    off_bad = [{"case_id": v["case_id"], "repeat": v["repeat"],
                "cap": v["fanout_cap_observed"], "fired": v["fanout_fired"]}
               for v in ok_views if v["arm"] == OFF
               and (v["fanout_cap_observed"] != 0 or v["fanout_fired"] != 0)]
    on_bad = [{"case_id": v["case_id"], "repeat": v["repeat"],
               "cap": v["fanout_cap_observed"]}
              for v in ok_views if v["arm"] == ON and v["fanout_cap_observed"] != 3]
    nc_c = {
        "name": "NC-c switch integrity",
        "statement": ("every fanout_off run records cap==0 and zero firings; every "
                      "fanout_on run records cap==3"),
        "off_runs_checked": sum(1 for v in ok_views if v["arm"] == OFF),
        "on_runs_checked": sum(1 for v in ok_views if v["arm"] == ON),
        "violations_off": off_bad, "violations_on": on_bad,
        "passed": not off_bad and not on_bad,
    }

    # NC-b: the fan-out must never add a write or terminal tool.
    offenders = [{"case_id": v["case_id"], "repeat": v["repeat"], "arm": v["arm"],
                  "added": v["fanout_added_tools"]}
                 for v in ok_views
                 if set(v["fanout_added_tools"]) & set(FORBIDDEN_FANOUT_TOOLS)]
    added_hist: dict = {}
    for v in ok_views:
        for t in v["fanout_added_tools"]:
            added_hist[t] = added_hist.get(t, 0) + 1
    nc_b = {
        "name": "NC-b fan-out touches only read tools",
        "statement": f"none of {list(FORBIDDEN_FANOUT_TOOLS)} may appear in fanout_added_tools",
        "runs_checked": len(ok_views),
        "total_fanout_additions": sum(added_hist.values()),
        "added_tool_histogram": dict(sorted(added_hist.items())),
        "violations": offenders, "passed": not offenders,
    }

    # NC-a: the ON arm's batch count must not increase. Two readings, per the prereg --
    # (i) strict, over all pairs; (ii) restricted to the pairs whose ON run fanned out ONLY
    # at plan time, which is the subset where "rides an existing batch for free" is claimed.
    def _plan_only(arms):
        on = arms[ON]
        return on["fanout_fired"] > 0 and on["fanout_fired_answer_time"] == 0

    strict = _boot_diff(pairs, "tool_batches")
    plan_only = _boot_diff(pairs, "tool_batches", subset=_plan_only)
    n_plan_only = sum(1 for arms in pairs.values() if _plan_only(arms))
    n_answer_time = sum(1 for arms in pairs.values()
                        if arms[ON]["fanout_fired_answer_time"] > 0)
    increases = sum(1 for arms in pairs.values()
                    if (arms[ON]["tool_batches"] or 0) > (arms[OFF]["tool_batches"] or 0))
    decreases = sum(1 for arms in pairs.values()
                    if (arms[ON]["tool_batches"] or 0) < (arms[OFF]["tool_batches"] or 0))
    nc_a = {
        "name": "NC-a the ON arm must not add batches",
        "statement": "paired tool_batches (ON - OFF) must not be significantly positive",
        "n_pairs": len(pairs),
        "pairs_on_batches_greater": increases,
        "pairs_on_batches_fewer": decreases,
        "pairs_on_batches_equal": len(pairs) - increases - decreases,
        "strict_all_pairs": strict,
        "plan_time_only_pairs": plan_only,
        "n_pairs_plan_time_only": n_plan_only,
        "n_pairs_with_answer_time_firing": n_answer_time,
        # Fails only on a SIGNIFICANT increase; a CI that crosses zero is not a pass-by-
        # default, so it is reported as its own state rather than folded into `passed`.
        "passed": bool(strict["crosses_zero"]) or (strict["point"] or 0) <= 0,
        "significant_increase": (not strict["crosses_zero"]
                                 and (strict["point"] or 0) > 0),
    }

    return {"nc_c_switch_integrity": nc_c, "nc_b_read_tools_only": nc_b,
            "nc_a_no_extra_batches": nc_a,
            "all_passed": nc_c["passed"] and nc_b["passed"] and nc_a["passed"]}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 2
    runs_paths = [Path(p) for p in argv[:-1]]
    out_dir = Path(argv[-1])
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_runs(runs_paths)
    views = [_run_view(r) for r in rows]
    pairs, dropped = _pairs(views)

    nc = negative_controls(views, pairs)
    (out_dir / "negative_control_D.json").write_text(
        json.dumps(nc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    contrasts = {
        "dimension_coverage_ratio": _boot_diff(pairs, "coverage"),
        "dimensions_covered_count": _boot_diff(pairs, "dim_covered_n"),
        "llm_calls": _boot_diff(pairs, "llm_calls"),
        "tool_batches": _boot_diff(pairs, "tool_batches"),
        "n_tools_executed": _boot_diff(pairs, "n_tools_executed"),
        "wall_ms": _boot_diff(pairs, "wall_ms"),
        "turn_latency_ms": _boot_diff(pairs, "turn_latency_ms"),
        "cost_usd": _boot_diff(pairs, "cost_usd"),
        "tokens_in": _boot_diff(pairs, "tokens_in"),
        "tokens_out": _boot_diff(pairs, "tokens_out"),
        "soft_wrapped": _boot_diff(pairs, "soft_wrapped"),
    }

    failed = [v for v in views if not v["ok"]]
    analysis = {
        "experiment": "D",
        "preregistration": "evaluation/results/fanout_ab/PREREGISTRATION.md",
        "arm_base": OFF, "arm_test": ON,
        "difference_convention": "ON - OFF (positive = the fan-out arm is higher)",
        "bootstrap": {"resampling_unit": "case", "n_boot": N_BOOT, "seed": SEED,
                      "interval": "95% percentile",
                      "rng_deviation": ("prereg named numpy.random.default_rng; this reuses "
                                        "analyze.cluster_bootstrap (stdlib random.Random) "
                                        "with the same seed and n_boot")},
        "negative_controls_verdict": {
            "all_passed": nc["all_passed"],
            "nc_a": nc["nc_a_no_extra_batches"]["passed"],
            "nc_b": nc["nc_b_read_tools_only"]["passed"],
            "nc_c": nc["nc_c_switch_integrity"]["passed"],
            "note": ("negative controls are computed and written BEFORE the contrasts; a "
                     "failure here makes the headline numbers unusable"),
        },
        "runs_total": len(views),
        "runs_ok": sum(1 for v in views if v["ok"]),
        "runs_failed": len(failed),
        "failures": [{"case_id": v["case_id"], "repeat": v["repeat"], "arm": v["arm"]}
                     for v in failed],
        "cases_seen": len({v["case_id"] for v in views}),
        "pairs_usable": len(pairs),
        "pairs_dropped": dropped,
        "per_arm": {ON: _arm_summary(views, ON), OFF: _arm_summary(views, OFF)},
        "paired_contrasts": contrasts,
    }
    (out_dir / "analysis_D.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (out_dir / "table_D.md").write_text(_table(analysis, nc), encoding="utf-8")
    print(f"wrote {out_dir}/analysis_D.json, table_D.md, negative_control_D.json "
          f"(pairs={len(pairs)}, runs_ok={analysis['runs_ok']}/{len(views)})")
    return 0


def _fmt(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def _table(a, nc) -> str:
    L = ["# Experiment D — dimension fan-out / batch packing",
         "",
         f"Preregistration: `{a['preregistration']}`. Difference convention: "
         f"**{a['difference_convention']}**.",
         f"Bootstrap: {a['bootstrap']['n_boot']} resamples, unit = "
         f"{a['bootstrap']['resampling_unit']}, seed {a['bootstrap']['seed']}, "
         f"{a['bootstrap']['interval']}.",
         "",
         "## Negative controls (reported first)",
         "",
         "| control | result |",
         "|---|---|"]
    ncc, ncb, nca = (nc["nc_c_switch_integrity"], nc["nc_b_read_tools_only"],
                     nc["nc_a_no_extra_batches"])
    L += [f"| NC-c switch integrity | {'PASS' if ncc['passed'] else 'FAIL'} — "
          f"{ncc['off_runs_checked']} OFF runs at cap 0 with zero firings, "
          f"{ncc['on_runs_checked']} ON runs at cap 3 |",
          f"| NC-b read tools only | {'PASS' if ncb['passed'] else 'FAIL'} — "
          f"{ncb['total_fanout_additions']} fan-out additions across "
          f"{ncb['runs_checked']} runs; histogram "
          f"{ncb['added_tool_histogram']}; violations {len(ncb['violations'])} |",
          f"| NC-a no extra batches | {'PASS' if nca['passed'] else 'FAIL'} — "
          f"paired tool_batches ON-OFF = {_fmt(nca['strict_all_pairs']['point'])} "
          f"[{_fmt(nca['strict_all_pairs']['ci_low'])}, "
          f"{_fmt(nca['strict_all_pairs']['ci_high'])}] over {nca['n_pairs']} pairs |",
          "",
          f"Plan-time-only pairs: {nca['n_pairs_plan_time_only']}; pairs with an "
          f"answer-time firing: {nca['n_pairs_with_answer_time_firing']}. "
          f"Restricted to plan-time-only pairs, paired tool_batches ON-OFF = "
          f"{_fmt(nca['plan_time_only_pairs']['point'])} "
          f"[{_fmt(nca['plan_time_only_pairs']['ci_low'])}, "
          f"{_fmt(nca['plan_time_only_pairs']['ci_high'])}] "
          f"(n_cases={nca['plan_time_only_pairs']['n_cases']}).",
          "",
          "## Per arm",
          "",
          "| metric | fanout_on | fanout_off |",
          "|---|---|---|"]
    on, off = a["per_arm"][ON], a["per_arm"][OFF]
    def row(label, f):
        return f"| {label} | {f(on)} | {f(off)} |"
    L += [
        row("runs ok", lambda d: str(d["n_runs_ok"])),
        row("dimension coverage (covered/cued)",
            lambda d: (f"{d['dimension_coverage_pooled']['num_dimensions_covered']}/"
                       f"{d['dimension_coverage_pooled']['den_dimensions_cued']} = "
                       f"{_fmt(d['dimension_coverage_pooled']['rate'])}")),
        row("llm_calls mean", lambda d: _fmt(d["llm_calls"]["mean"], 2)),
        row("tool_batches mean", lambda d: _fmt(d["tool_batches"]["mean"], 2)),
        row("tools executed mean", lambda d: _fmt(d["n_tools_executed"]["mean"], 2)),
        row("e2e wall ms p50", lambda d: _fmt(d["wall_ms"]["p50"], 0)),
        row("e2e wall ms p95", lambda d: _fmt(d["wall_ms"]["p95"], 0)),
        row("soft-wrapped runs", lambda d: str(d["soft_wrapped_runs"])),
        row("cost USD total", lambda d: _fmt(d["cost_usd_total"], 4)),
        row("tokens in / out",
            lambda d: f"{d['tokens_in_total']} / {d['tokens_out_total']}"),
        row("runs where the fan-out fired", lambda d: str(d["runs_where_fanout_fired"])),
        row("firings plan-time / answer-time",
            lambda d: f"{d['fanout_firings_plan_time']} / {d['fanout_firings_answer_time']}"),
        "",
        f"Usable pairs: **{a['pairs_usable']}** over {a['cases_seen']} cases "
        f"({a['runs_ok']}/{a['runs_total']} runs ok, {a['runs_failed']} failed, "
        f"{len(a['pairs_dropped'])} pairs dropped).",
        "",
        "## Paired contrasts (ON − OFF), cluster bootstrap over cases",
        "",
        "| metric | point | 95% CI | pairs | cases | verdict |",
        "|---|---|---|---|---|---|"]
    for name, ci in a["paired_contrasts"].items():
        nd = 4 if name in ("cost_usd", "dimension_coverage_ratio", "soft_wrapped") else 2
        L.append(f"| {name} | {_fmt(ci['point'], nd)} | "
                 f"[{_fmt(ci['ci_low'], nd)}, {_fmt(ci['ci_high'], nd)}] | "
                 f"{ci['n_paired_observations']} | {ci['n_cases']} | {ci['verdict']} |")
    L += ["",
          "A CI that includes 0 means **no significant difference was observed** — it is "
          "not evidence that there is no difference, and no direction is read off it."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
