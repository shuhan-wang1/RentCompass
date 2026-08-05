"""Render the report's metric tables straight from the analysis JSON.

Every number in EVAL_REPORT_20260804.md that comes from a study is produced here, so no
figure is transcribed by hand and each table carries the file it was read from.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _f(v, nd=1, suffix=""):
    if v is None:
        return "INCOMPLETE"
    if isinstance(v, float):
        return f"{v:,.{nd}f}{suffix}"
    return f"{v:,}{suffix}"


def _pct(v, nd=1):
    return "INCOMPLETE" if v is None else f"{v*100:.{nd}f}%"


def _ci(b, nd=1, scale=1.0, suffix=""):
    if b is None or b.get("ci_low") is None:
        return "INCOMPLETE"
    return (f"{b['point']*scale:,.{nd}f}{suffix} "
            f"[{b['ci_low']*scale:,.{nd}f}{suffix}, {b['ci_high']*scale:,.{nd}f}{suffix}]")


def _verdict(b):
    if b is None or b.get("crosses_zero") is None:
        return "INCOMPLETE"
    return "未观察到显著差异 (CI 跨 0)" if b["crosses_zero"] else "CI 不跨 0"


def render_study(path: Path, title: str, base_label: str, test_label: str) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    base, test = d["arm_base"], d["arm_test"]
    a, b = d["per_arm"][base], d["per_arm"][test]
    boots = d["bootstrap_test_minus_base"]
    src = path.as_posix()
    L = []
    L.append(f"#### {title}")
    L.append("")
    L.append(f"来源文件：`{src}`（由 `evaluation/results/_harness/analyze.py` 生成，"
             f"输入 runs.jsonl 见该文件 `source_files` 字段）")
    L.append("")
    L.append(f"运行总数 {d['runs_total']}，成功 {d['runs_ok']}，失败 {d['runs_failed']}"
             f"（失败分布 {d['failures_by_arm']}，原因 {d['failure_reasons'] or '无'}）；"
             f"配对上的 case 数 {d['cases_paired']} / 出现过的 case 数 {d['cases_seen']}。")
    L.append("")
    L.append(f"| 指标 | {base_label} (对照) | {test_label} (实验) | 差值 test−base，bootstrap 95% CI | 判读 |")
    L.append("|---|---|---|---|---|")

    rows = [
        ("LLM 调用总数", _f(a["llm_calls_total"], 0), _f(b["llm_calls_total"], 0),
         _ci(boots.get("llm_calls_pct_change"), 1, 100.0, "%"), _verdict(boots.get("llm_calls_pct_change"))),
        ("其中 thinking 模式调用", _f(a["thinking_calls_total"], 0), _f(b["thinking_calls_total"], 0), "—", "—"),
        ("其中 deepseek-v4-pro 调用", _f(a["pro_calls_total"], 0), _f(b["pro_calls_total"], 0), "—", "—"),
        ("绕过 ModelRouter 的调用 (purpose=memory)", _f(a["unrouted_calls_total"], 0),
         _f(b["unrouted_calls_total"], 0), "—", "不受本 A/B 的路由改动影响，见正文"),
        ("输入 token", _f(a["tokens_in_total"], 0), _f(b["tokens_in_total"], 0), "—", "—"),
        ("输出 token", _f(a["tokens_out_total"], 0), _f(b["tokens_out_total"], 0),
         _ci(boots.get("tokens_out_pct_change"), 1, 100.0, "%"), _verdict(boots.get("tokens_out_pct_change"))),
        ("总 token", _f(a["tokens_total"], 0), _f(b["tokens_total"], 0),
         _ci(boots.get("tokens_total_pct_change"), 1, 100.0, "%"), _verdict(boots.get("tokens_total_pct_change"))),
        ("缓存命中 token (provider 侧)", _f(a["cached_tokens_total"], 0), _f(b["cached_tokens_total"], 0), "—", "—"),
        ("成本 USD（固定价表）", _f(a["cost_usd_total"], 4), _f(b["cost_usd_total"], 4),
         _ci(boots.get("cost_pct_change"), 1, 100.0, "%"), _verdict(boots.get("cost_pct_change"))),
        ("端到端 mean (ms)", _f(a["e2e_ms"]["mean"], 0), _f(b["e2e_ms"]["mean"], 0),
         _ci(boots.get("e2e_mean_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("e2e_mean_ms_diff"))),
        ("端到端 p50 (ms)", _f(a["e2e_ms"]["p50"], 0), _f(b["e2e_ms"]["p50"], 0),
         _ci(boots.get("e2e_p50_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("e2e_p50_ms_diff"))),
        ("端到端 p95 (ms)", _f(a["e2e_ms"]["p95"], 0), _f(b["e2e_ms"]["p95"], 0),
         _ci(boots.get("e2e_p95_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("e2e_p95_ms_diff"))),
        ("检索阶段 mean (ms)", _f(a["retrieval_stage_ms"]["mean"], 0), _f(b["retrieval_stage_ms"]["mean"], 0),
         _ci(boots.get("retrieval_mean_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("retrieval_mean_ms_diff"))),
        ("检索阶段 p50 (ms)", _f(a["retrieval_stage_ms"]["p50"], 0), _f(b["retrieval_stage_ms"]["p50"], 0),
         _ci(boots.get("retrieval_p50_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("retrieval_p50_ms_diff"))),
        ("检索阶段 p95 (ms)", _f(a["retrieval_stage_ms"]["p95"], 0), _f(b["retrieval_stage_ms"]["p95"], 0),
         _ci(boots.get("retrieval_p95_ms_diff"), 0, 1.0, " ms"), _verdict(boots.get("retrieval_p95_ms_diff"))),
        (f"检索阶段 mean（仅真正执行了工具批次的 run，n={a['retrieval_stage_ms_toolruns_only']['n']} / "
         f"{b['retrieval_stage_ms_toolruns_only']['n']}）",
         _f(a["retrieval_stage_ms_toolruns_only"]["mean"], 0),
         _f(b["retrieval_stage_ms_toolruns_only"]["mean"], 0), "—", "见正文"),
        ("检索阶段 p50（同上子集）", _f(a["retrieval_stage_ms_toolruns_only"]["p50"], 0),
         _f(b["retrieval_stage_ms_toolruns_only"]["p50"], 0), "—", "见正文"),
        ("检索阶段 p95（同上子集）", _f(a["retrieval_stage_ms_toolruns_only"]["p95"], 0),
         _f(b["retrieval_stage_ms_toolruns_only"]["p95"], 0), "—", "见正文"),
        ("证据支撑率 grounded/verifiable",
         f"{a['grounded_rate']['num']}/{a['grounded_rate']['den']} ({_pct(a['grounded_rate']['rate'])})",
         f"{b['grounded_rate']['num']}/{b['grounded_rate']['den']} ({_pct(b['grounded_rate']['rate'])})",
         _ci(boots.get("grounded_rate_diff"), 2, 100.0, " pp"), _verdict(boots.get("grounded_rate_diff"))),
        ("金额支撑率 money_grounded/money_total",
         f"{a['money_grounded_rate']['num']}/{a['money_grounded_rate']['den']} ({_pct(a['money_grounded_rate']['rate'])})",
         f"{b['money_grounded_rate']['num']}/{b['money_grounded_rate']['den']} ({_pct(b['money_grounded_rate']['rate'])})",
         _ci(boots.get("money_grounded_rate_diff"), 2, 100.0, " pp"), _verdict(boots.get("money_grounded_rate_diff"))),
        ("与证据矛盾的主张数 (contradicted)", _f(a["contradicted_total"], 0), _f(b["contradicted_total"], 0), "—", "—"),
        ("任务完成 task_completed",
         f"{a['task_completed']['num']}/{a['task_completed']['den']}",
         f"{b['task_completed']['num']}/{b['task_completed']['den']}",
         _ci(boots.get("task_completed_rate_diff"), 2, 100.0, " pp"), _verdict(boots.get("task_completed_rate_diff"))),
        ("约束全通过 passed",
         f"{a['constraint_pass']['num']}/{a['constraint_pass']['den']}",
         f"{b['constraint_pass']['num']}/{b['constraint_pass']['den']}",
         _ci(boots.get("constraint_pass_rate_diff"), 2, 100.0, " pp"), _verdict(boots.get("constraint_pass_rate_diff"))),
        ("执行的工具调用数 / run（均值差）",
         _f(a["tool_calls_total"] / a["n_runs"] if a["n_runs"] else None, 2),
         _f(b["tool_calls_total"] / b["n_runs"] if b["n_runs"] else None, 2),
         _ci(boots.get("n_tools_executed_diff"), 2), _verdict(boots.get("n_tools_executed_diff"))),
        ("监听缓存命中率 (listing cache)",
         f"{a['cache_hit_rate']['num']}/{a['cache_hit_rate']['den']} ({_pct(a['cache_hit_rate']['rate'])})",
         f"{b['cache_hit_rate']['num']}/{b['cache_hit_rate']['den']} ({_pct(b['cache_hit_rate']['rate'])})",
         "—", "—"),
        ("外部工具失败率",
         f"{a['tool_failure_rate']['num']}/{a['tool_failure_rate']['den']} ({_pct(a['tool_failure_rate']['rate'])})",
         f"{b['tool_failure_rate']['num']}/{b['tool_failure_rate']['den']} ({_pct(b['tool_failure_rate']['rate'])})",
         "—", "—"),
    ]
    for r in rows:
        L.append("| " + " | ".join(r) + " |")
    L.append("")
    L.append(f"bootstrap：cluster bootstrap（重采样单位 = case），"
             f"重采样次数 {boots.get('e2e_mean_ms_diff', {}).get('n_boot', 'n/a')}，"
             f"seed {boots.get('e2e_mean_ms_diff', {}).get('seed', 'n/a')}，"
             f"配对 case 数 {boots.get('e2e_mean_ms_diff', {}).get('n_cases', 'n/a')}。")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--study", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--base-label", required=True)
    p.add_argument("--test-label", required=True)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    text = render_study(Path(a.study), a.title, a.base_label, a.test_label)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
