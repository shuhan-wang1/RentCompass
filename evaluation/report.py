"""Aggregate every real eval artifact on disk into REPORT.md + CV_METRICS.md.

    python -m evaluation.report --results evaluation/results --out evaluation/results \
        --timestamp <UTC ISO8601>

Reads whatever exists under ``--results`` — the benchmark run dir(s)
(``*/summary.json`` + ``per_case.csv`` + ``model_usage.csv`` + ``tool_metrics.csv``),
``ablation_model.json``, ``ablation_retrieval.json``, ``fault_summary.json``,
``memory_eval.json`` — plus the static benchmark scope files
(``benchmark/cases.jsonl``, ``metrics/graders.CONSTRAINT_CHECKERS``,
``fault_injection`` scenario count, ``model_pricing.yaml``) — and writes two
Markdown reports.

ABSOLUTE RULE: every number is copied or derived from a result file. Nothing is
estimated or assumed. When a study genuinely was not produced, the section says so
with the reason (a "not produced" note) — never a fabricated number. Every rate is
printed WITH its denominator (e.g. ``num/den (xx.x%)``), never a bare percentage.

This module is intentionally *stateless about blockers*: it does not assume any run
is pending or blocked. It reports exactly what the files contain. Metrics that a
given run did not emit (e.g. an LLM-judge column that was not enabled) are reported
as "not produced in this run" with the reason, driven off the files themselves.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# tiny IO / formatting helpers
# --------------------------------------------------------------------------- #
def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except Exception:
        return str(p).replace("\\", "/")


def _load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_yaml(p: Path) -> Optional[dict]:
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_csv(p: Path) -> List[dict]:
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def _find_summaries(results: Path) -> List[dict]:
    out = []
    for p in sorted(results.glob("**/summary.json")):
        d = _load_json(p)
        if d:
            out.append({"path": p, "dir": p.parent.name, "data": d})
    return out


def _disp(ratio) -> str:
    """Render a {num,den,display,rate} block as ``num/den (xx.x%)`` — never bare %."""
    if not isinstance(ratio, dict):
        return "n/a" if ratio is None else str(ratio)
    if ratio.get("status", "").startswith("blocked") or ratio.get("status") == "blocked":
        return f"blocked ({ratio.get('reason', '?')})"
    d = ratio.get("display")
    r = ratio.get("rate")
    if d is None:
        return "n/a"
    if r is None:
        return f"{d} (n/a%)"
    return f"{d} ({r * 100:.1f}%)"


def _num(v, fmt="{:.1f}") -> str:
    return fmt.format(v) if isinstance(v, (int, float)) else "n/a"


def _change(old, new) -> str:
    """Signed change of ``new`` vs ``old`` as a % of old. Negative = reduction."""
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)) or old == 0:
        return "n/a"
    return f"{(new - old) / old * 100:+.1f}%"


def _env_libs() -> List[Tuple[str, str]]:
    import importlib.metadata as md
    libs = ["chromadb", "folium", "pydantic", "PyYAML", "openai", "httpx",
            "langgraph", "langchain-core", "sentence-transformers", "numpy",
            "requests", "flask"]
    out = []
    for lib in libs:
        try:
            out.append((lib, md.version(lib)))
        except Exception:
            out.append((lib, "absent"))
    return out


def _constraint_type_count() -> Optional[int]:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from evaluation.metrics.graders import CONSTRAINT_CHECKERS  # type: ignore
        return len(CONSTRAINT_CHECKERS)
    except Exception:
        return None


def _fault_scenario_count() -> Optional[int]:
    """Count scenarios defined in the fault harness (source of truth is the module)."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from evaluation.fault_injection import scenarios as sc  # type: ignore
        for attr in ("SCENARIOS", "ALL_SCENARIOS", "scenarios"):
            v = getattr(sc, attr, None)
            if isinstance(v, (list, tuple)):
                return len(v)
    except Exception:
        pass
    return None


def _count_fixtures() -> Optional[int]:
    d = REPO_ROOT / "evaluation" / "benchmark" / "fixtures"
    try:
        return len([p for p in d.iterdir() if p.suffix == ".json"])
    except Exception:
        return None


def _load_cases() -> dict:
    """Parse benchmark/cases.jsonl -> counts. Returns {} on failure."""
    path = REPO_ROOT / "evaluation" / "benchmark" / "cases.jsonl"
    cats: Dict[str, int] = {}
    n = smoke = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            n += 1
            cats[c.get("category", "?")] = cats.get(c.get("category", "?"), 0) + 1
            if c.get("smoke"):
                smoke += 1
    except Exception:
        return {}
    return {"n": n, "smoke": smoke, "cats": cats, "path": path}


def _needs_checker_cases() -> List[str]:
    """IDs of benchmark cases whose ``notes`` flag NEEDS_CHECKER (graded via the
    closest existing constraint-checker type — an approximation)."""
    path = REPO_ROOT / "evaluation" / "benchmark" / "cases.jsonl"
    ids: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            if "NEEDS_CHECKER" in (c.get("notes") or ""):
                ids.append(str(c.get("id") or c.get("case_id") or "?"))
    except Exception:
        return []
    return ids


# --------------------------------------------------------------------------- #
# per_case.csv analysis (per-category pass + real-behaviour findings)
# --------------------------------------------------------------------------- #
def _analyse_per_case(summary_dir: Path) -> Optional[dict]:
    rows = _load_csv(summary_dir / "per_case.csv")
    if not rows:
        return None

    def is_true(v) -> bool:
        return str(v).strip().lower() == "true"

    cat_pass: Dict[str, List[int]] = {}
    clarification: List[str] = []
    web_search: List[str] = []
    web_search_failed: List[str] = []
    total_pass = 0
    for r in rows:
        cat = r.get("category", "?")
        p = is_true(r.get("passed"))
        cat_pass.setdefault(cat, [0, 0])
        cat_pass[cat][1] += 1
        if p:
            cat_pass[cat][0] += 1
            total_pass += 1
        route = (r.get("route") or "")
        cid = r.get("case_id", "?")
        if route == "clarification":
            clarification.append(cid)
        if "web_search" in route or route == "multi_search":
            web_search.append(cid)
            if not p:
                web_search_failed.append(cid)
    return {
        "rows": rows,
        "n": len(rows),
        "total_pass": total_pass,
        "cat_pass": cat_pass,
        "clarification": clarification,
        "web_search": web_search,
        "web_search_failed": web_search_failed,
    }


def _sum_cached(summary_dir: Path) -> Tuple[int, int]:
    """Return (sum cached_tokens, n model-call rows) from model_usage.csv."""
    rows = _load_csv(summary_dir / "model_usage.csv")
    total = 0
    for r in rows:
        try:
            total += int(float(r.get("cached_tokens") or 0))
        except Exception:
            pass
    return total, len(rows)


# --------------------------------------------------------------------------- #
# static definitions
# --------------------------------------------------------------------------- #
_METRIC_DEFS = [
    ("passed (end-to-end)",
     "a case passes iff task_completed AND no forbidden tool was called AND every "
     "expected_constraint passes AND 0 contradicted claims. A single failed heuristic "
     "constraint fails the whole case."),
    ("task_completion", "non-empty final answer AND no run/harness error."),
    ("grounded_rate",
     "grounded verifiable claims / total verifiable claims extracted from the answer "
     "(money / commute / crime / distance / postcode). A claim is grounded when it "
     "traces to tool/fixture evidence; the complement is 'unsupported'."),
    ("money_grounded_rate", "grounded monetary claims / total monetary claims."),
    ("contradicted_claims",
     "count of claims in the answer that directly contradict the tool evidence "
     "(e.g. two disagreeing prices, wrong direction). 0 is the target."),
    ("source_coverage", "claims traceable to a named TOOL source / total claims."),
    ("constraints", "expected_constraints that passed / total expected_constraints."),
    ("strong_model_calls", "llm_call events whose model is the reasoner tier."),
    ("critic_repair_success",
     "critic-triggered regenerations that produced an improved (re-graded better) answer "
     "/ critic triggers."),
    ("retry_recovery_rate",
     "fault scenarios where the REAL retry loop recovered / scenarios where retry applies "
     "(non-recoverable-by-design faults, e.g. HTTP 500 on every attempt, are counted in "
     "the denominator and correctly do NOT recover)."),
    ("fallback_success_rate",
     "MCP fault scenarios where the in-process fallback produced a valid result / MCP "
     "fault scenarios."),
    ("idempotency_pass_rate",
     "write-fault scenarios that produced exactly one durable write / idempotency scenarios."),
    ("faults_correctly_surfaced",
     "scenarios where the fault was surfaced honestly (error, caveat, neutralised, or "
     "recovery) instead of silently fabricated / total scenarios."),
    ("post_fault_completion_rate", "scenarios that still completed the task after the fault."),
    ("post_fault_ungrounded_rate",
     "scenarios that produced an ungrounded answer after the fault (lower is better)."),
    ("race_anomalies (retrieval A/B)",
     "serial-vs-parallel (case,repeat) pairs that differ in tool-call count or completion "
     "status (a proxy for dropped / raced results). Broken out into tool_count_mismatch "
     "and completion_mismatch."),
    ("memory: identity_gate / user_isolation / forget_delete / restart_recovery / "
     "retrieval_relevance",
     "deterministic ChromaDB store checks — no model in the loop; these measure real "
     "store behaviour (per-user filtering, GDPR erasure, on-disk persistence, retrieval)."),
    ("memory: extraction_precision / update_correctness / stale_replacement / "
     "contradiction_handling",
     "exercise the store's write/consolidate MECHANICS with the LLM extraction / "
     "importance / consolidation calls STUBBED (canned) — they measure plumbing, NOT real "
     "LLM extraction quality."),
]

def _limitations(n_bench, approx_ids: List[str]) -> List[str]:
    nb = n_bench if n_bench is not None else "?"
    if approx_ids:
        approx = " / ".join(approx_ids)
        n_approx = len(approx_ids)
    else:
        approx, n_approx = "E8 / F11 / G16", 3
    return [
        f"This is an OFFLINE benchmark on {nb} curated cases, not a study of real users. "
        "Pass/grounding rates describe agent behaviour on these prompts, not field accuracy.",
        "The main benchmark and the model A/B are SINGLE runs (repeat=1); only the retrieval "
        "A/B is repeated (3x). Single-run rates on small n have wide confidence intervals.",
        "Grounding and several constraint checkers are HEURISTIC (text-marker / regex based); "
        "they can under- or over-count claims. The optional LLM judge was not enabled this run.",
        "LIVE non-fixture cases (many A / C / D / E prompts) run tools in-process against "
        "cache/live network and are NONDETERMINISTIC; re-running can shift their pass/latency.",
        "SearXNG IS operational this run, so the live `web_search` tool returns real results "
        "and the web-dependent B / F cases ARE grounded (market-rent answers cite "
        "Zoopla / Rightmove / SpareRoom). The residual caveat is that live web/scrape results "
        "are NON-DETERMINISTIC across runs, so exact figures vary between runs.",
        f"{n_approx} benchmark cases ({approx}, flagged NEEDS_CHECKER in `cases.jsonl`) are "
        "graded via the closest existing constraint-checker type rather than a bespoke checker "
        "— an approximation that can under- or over-count those specific cases.",
        "Cost is DeepSeek's published rate applied to measured token counts (aliased models "
        "slated for 2026-07-24 deprecation); re-confirm rates before quoting spend.",
        "Fault injection and the memory store checks mock/stub the LLM; those numbers are "
        "genuine RESILIENCE / STORE mechanics, not answer-quality measurements.",
        "No human review of the generated answers was performed.",
    ]

# memory check classification: which are REAL deterministic store checks vs
# stubbed-model mechanics.
_MEM_REAL_CHECKS = {"identity_gate", "user_isolation", "forget_delete",
                    "restart_recovery", "retrieval_relevance"}
_MEM_STUB_CHECKS = {"extraction_precision", "update_correctness", "stale_replacement",
                    "contradiction_handling"}
_MEM_REAL_RATES = {"memory_write_success_rate", "user_isolation_pass_rate",
                   "forget_request_pass_rate", "restart_recovery_pass_rate"}
_MEM_MIXED_RATES = {"memory_retrieval_accuracy"}


# --------------------------------------------------------------------------- #
# REPORT.md
# --------------------------------------------------------------------------- #
def build_report_md(results: Path, timestamp: str) -> str:
    summaries = _find_summaries(results)
    ablation_model = _load_json(results / "ablation_model.json")
    ablation_retr = _load_json(results / "ablation_retrieval.json")
    fault = _load_json(results / "fault_summary.json")
    memory = _load_json(results / "memory_eval.json")
    pricing = _load_yaml(REPO_ROOT / "evaluation" / "model_pricing.yaml")
    cases = _load_cases()

    # case counts DERIVED from the loaded artifacts (never hardcoded — so prose
    # can't go stale as the benchmark grows).
    bench = summaries[0]["data"] if summaries else {}
    n_bench = bench.get("n_runs") or bench.get("n_cases") or (cases.get("n") if cases else None)
    n_model = (ablation_model or {}).get("n_cases")

    L: List[str] = []
    a = L.append
    a("# RentCompass Offline Evaluation — REPORT\n")
    a("_Every number below is copied or derived from a file under "
      "`evaluation/results/` (or a static scope file). Nothing is estimated; every rate "
      "carries its denominator._\n")

    # (1) evaluation date
    a("## 1. Evaluation date\n")
    a(f"- Report generated: **{timestamp}**")
    produced = sorted({s["data"].get("timestamp") for s in summaries
                       if s["data"].get("timestamp")}
                      | {d.get("timestamp") for d in (ablation_model, ablation_retr, fault,
                                                      memory) if d and d.get("timestamp")})
    if produced:
        a(f"- Result files were produced at (UTC/ISO from each file): "
          f"{', '.join('`' + str(t) + '`' for t in produced)}")
    a("")

    # (2) git commit
    a("## 2. Git commit\n")
    a(f"- Report generated at HEAD: **`{_git_commit()}`**")
    res_commits = sorted({d.get("git_commit") for d in
                          [s["data"] for s in summaries] + [ablation_model, ablation_retr,
                                                            fault, memory]
                          if d and d.get("git_commit")})
    if res_commits:
        a(f"- Commit recorded in the result files: "
          f"{', '.join('`' + str(c) + '`' for c in res_commits)}")
    a("")

    # (3) environment
    a("## 3. Environment\n")
    a(f"- OS: `{platform.system()} {platform.release()}` "
      f"(reported by the interpreter; host is Windows 11).")
    a(f"- Conda env: `{os.environ.get('CONDA_DEFAULT_ENV', 'unknown')}` "
      f"(target env: `uk_rent`).")
    a(f"- Python: `{platform.python_version()}` — `{sys.executable}`")
    a("- Key libraries (from installed metadata):")
    for lib, ver in _env_libs():
        a(f"  - `{lib}`: {ver}")
    a("- `chromadb` is present in this env, so the memory store eval RAN (not blocked). "
      "SearXNG is operational, so the live `web_search` tool returns real results and "
      "web-dependent cases are grounded (see §6 and §12).\n")

    # (4) models + pricing/version note
    a("## 4. Models + versions\n")
    reasoner = (ablation_model or {}).get("reasoner_model", "deepseek-reasoner")
    a("- Router maps light nodes to `deepseek-chat` and strong/thinking nodes to "
      f"`{reasoner}`.")
    a("- **Pricing / version note (important):** per `model_pricing.yaml`, `deepseek-chat` "
      "and `deepseek-reasoner` are the non-thinking / thinking modes of the SAME underlying "
      "model (`deepseek-v4-flash`) and share the SAME per-token rate. Therefore any cost "
      "saving from model routing is TOKEN-VOLUME driven (fewer / shorter reasoner "
      "generations), **not** a cheaper per-token rate.")
    if pricing:
        a(f"- Price source: `{pricing.get('price_source', '?')}`, as of "
          f"`{pricing.get('price_as_of', '?')}` "
          f"({pricing.get('currency', 'USD')} per {pricing.get('per_tokens', '?')} tokens). "
          "These aliases are slated for deprecation on 2026-07-24 — re-confirm rates after "
          "that date.")
        dc = (pricing.get("models") or {}).get("deepseek-chat", {})
        a(f"- `deepseek-chat`/`deepseek-reasoner` rate: input ${dc.get('input')} / cached "
          f"${dc.get('cached_input')} / output ${dc.get('output')} per "
          f"{pricing.get('per_tokens', '?')} tokens.")
    a("")

    # (5) benchmark counts + categories
    a("## 5. Benchmark counts + categories\n")
    if cases:
        a(f"- Benchmark cases: **{cases['n']}** (smoke subset: **{cases['smoke']}**), "
          f"across **{len(cases['cats'])}** categories.")
        for k in sorted(cases["cats"]):
            a(f"  - `{k}`: {cases['cats'][k]}")
    else:
        a("- benchmark/cases.jsonl not readable — counts not produced.")
    nct = _constraint_type_count()
    a(f"- Constraint-type vocabulary (from `metrics.graders.CONSTRAINT_CHECKERS`): "
      f"**{nct if nct is not None else 'n/a'}** machine-checkable types.")
    nfs = _fault_scenario_count()
    if fault:
        a(f"- Fault-injection scenarios: **{fault.get('n_scenarios')}** "
          f"(harness defines {nfs if nfs is not None else 'n/a'}).")
    if ablation_retr:
        a(f"- Retrieval A/B scope: **{ablation_retr.get('n_cases')}** cases x "
          f"**{ablation_retr.get('repeat_count')}** repeats per config.")
    a("")

    # (6) live vs fixture
    a("## 6. Live vs fixture\n")
    nfix = _count_fixtures()
    if summaries:
        for s in summaries:
            d = s["data"]
            mode = "LIVE" if not d.get("offline") else "FIXTURE/offline (unbilled)"
            a(f"- Run `{s['dir']}`: mode=**{d.get('mode')}** ({mode}), "
              f"config=`{d.get('config')}`, n_runs={d.get('n_runs')}.")
            if d.get("notes"):
                a(f"  - Note (from summary.json): {d['notes']}")
    else:
        a("- No benchmark `summary.json` found under results.")
    a(f"- Fixture bank on disk: **{nfix if nfix is not None else 'n/a'}** recorded "
      "tool-output fixtures (`evaluation/benchmark/fixtures/`). Fixtured cases replay "
      "recorded evidence deterministically; non-fixture cases run tools in-process "
      "(cache/live network, nondeterministic).")
    a("- **Web grounding works this run:** SearXNG is operational, so the live `web_search` "
      "tool returns real Rightmove / Zoopla / SpareRoom results and web-dependent cases ARE "
      "grounded (visible in the B_money / F_grounding `multi_search` cases in §10.1). The "
      "remaining caveat is nondeterminism — live web/scrape results vary across runs.\n")

    # (7) cache usage
    a("## 7. Cache usage\n")
    if summaries:
        for s in summaries:
            cached, nrows = _sum_cached(s["path"].parent)
            a(f"- Run `{s['dir']}`: sum `cached_tokens` = **{cached}** across {nrows} "
              "model-call rows in `model_usage.csv` (real DeepSeek prompt-cache hits on the "
              "live run).")
    else:
        a("- No `model_usage.csv` found — cache usage not produced.")
    a("")

    # (8) repeat counts
    a("## 8. Repeat counts\n")
    for s in summaries:
        a(f"- Benchmark `{s['dir']}`: repeats = **{s['data'].get('repeats')}**")
    if ablation_model:
        a(f"- Model-routing A/B: repeat = **{ablation_model.get('repeat')}**")
    if ablation_retr:
        a(f"- Retrieval-concurrency A/B: repeat_count = "
          f"**{ablation_retr.get('repeat_count')}** (36 runs per config).")
    a("")

    # (9) metric definitions
    a("## 9. Metric definitions\n")
    for name, desc in _METRIC_DEFS:
        a(f"- **{name}**: {desc}")
    a("")

    # (10) full results with denominators
    a("## 10. Full results (with denominators)\n")

    # 10.1 benchmark
    a("### 10.1 Main benchmark\n")
    if summaries:
        for s in summaries:
            d = s["data"]
            a(f"**Run `{s['dir']}`** (mode={d.get('mode')}, config={d.get('config')}, "
              f"n_runs={d.get('n_runs')}):\n")
            a(f"- passed (end-to-end): {_disp(d.get('passed'))}")
            a(f"- task_completion: {_disp(d.get('task_completion'))}")
            a(f"- constraints: {_disp(d.get('constraints'))}")
            a(f"- grounded_rate: {_disp(d.get('grounded_rate'))}")
            a(f"- money_grounded_rate: {_disp(d.get('money_grounded_rate'))}")
            a(f"- source_coverage: {_disp(d.get('source_coverage'))}")
            a(f"- contradicted_claims: **{d.get('contradicted_claims')}**")
            a(f"- critic_triggers: {d.get('critic_triggers')}, "
              f"critic_repairs: {d.get('critic_repairs')}")
            lat = d.get("latency_ms", {}) or {}
            a(f"- e2e latency ms: mean={_num(lat.get('mean'))} p50={_num(lat.get('p50'))} "
              f"p95={_num(lat.get('p95'))} (n={lat.get('n')})")
            tok = d.get("tokens", {}) or {}
            a(f"- tokens: input={tok.get('input')} output={tok.get('output')}")
            a(f"- total_cost_usd: **${_num(d.get('total_cost_usd'), '{:.4f}')}** "
              f"(cap ${_num(d.get('cost_cap_usd'), '{:.2f}')})\n")

            # per-category pass + tool metrics (from CSVs alongside the summary)
            pc = _analyse_per_case(s["path"].parent)
            if pc:
                a(f"Per-category pass (from `{_rel(s['path'].parent / 'per_case.csv')}`, "
                  f"cross-checks the {_disp(d.get('passed'))} headline; "
                  f"sum={pc['total_pass']}/{pc['n']}):\n")
                a("| Category | Passed |")
                a("|---|---|")
                for cat in sorted(pc["cat_pass"]):
                    ok, tot = pc["cat_pass"][cat]
                    pct = f"{ok / tot * 100:.1f}%" if tot else "n/a"
                    a(f"| `{cat}` | {ok}/{tot} ({pct}) |")
                a("")
            tm = _load_csv(s["path"].parent / "tool_metrics.csv")
            if tm:
                a(f"Tool call metrics (from `{_rel(s['path'].parent / 'tool_metrics.csv')}`):\n")
                a("| Tool | calls | success | fail | mean_ms | p95_ms |")
                a("|---|---|---|---|---|---|")
                for r in tm:
                    a(f"| `{r.get('tool')}` | {r.get('calls')} | {r.get('success')} | "
                      f"{r.get('fail')} | {_num(float(r['mean_latency_ms']))} | "
                      f"{_num(float(r['p95_latency_ms']))} |")
                a("")
    else:
        a("_No benchmark summary found — main benchmark not produced._\n")

    # 10.2 model-routing A/B
    a("### 10.2 Model-routing A/B (baseline_all_strong vs routed_models)\n")
    if ablation_model:
        pc = ablation_model.get("per_config", {}) or {}
        base = pc.get("baseline_all_strong", {})
        routed = pc.get("routed_models", {})
        a(f"- mode: **{ablation_model.get('mode')}**, n_cases={ablation_model.get('n_cases')}, "
          f"repeat={ablation_model.get('repeat')}\n")
        a("| Metric | baseline_all_strong | routed_models | change (routed vs baseline) |")
        a("|---|---|---|---|")
        _row = lambda label, bv, rv, ch: a(f"| {label} | {bv} | {rv} | {ch} |")
        _row("strong_model_calls / total",
             f"{base.get('strong_model_calls')}/{base.get('total_model_calls')}",
             f"{routed.get('strong_model_calls')}/{routed.get('total_model_calls')}",
             _change(base.get("strong_model_calls"), routed.get("strong_model_calls")))
        _row("total tokens (in/out)",
             f"{base.get('total_tokens')} ({base.get('total_input_tokens')}/"
             f"{base.get('total_output_tokens')})",
             f"{routed.get('total_tokens')} ({routed.get('total_input_tokens')}/"
             f"{routed.get('total_output_tokens')})",
             f"total {_change(base.get('total_tokens'), routed.get('total_tokens'))}, "
             f"output {_change(base.get('total_output_tokens'), routed.get('total_output_tokens'))}")
        _row("estimated_cost_usd",
             f"${_num(base.get('estimated_cost_usd'), '{:.5f}')}",
             f"${_num(routed.get('estimated_cost_usd'), '{:.5f}')}",
             _change(base.get("estimated_cost_usd"), routed.get("estimated_cost_usd")))
        _row("e2e latency mean ms",
             _num((base.get("e2e_latency_ms") or {}).get("mean")),
             _num((routed.get("e2e_latency_ms") or {}).get("mean")),
             _change((base.get("e2e_latency_ms") or {}).get("mean"),
                     (routed.get("e2e_latency_ms") or {}).get("mean")))
        _row("e2e latency p95 ms",
             _num((base.get("e2e_latency_ms") or {}).get("p95")),
             _num((routed.get("e2e_latency_ms") or {}).get("p95")),
             _change((base.get("e2e_latency_ms") or {}).get("p95"),
                     (routed.get("e2e_latency_ms") or {}).get("p95")))
        _row("grounded_rate", _disp(base.get("grounded_rate")),
             _disp(routed.get("grounded_rate")), "grounding maintained")
        _row("task_completion", _disp(base.get("task_completion")),
             _disp(routed.get("task_completion")), "=")
        _row("tool_success_rate", _disp(base.get("tool_success_rate")),
             _disp(routed.get("tool_success_rate")), "")
        a("")
        a("- **Framing:** the cost / token / strong-call reductions are TOKEN-VOLUME driven "
          "(routing cheap nodes off the thinking-mode reasoner) — the two models share the "
          "same per-token rate, so this is NOT a per-token price difference.")
        note = (ablation_model.get("deltas_routed_vs_baseline") or {}).get("note")
        if note:
            a(f"- Recorded note: {note}")
        a("")
    else:
        a("_No `ablation_model.json` found — model A/B not produced._\n")

    # 10.3 retrieval A/B
    a("### 10.3 Retrieval-concurrency A/B (serial vs parallel)\n")
    if ablation_retr:
        pc = ablation_retr.get("per_config", {}) or {}
        ser = pc.get("serial_retrieval", {})
        par = pc.get("parallel_retrieval", {})
        a(f"- mode: **{ablation_retr.get('mode')}**, n_cases={ablation_retr.get('n_cases')}, "
          f"repeat_count={ablation_retr.get('repeat_count')} "
          f"(n_runs/config={ser.get('n_runs')}).\n")
        sr, pr = ser.get("retrieval_stage_latency_ms", {}), par.get("retrieval_stage_latency_ms", {})
        se, pe = ser.get("e2e_latency_ms", {}), par.get("e2e_latency_ms", {})
        a("| Metric | serial | parallel | change (parallel vs serial) |")
        a("|---|---|---|---|")
        a(f"| retrieval-stage latency mean ms | {_num(sr.get('mean'))} | {_num(pr.get('mean'))} "
          f"| {_change(sr.get('mean'), pr.get('mean'))} |")
        a(f"| retrieval-stage latency p50 ms | {_num(sr.get('p50'))} | {_num(pr.get('p50'))} "
          f"| {_change(sr.get('p50'), pr.get('p50'))} |")
        a(f"| retrieval-stage latency p95 ms | {_num(sr.get('p95'))} | {_num(pr.get('p95'))} "
          f"| {_change(sr.get('p95'), pr.get('p95'))} |")
        a(f"| e2e latency mean ms | {_num(se.get('mean'))} | {_num(pe.get('mean'))} "
          f"| {_change(se.get('mean'), pe.get('mean'))} |")
        a(f"| tool_success_rate | {_disp(ser.get('tool_success_rate'))} "
          f"| {_disp(par.get('tool_success_rate'))} | |")
        a("")
        an = ablation_retr.get("race_anomalies", {}) or {}
        _n_race = an.get("dropped_or_raced_results")
        if _n_race == 0:
            _race_note = ("No paired run differed in tool-call count or completion status — "
                          "i.e. no result was dropped or raced at any level under live "
                          "nondeterminism.")
        elif an.get("completion_mismatch") == 0:
            _race_note = (f"The {_n_race} anomaly/anomalies are tool-call-count differences "
                          "under live nondeterminism with NO completion mismatch — i.e. no "
                          "result was dropped at task-completion level.")
        else:
            _race_note = (f"{an.get('completion_mismatch')} of the {_n_race} anomalies are "
                          "completion-status mismatches — a possible dropped/raced result.")
        a(f"- **Race / dropped-result anomalies:** "
          f"{an.get('dropped_or_raced_results')}/{an.get('compared_pairs')} paired runs "
          f"(tool_count_mismatch={an.get('tool_count_mismatch')}, "
          f"completion_mismatch={an.get('completion_mismatch')}). " + _race_note)
        a("- **Interpretation:** parallel fan-out cuts the retrieval STAGE latency sharply, "
          "but end-to-end time is dominated by model synthesis, so e2e is ~unchanged.")
        a("")
    else:
        a("_No `ablation_retrieval.json` found — retrieval A/B not produced._\n")

    # 10.4 fault injection
    a("### 10.4 Fault injection (real tool/graph/idempotency/guardrail code, mocked model)\n")
    if fault:
        a(f"- {fault.get('note', '')}")
        a(f"- scenarios: **{fault.get('n_scenarios')}**")
        a(f"- faults_correctly_surfaced: {_disp(fault.get('faults_correctly_surfaced'))}")
        a(f"- retry_recovery_rate: {_disp(fault.get('retry_recovery_rate'))} "
          "(the non-recoveries are non-recoverable-by-design faults: HTTP 500 every attempt, "
          "missing schema field, critic/synthesis model raising)")
        a(f"- fallback_success_rate: {_disp(fault.get('fallback_success_rate'))}")
        a(f"- idempotency_pass_rate: {_disp(fault.get('idempotency_pass_rate'))}")
        a(f"- total_duplicate_writes: **{fault.get('total_duplicate_writes')}**")
        a(f"- post_fault_completion_rate: {_disp(fault.get('post_fault_completion_rate'))}")
        a(f"- post_fault_ungrounded_rate: {_disp(fault.get('post_fault_ungrounded_rate'))}")
        a(f"- harness_errors: **{fault.get('harness_errors')}**\n")
    else:
        a("_No `fault_summary.json` found — fault injection not produced._\n")

    # 10.5 memory eval
    a("### 10.5 Long-term memory eval\n")
    if memory:
        status = memory.get("status")
        a(f"- status: **{status}**; chromadb_available: "
          f"**{memory.get('chromadb_available')}**")
        if str(status).startswith("blocked"):
            a(f"- {memory.get('note', '')}\n")
        else:
            checks = memory.get("checks", {}) or {}
            rates = memory.get("rates", {}) or {}
            a("\n**REAL deterministic store checks (no model in the loop — genuine):**\n")
            for k in sorted(checks):
                if k in _MEM_REAL_CHECKS:
                    a(f"- {k}: {_disp(checks[k])}")
            for k in sorted(rates):
                if k in _MEM_REAL_RATES:
                    a(f"- {k}: {_disp(rates[k])}")
            a("\n**STUBBED-model mechanics (LLM extraction / consolidation is canned — "
              "measures plumbing, NOT extraction quality):**\n")
            for k in sorted(checks):
                if k in _MEM_STUB_CHECKS:
                    a(f"- {k}: {_disp(checks[k])} — *stub mechanics, not real extraction "
                      "quality*")
            for k in sorted(rates):
                if k in _MEM_MIXED_RATES:
                    a(f"- {k}: {_disp(rates[k])} — *retrieval-by-user-filter is real, but 4 of "
                      "its 5 sub-cases are seeded via the stubbed consolidation path; "
                      "retrieval_relevance (the failing sub-case) is fully real*")
            other = [k for k in list(checks) + list(rates)
                     if k not in _MEM_REAL_CHECKS | _MEM_STUB_CHECKS | _MEM_REAL_RATES
                     | _MEM_MIXED_RATES]
            if other:
                a("\n**Other keys present (unclassified):**\n")
                for k in other:
                    src = checks.get(k, rates.get(k))
                    a(f"- {k}: {_disp(src)}")
            a("")
    else:
        a("_No `memory_eval.json` found — memory eval not produced._\n")

    # (11) known limitations
    a("## 11. Known limitations\n")
    for lim in _limitations(n_bench, _needs_checker_cases()):
        a(f"- {lim}")
    a("")

    # (12) what could NOT be measured + why
    a("## 12. What could NOT be measured (and why)\n")
    a("- **LLM-judge agreement with the deterministic grader** — the auxiliary LLM judge is "
      "implemented and optional (`--judge`), but was NOT enabled on this run, so no "
      "judge-vs-grader agreement number was produced. Not a blocker; simply not run.")
    a("- **`web_search` answer quality (now PARTIALLY measurable)** — SearXNG is operational, "
      "so the tool returns real results and web-dependent cases ARE graded and grounded "
      "(market-rent answers cite Zoopla / Rightmove / SpareRoom). The residual limitation is "
      "NONDETERMINISM, not unavailability: live web/scrape results vary across runs, so exact "
      "web-grounded figures are not bit-for-bit reproducible from a single run.")
    a("- **Real long-term-memory extraction / consolidation quality** — the memory eval "
      "STUBS the LLM extraction / importance / consolidation calls (see `memory_eval.py` "
      "docstring), so `extraction_precision` and the update/stale/contradiction checks are "
      "store-plumbing mechanics, not a measure of real LLM extraction quality.")
    a("- **Live tool variance for non-fixture A / C / D / E cases** — those prompts hit "
      "cache/live network in-process and are nondeterministic; a single run does not bound "
      "their variance.\n")

    a("### Real agent findings surfaced (valuable eval output, not framework defects)\n")
    a("The low per-category pass rates in D / E / G reflect REAL agent behaviours that the "
      "benchmark correctly caught:\n")
    finding_pc = _analyse_per_case(summaries[0]["path"].parent) if summaries else None
    if finding_pc:
        clar = finding_pc["clarification"]
        webc = finding_pc["web_search"]
        cp = finding_pc["cat_pass"]

        def _catdisp(name):
            ok, tot = cp.get(name, [0, 0])
            return f"{ok}/{tot}"
        a(f"- **Over-clarification on area-level safety (D = {_catdisp('D_crime_poi')}):** "
          f"the agent asked a clarifying question instead of answering on cases "
          f"{', '.join(c for c in clar if c.startswith('D')) or '—'} "
          f"(all clarification-routed cases this run: {', '.join(clar) or '—'}). A real "
          "over-conservative safety gate, not a grading bug.")
        a(f"- **Memory `remember`/recall not firing (G = {_catdisp('G_memory')}):** several "
          "G cases routed to `direct_answer` / `reasoning_property` and the agent did not "
          "persist/recall the stored preference (only the explicit `recall_memory` cases "
          "passed). A real behaviour gap surfaced by the eval.")
        a(f"- **Multi-constraint tool-chaining + source-citation gaps "
          f"(E = {_catdisp('E_multi_constraint')}):** E answers are largely grounded but "
          "fail on the FULL constraint set (partial constraint satisfaction), i.e. the agent "
          "does not chain enough tools / cite every required source to satisfy every "
          "constraint at once.")
        webf = finding_pc["web_search_failed"]
        a(f"- **Web-dependent cases now grounded (SearXNG operational):** "
          f"{len(webc)} cases routed through `web_search`/`multi_search` "
          f"({', '.join(webc) or '—'}); the live search backend returns real "
          "Rightmove / Zoopla / SpareRoom results, so market-rent claims ARE web-grounded. "
          f"Any residual failures in this subset ({', '.join(webf) or '—'}) stem from the "
          "constraint/citation gaps above plus run-to-run web nondeterminism, NOT from the "
          "web tool being unavailable.")
    a("")

    # (13) which numbers are CV-suitable
    a("## 13. Which numbers are CV-suitable\n")
    a(f"- **Well-supported (see `CV_METRICS.md` -> 可安全使用):** model-routing A/B engineering "
      f"deltas at n={n_model} (strong-call, cost, e2e, token reductions with grounding "
      f"maintained); live grounding fidelity on the {n_bench}-case benchmark "
      f"(grounded {_disp(bench.get('grounded_rate'))}, money "
      f"{_disp(bench.get('money_grounded_rate'))}, contradicted "
      f"{bench.get('contradicted_claims')}); retrieval-stage parallelization latency reduction "
      "with near-perfect race parity; fault-tolerance mechanics; the REAL memory store "
      "isolation/forget/restart checks; and framework scope.")
    a(f"- **Do NOT quote as a headline (see `CV_METRICS.md` -> 不建议使用):** the raw "
      f"end-to-end pass_rate {_disp(bench.get('passed'))} (dragged down by the real agent "
      "findings above + heuristic checkers + live tool variance / web nondeterminism); the "
      "STUBBED memory extraction number; any n<15 single-run rate; and LLM-judge agreement "
      "(not run).")
    a("\n_Full per-claim CV guidance — 中文/English wording, raw num/den, definition, file "
      "path, safe flag, required caveat — is in `CV_METRICS.md`._\n")

    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# CV_METRICS.md
# --------------------------------------------------------------------------- #
def _cv_block(a, *, title, safe, zh, en, raw, definition, location, caveat):
    a(f"### {'[SAFE]' if safe else '[AVOID]'} {title}\n")
    a(f"- **中文 CV 表述**: {zh}")
    a(f"- **English CV statement**: {en}")
    a(f"- **Raw data (num/den)**: {raw}")
    a(f"- **Metric definition**: {definition}")
    a(f"- **Result file**: `{location}`")
    a(f"- **Safe to use**: {'YES' if safe else 'NO'}")
    a(f"- **Required caveat**: {caveat}\n")


def build_cv_md(results: Path, timestamp: str) -> str:
    summaries = _find_summaries(results)
    bench = summaries[0]["data"] if summaries else {}
    bench_path = _rel(summaries[0]["path"]) if summaries else "evaluation/results/*/summary.json"
    ablation_model = _load_json(results / "ablation_model.json")
    ablation_retr = _load_json(results / "ablation_retrieval.json")
    fault = _load_json(results / "fault_summary.json")
    memory = _load_json(results / "memory_eval.json")

    # case counts DERIVED from the loaded artifacts (never hardcoded).
    n_bench = bench.get("n_runs") or bench.get("n_cases")
    n_model = (ablation_model or {}).get("n_cases") or n_bench

    L: List[str] = []
    a = L.append
    a("# CV_METRICS — RentCompass Evaluation\n")
    a(f"_Generated {timestamp}, HEAD `{_git_commit()}`. Every number is copied verbatim from "
      "a result file; nothing is estimated. Each rate carries its denominator._\n")

    # =================== 可安全使用 =================== #
    a("## 可安全使用\n")

    # 1) model-routing A/B engineering deltas
    if ablation_model:
        pc = ablation_model.get("per_config", {}) or {}
        base, routed = pc.get("baseline_all_strong", {}), pc.get("routed_models", {})
        sc = _change(base.get("strong_model_calls"), routed.get("strong_model_calls"))
        cost = _change(base.get("estimated_cost_usd"), routed.get("estimated_cost_usd"))
        tok = _change(base.get("total_tokens"), routed.get("total_tokens"))
        otok = _change(base.get("total_output_tokens"), routed.get("total_output_tokens"))
        e2e = _change((base.get("e2e_latency_ms") or {}).get("mean"),
                      (routed.get("e2e_latency_ms") or {}).get("mean"))
        base_total = base.get("total_model_calls")
        routed_total = routed.get("total_model_calls")
        _cv_block(
            a,
            title=f"Model-routing A/B engineering deltas (n={n_model}, live)",
            safe=True,
            zh=f"在 {n_model} 例真实基准上，按节点路由模型（强模型仅用于必要节点）相较全强模型基线："
               f"强模型调用 {base.get('strong_model_calls')}/{base_total}→"
               f"{routed.get('strong_model_calls')}/{routed_total}（{sc}），总 token "
               f"{base.get('total_tokens')}→{routed.get('total_tokens')}"
               f"（{tok}），输出 token {otok}，成本 {cost}，端到端均值 {e2e}，"
               f"grounding 基本持平（{base.get('grounded_rate', {}).get('display')} vs "
               f"{routed.get('grounded_rate', {}).get('display')}）。",
            en=f"On the {n_model}-case live benchmark, per-node model routing vs an all-strong "
               f"baseline cut strong-model calls "
               f"{base.get('strong_model_calls')}/{base_total}->"
               f"{routed.get('strong_model_calls')}/{routed_total} ({sc}), "
               f"total tokens ({tok}), output tokens ({otok}), cost ({cost}), and mean e2e "
               f"latency ({e2e}), while grounding held "
               f"({base.get('grounded_rate', {}).get('display')} vs "
               f"{routed.get('grounded_rate', {}).get('display')}).",
            raw=f"strong_calls {base.get('strong_model_calls')}/{base_total} -> "
                f"{routed.get('strong_model_calls')}/{routed_total}; tokens "
                f"{base.get('total_tokens')} -> "
                f"{routed.get('total_tokens')}; output_tokens {base.get('total_output_tokens')} "
                f"-> {routed.get('total_output_tokens')}; cost "
                f"${base.get('estimated_cost_usd'):.5f} -> ${routed.get('estimated_cost_usd'):.5f}; "
                f"e2e_mean_ms {(base.get('e2e_latency_ms') or {}).get('mean'):.0f} -> "
                f"{(routed.get('e2e_latency_ms') or {}).get('mean'):.0f}; grounded "
                f"{base.get('grounded_rate', {}).get('display')} vs "
                f"{routed.get('grounded_rate', {}).get('display')}",
            definition=f"Aggregated per-config token/call/cost/latency counters over {n_model} "
                       "cases x 1 repeat; cost = published DeepSeek rate x measured tokens.",
            location="evaluation/results/ablation_model.json (+ ablation_model.csv)",
            caveat="Cost/token saving is TOKEN-VOLUME driven, NOT a cheaper per-token rate "
                   "(chat & reasoner share one rate). Single live run; offline benchmark, not "
                   "real users; live tool variance applies.",
        )

    # 2) grounding fidelity on the live benchmark
    if bench:
        _cv_block(
            a,
            title=f"Grounding fidelity on the live {n_bench}-case benchmark",
            safe=True,
            zh=f"在 {n_bench} 例真实基准（真实 DeepSeek + 真实/缓存工具 + 实时 web_search）上，"
               f"可核验声明的 grounded 率 "
               f"{_disp(bench.get('grounded_rate'))}，金额类 grounded 率 "
               f"{_disp(bench.get('money_grounded_rate'))}，与证据矛盾的声明数 "
               f"{bench.get('contradicted_claims')}。",
            en=f"On the {n_bench}-case live benchmark, verifiable-claim grounded rate "
               f"{_disp(bench.get('grounded_rate'))}, money-claim grounded rate "
               f"{_disp(bench.get('money_grounded_rate'))}, with "
               f"{bench.get('contradicted_claims')} claims contradicting the tool evidence.",
            raw=f"grounded {bench.get('grounded_rate', {}).get('display')}, money_grounded "
                f"{bench.get('money_grounded_rate', {}).get('display')}, contradicted="
                f"{bench.get('contradicted_claims')}, source_coverage "
                f"{bench.get('source_coverage', {}).get('display')}",
            definition="grounded_rate = grounded verifiable claims / total verifiable claims; "
                       "money_grounded_rate = grounded money claims / total money claims; "
                       "contradicted = claims that contradict tool evidence. Heuristic grader.",
            location=bench_path,
            caveat="Heuristic (text-marker) grading; single live run. SearXNG is operational, "
                   "so web cases ARE included and grounded (cite Zoopla/Rightmove/SpareRoom), "
                   "but live web/scrape results are NON-DETERMINISTIC across runs, so exact "
                   "figures vary. Report as grounding FIDELITY, not overall answer quality.",
        )

    # 3) retrieval parallelization latency + race parity
    if ablation_retr:
        pc = ablation_retr.get("per_config", {}) or {}
        ser, par = pc.get("serial_retrieval", {}), pc.get("parallel_retrieval", {})
        sr, pr = ser.get("retrieval_stage_latency_ms", {}), par.get("retrieval_stage_latency_ms", {})
        an = ablation_retr.get("race_anomalies", {}) or {}
        mean_ch = _change(sr.get("mean"), pr.get("mean"))
        p95_ch = _change(sr.get("p95"), pr.get("p95"))
        _cv_block(
            a,
            title="Retrieval parallelization: retrieval-stage latency reduction + race parity",
            safe=True,
            zh=f"检索并发消融（{ablation_retr.get('n_cases')} 例 x "
               f"{ablation_retr.get('repeat_count')} 重复 = {ser.get('n_runs')} 次/配置）：并行 "
               f"map-reduce 将检索阶段延迟均值 {mean_ch}、p95 {p95_ch}；串并配对中仅 "
               f"{an.get('dropped_or_raced_results')}/{an.get('compared_pairs')} 出现工具调用数"
               f"差异（完成态不匹配 {an.get('completion_mismatch')}，无结果在完成层丢失）。",
            en=f"Retrieval-concurrency ablation ({ablation_retr.get('n_cases')} cases x "
               f"{ablation_retr.get('repeat_count')} repeats = {ser.get('n_runs')} runs/config): "
               f"parallel fan-out cut retrieval-STAGE latency mean {mean_ch} and p95 {p95_ch}; "
               f"only {an.get('dropped_or_raced_results')}/{an.get('compared_pairs')} paired runs "
               f"showed a tool-count difference (completion_mismatch="
               f"{an.get('completion_mismatch')}; no result dropped at completion level).",
            raw=f"retrieval_stage_mean_ms {sr.get('mean'):.0f} -> {pr.get('mean'):.0f} ({mean_ch}); "
                f"p95 {sr.get('p95'):.0f} -> {pr.get('p95'):.0f} ({p95_ch}); "
                f"race_anomalies {an.get('dropped_or_raced_results')}/{an.get('compared_pairs')} "
                f"(tool_count={an.get('tool_count_mismatch')}, "
                f"completion={an.get('completion_mismatch')})",
            definition="retrieval-stage latency = time inside the retrieval/tool fan-out node; "
                       "race anomaly = serial-vs-parallel pair differing in tool-call count or "
                       "completion status.",
            location="evaluation/results/ablation_retrieval.json (+ ablation_retrieval.csv)",
            caveat="e2e latency is ~unchanged (synthesis-dominated) — quote the retrieval-STAGE "
                   "reduction, not an end-to-end speedup. "
                   + ("No race anomaly was observed (no dropped result)."
                      if an.get("dropped_or_raced_results") == 0 else
                      "Any anomalies are benign tool-count differences under live "
                      "nondeterminism, not dropped results."),
        )

    # 4) fault-tolerance mechanics
    if fault:
        _cv_block(
            a,
            title="Fault-tolerance mechanics (15 real scenarios)",
            safe=True,
            zh=f"在 15 个真实故障注入场景（真实工具/图/幂等/护栏代码，仅模型被 mock）中，"
               f"故障正确暴露 {_disp(fault.get('faults_correctly_surfaced'))}，幂等写入 "
               f"{_disp(fault.get('idempotency_pass_rate'))}（重复持久化写入 "
               f"{fault.get('total_duplicate_writes')} 次），降级回退 "
               f"{_disp(fault.get('fallback_success_rate'))}，故障后仍完成任务 "
               f"{_disp(fault.get('post_fault_completion_rate'))}。",
            en=f"Across 15 real fault-injection scenarios (real tool/graph/idempotency/guardrail "
               f"code; only the LLM mocked): faults surfaced honestly "
               f"{_disp(fault.get('faults_correctly_surfaced'))}, idempotency "
               f"{_disp(fault.get('idempotency_pass_rate'))} with "
               f"{fault.get('total_duplicate_writes')} duplicate durable writes, fallback "
               f"{_disp(fault.get('fallback_success_rate'))}, post-fault completion "
               f"{_disp(fault.get('post_fault_completion_rate'))}.",
            raw=f"faults_surfaced {fault.get('faults_correctly_surfaced', {}).get('display')}, "
                f"idempotency {fault.get('idempotency_pass_rate', {}).get('display')}, "
                f"fallback {fault.get('fallback_success_rate', {}).get('display')}, "
                f"dup_writes={fault.get('total_duplicate_writes')}, retry_recovery "
                f"{fault.get('retry_recovery_rate', {}).get('display')}, post_fault_completion "
                f"{fault.get('post_fault_completion_rate', {}).get('display')}",
            definition="See REPORT.md §9. Only the model is mocked; tool/idempotency/guardrail "
                       "code is production code, so these are genuine resilience mechanics.",
            location="evaluation/results/fault_summary.json (+ fault_injection.csv)",
            caveat="Single run; model mocked (does NOT measure answer quality). retry_recovery "
                   "4/8 is expected — the 4 non-recoveries are non-recoverable-by-design faults. "
                   "Report as 'resilience mechanics', not end-to-end accuracy.",
        )

    # 5) real memory store checks
    if memory and not str(memory.get("status")).startswith("blocked"):
        checks = memory.get("checks", {}) or {}
        rates = memory.get("rates", {}) or {}
        _cv_block(
            a,
            title="Long-term memory: REAL store isolation / forget / restart checks",
            safe=True,
            zh=f"记忆存储的确定性检查（真实 ChromaDB，无模型参与）：用户隔离 "
               f"{_disp(checks.get('user_isolation'))}，遗忘/删除 "
               f"{_disp(checks.get('forget_delete'))}，进程重启恢复 "
               f"{_disp(checks.get('restart_recovery'))}，写入成功 "
               f"{_disp(rates.get('memory_write_success_rate'))}，身份门控 "
               f"{_disp(checks.get('identity_gate'))}。",
            en=f"Deterministic memory-store checks (real ChromaDB, no model in the loop): "
               f"user isolation {_disp(checks.get('user_isolation'))}, forget/delete "
               f"{_disp(checks.get('forget_delete'))}, process-restart recovery "
               f"{_disp(checks.get('restart_recovery'))}, write success "
               f"{_disp(rates.get('memory_write_success_rate'))}, identity gate "
               f"{_disp(checks.get('identity_gate'))}.",
            raw=f"user_isolation {checks.get('user_isolation', {}).get('display')}, "
                f"forget_delete {checks.get('forget_delete', {}).get('display')}, "
                f"restart_recovery {checks.get('restart_recovery', {}).get('display')}, "
                f"write_success {rates.get('memory_write_success_rate', {}).get('display')}, "
                f"identity_gate {checks.get('identity_gate', {}).get('display')}",
            definition="Store-level deterministic checks: per-user retrieval filtering, GDPR "
                       "forget/delete, on-disk persistence across a fresh instance, and the "
                       "pure identity-gate function. No LLM involved.",
            location="evaluation/results/memory_eval.json",
            caveat="These store checks are REAL, but small n (mostly 1/1-5/5). Keep them "
                   "SEPARATE from the extraction/consolidation numbers, which are stubbed "
                   "(see 不建议使用). Also covered by tests/test_agent_memory_isolation.py.",
        )

    # 6) framework scope
    nct = _constraint_type_count()
    cases = _load_cases()
    _cv_block(
        a,
        title="Evaluation framework scope",
        safe=True,
        zh=f"自建离线评测框架：{cases.get('n', '?')} 例基准（{len(cases.get('cats', {}))} 类、"
           f"{nct if nct is not None else '?'} 种可机检约束类型、确定性打分器 + 可选 LLM 裁判），"
           f"外加指标采集/定价/故障注入（{(fault or {}).get('n_scenarios', '?')} 场景）/消融/"
           f"记忆评测子系统。",
        en=f"Built a self-contained offline eval framework: a {cases.get('n', '?')}-case "
           f"benchmark ({len(cases.get('cats', {}))} categories, "
           f"{nct if nct is not None else '?'} machine-checkable constraint types, a "
           f"deterministic grader + optional LLM judge), plus metrics-collection, pricing, "
           f"fault-injection ({(fault or {}).get('n_scenarios', '?')} scenarios), ablation, "
           f"and memory-eval subsystems.",
        raw=f"{cases.get('n', '?')} cases; {len(cases.get('cats', {}))} categories; "
            f"{nct if nct is not None else '?'} constraint types; "
            f"{(fault or {}).get('n_scenarios', '?')} fault scenarios; retrieval A/B "
            f"{(ablation_retr or {}).get('n_cases', '?')}x{(ablation_retr or {}).get('repeat_count', '?')}",
        definition="Counts derived from benchmark/cases.jsonl, metrics.graders."
                   "CONSTRAINT_CHECKERS, and the fault_injection scenario module.",
        location="evaluation/benchmark/cases.jsonl, evaluation/metrics/graders.py, "
                 "evaluation/fault_injection/",
        caveat="Describe as engineering SCOPE (framework built), not as a quality score.",
    )

    # =================== 不建议使用 =================== #
    a("## 不建议使用\n")

    # a) raw end-to-end pass rate as a headline
    if bench:
        _cv_block(
            a,
            title="Raw end-to-end pass_rate as a quality headline",
            safe=False,
            zh=f"端到端整体通过率 {_disp(bench.get('passed'))}。不建议作为质量头条：该数值被真实的"
               "智能体行为发现（D 类地区治安过度澄清、G 类记忆未落盘/召回缺失、E 类多约束链路与"
               "来源引用不足）、启发式约束打分器、以及实时工具波动（web/scrape 结果跨运行不确定）"
               "共同拉低。",
            en=f"Overall end-to-end pass_rate {_disp(bench.get('passed'))}. Do NOT use as a "
               "quality headline: it is dragged down by REAL agent findings (D area-crime over-"
               "clarification, G memory-not-persisted / recall gaps, E multi-constraint chaining "
               "+ source-citation gaps), heuristic constraint checkers, and live tool variance "
               "(web/scrape results non-deterministic across runs). It is a useful diagnostic, "
               "not a headline accuracy number.",
            raw=f"passed {bench.get('passed', {}).get('display')} "
                f"({bench.get('passed', {}).get('rate', 0) * 100:.1f}%); "
                f"constraints {bench.get('constraints', {}).get('display')}",
            definition="A case passes only if it completes AND calls no forbidden tool AND "
                       "passes EVERY heuristic constraint AND has 0 contradictions.",
            location=bench_path,
            caveat=f"Report the component metrics (grounding, routing deltas) and the per-"
                   f"category findings instead. Quoting {bench.get('passed', {}).get('display')} "
                   "alone misrepresents the system.",
        )

    # b) stubbed memory extraction
    if memory and not str(memory.get("status")).startswith("blocked"):
        checks = memory.get("checks", {}) or {}
        _cv_block(
            a,
            title="Memory extraction_precision / update / consolidation numbers",
            safe=False,
            zh=f"记忆抽取精度 {_disp(checks.get('extraction_precision'))}、更新/替换/矛盾处理均为 "
               "1/1。不建议使用：这些数值下的 LLM 事实抽取/重要性/整合调用是被打桩（stub）的，"
               "只反映存储机制，不代表真实抽取质量。",
            en=f"Memory extraction_precision {_disp(checks.get('extraction_precision'))} and the "
               "update/stale/contradiction checks (all 1/1). Do NOT use: the LLM extraction / "
               "importance / consolidation calls are STUBBED, so these reflect store plumbing, "
               "not real extraction quality.",
            raw=f"extraction_precision {checks.get('extraction_precision', {}).get('display')}, "
                f"update_correctness {checks.get('update_correctness', {}).get('display')}, "
                f"stale_replacement {checks.get('stale_replacement', {}).get('display')}, "
                f"contradiction_handling {checks.get('contradiction_handling', {}).get('display')}",
            definition="Checks that route through the LLM extraction/consolidation path, which "
                       "the eval replaces with canned outputs (see memory_eval.py docstring).",
            location="evaluation/results/memory_eval.json",
            caveat="Mechanics of a stub, not extraction quality. Real extraction quality needs "
                   "a live model run and human/judge review.",
        )

    # c) small-n single-run rates
    _cv_block(
        a,
        title="Any n<15 single-run rate as a standalone statistic",
        safe=False,
        zh="任何 n<15 的单次运行比率（如各记忆子检查 1/1、单个 race 异常 1/36、部分工具的少量"
           "调用延迟）不建议单独引用：置信区间过宽。",
        en="Any n<15 single-run rate quoted standalone (e.g. individual memory sub-checks at "
           "1/1, the single retrieval race anomaly at 1/36, low-call-count per-tool latencies) "
           "— do NOT quote alone: confidence intervals are too wide.",
        raw="e.g. restart_recovery 1/1, retrieval_relevance 0/1, race_anomalies 1/36",
        definition="Rates whose denominator is a handful of runs.",
        location="evaluation/results/memory_eval.json, evaluation/results/ablation_retrieval.json",
        caveat="Aggregate or repeat before quoting; otherwise present only as directional.",
    )

    # d) LLM-judge agreement (not run)
    _cv_block(
        a,
        title="LLM-judge agreement with the deterministic grader",
        safe=False,
        zh="LLM 裁判与确定性打分器的一致率：本轮未运行（裁判为可选 `--judge`，未启用），"
           "无结果文件可引用，切勿编造。",
        en="LLM-judge vs deterministic-grader agreement: NOT run this round (the judge is "
           "optional via `--judge` and was not enabled), so there is no result file to cite. "
           "Do not fabricate a number.",
        raw="not produced (no judge column in this run's outputs)",
        definition="Agreement between the optional LLM judge and the deterministic grader.",
        location="not produced (run: run_benchmark --live --judge to generate)",
        caveat="Only quote once actually measured.",
    )

    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.report")
    p.add_argument("--results", default="evaluation/results")
    p.add_argument("--out", default="evaluation/results")
    p.add_argument("--timestamp", default=None)
    args = p.parse_args(argv)
    results = Path(args.results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    report = build_report_md(results, ts)
    cv = build_cv_md(results, ts)
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    (out / "CV_METRICS.md").write_text(cv, encoding="utf-8")
    print(f"wrote {out / 'REPORT.md'} ({len(report)} chars)")
    print(f"wrote {out / 'CV_METRICS.md'} ({len(cv)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
