"""held-out 集 preflight 门禁 —— 在发起任何模型请求之前运行。

存在的理由（owner ruling 2026-08-05）：第三次设计里那条 44/50 (88%) 的静态-实判一致率
**只能作为规划验证，不能当保证**。要让「每项 ≥30 可判」成为设计保证而不是对旧样本比例的外推，
held-out 出题完成后必须先过这道门禁，**未通过的题替换掉，不允许在跑完之后用 N/A 消化**。

本脚本负责门禁的**静态**一半：

  Q  配额        每个分层的题数是否达标（默认 §2.13 的 110 例配额）
  H  硬约束      hard 配额题是否都带**明确、可验证、互不矛盾**的硬约束
  C  完成态      每题是否写明了该任务的正确完成态
  E  可评据      每条可评主张是否都有用户上下文 / 允许的派生计算 / 冻结的工具证据
  M  元数据      held-out 出题要求的每题必备字段（schema_version / task_category /
                 hard_constraint_slots / allowed_evidence_sources / 非重复说明 / 唯一 id）
  Q3 词表审计    任何未归类的 constraint type 直接判失败
  Q4 满足性配额  每个语义槽位的**满足性**覆盖下限（不收 heuristic / unknown / 无 listing）
  Q5 行为配额    「正确处理无结果/未知」的行为覆盖下限，单独卡、单独报

静态检查**不能**替代人工抽查：它能查「有没有写」和「写得能不能机器核验」，
查不了「写得对不对」。所以脚本同时生成一份人工抽查清单（--checklist）。

用法：
    python evaluation/results/_harness/holdout_preflight.py --cases <holdout.jsonl> \
        --out <report.json> --checklist <checklist.md> [--schema v2] [--fixtures DIR]
退出码 0 = 全部通过；1 = 有题未通过（必须替换后重跑本门禁）。

--------------------------------------------------------------------------------------
2026-08-05 的两处规则修正（**在出题之前、发出任何模型请求之前**做的，记录在 PROGRESS.log）
--------------------------------------------------------------------------------------
1. ``--schema v2`` 起用 ``constraint_schema_v2``：七个语义槽位各有独立类型，
   ``bedroom_count_match`` 从重载的 ``room_type_match`` 拆出，新增 ``area_match`` /
   ``move_in_date_satisfied`` / ``property_feature_present``。
2. **N1 的适用范围收窄到检索类**。v1 的 N1 对「fixture 里没有 listing」的题一律要求
   `correct_completion` 出现「诚实说明无匹配」的措辞——那会误伤纯计算 / 记忆 / 澄清类，
   它们本来就不该有 listing，正确完成态也不是「说没找到房子」。N1 的立意是「不能把
   *没有候选* 说成 *候选满足了约束*」，这只对检索类成立。这是**规则缺陷修正**，
   不是为了让某道题通过——修正时还没有任何一道 held-out 题存在。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---- §2.13 的建议配额（可用 --quota 覆盖） -------------------------------- #
DEFAULT_QUOTA = {
    "retrieval_hard": 35,     # retrieval 且带显式硬约束
    "retrieval_soft": 20,     # retrieval 且不带硬约束
    "calculation": 20,
    "memory": 20,
    "clarify": 15,
}

MEMORY_TOOLS = {"recall_memory", "remember"}

# 硬约束枚举来自**单一冻结定义** —— 配额、preflight、judge prompt 三处同源。
# 2026-08-05 之前这里内联了一份自己的集合，其中 6 个类型名在 cases.jsonl 里根本不存在，
# 同时漏掉了真实存在的 commute_leq_minutes。不再内联。
sys.path.insert(0, str(Path(__file__).resolve().parent))

_SCHEMA_ARG = "v2"
for _i, _a in enumerate(sys.argv):
    if _a == "--schema" and _i + 1 < len(sys.argv):
        _SCHEMA_ARG = sys.argv[_i + 1]
    elif _a.startswith("--schema="):
        _SCHEMA_ARG = _a.split("=", 1)[1]

if _SCHEMA_ARG == "v1":
    import hard_constraints as SCHEMA          # noqa: E402  (the frozen v1 record)
else:
    import constraint_schema_v2 as SCHEMA      # noqa: E402

MACHINE_CHECKABLE = SCHEMA.MACHINE_CHECKABLE
MISSING_TYPES = SCHEMA.MISSING_TYPES
REQUIRED_ARGS = SCHEMA.REQUIRED_ARGS
EXCLUDED_INSTRUMENT_TYPES = SCHEMA.EXCLUDED_INSTRUMENT_TYPES
audit_vocabulary = SCHEMA.audit_vocabulary
user_hard_constraints = SCHEMA.user_hard_constraints
slot_coverage = SCHEMA.slot_coverage
slot_of = SCHEMA.slot_of
SLOT_MIN_COVERAGE = SCHEMA.SLOT_MIN_COVERAGE
BEHAVIOR_MIN_COVERAGE = SCHEMA.BEHAVIOR_MIN_COVERAGE
SLOT_EVIDENCE_FIELD = SCHEMA.SLOT_EVIDENCE_FIELD
constraint_is_satisfaction_verifiable = SCHEMA.constraint_is_satisfaction_verifiable
_fixture_listings = SCHEMA._fixture_listings
SCHEMA_VERSION = getattr(SCHEMA, "SCHEMA_VERSION", "rentcompass/hard_constraints/v1")

HARD_CONSTRAINT_FIELDS = REQUIRED_ARGS
# 同一 field 上的方向，用于查互相矛盾（v1 路径保留；v2 走 SCHEMA.contradictions）
_DIRECTION = {"<=": "upper", "<": "upper", ">=": "lower", ">": "lower", "==": "exact"}

# 声明「正确完成态」的字段名（held-out 出题时新增；任一存在即可）
COMPLETION_FIELDS = ("correct_completion", "expected_behaviour", "expected_behavior")

# M：held-out 每题必备的元数据字段（任务书 §二）
REQUIRED_METADATA = ("case_id", "task_category", "schema_version", "hard_constraint_slots",
                     "allowed_evidence_sources", "failure_conditions", "novelty_note")
TASK_CATEGORIES = ("retrieval_hard", "retrieval_soft", "calculation", "memory", "clarify")


def task_class(case: dict) -> str:
    """与 blind_review_v2.task_class 完全一致的冻结规则（此处重述以便独立运行）。

    held-out 题自带 ``task_category``（含 retrieval_hard / retrieval_soft 的区分），
    此时直接用它——分层是出题时冻结的，不该由启发式反推。"""
    tc = case.get("task_category")
    if tc in TASK_CATEGORIES:
        return "retrieval" if tc.startswith("retrieval") else tc
    et = set(case.get("expected_tools") or [])
    if case.get("expected_route") == "clarification":
        return "clarify"
    if case.get("category") == "G_memory" or (et and et <= MEMORY_TOOLS):
        return "memory"
    if not et and (case.get("reference_calculations") or case.get("category") == "B_money"):
        return "calculation"
    return "retrieval"


def hard_constraints(case: dict) -> List[dict]:
    return user_hard_constraints(case)


def stratum(case: dict) -> str:
    tc = case.get("task_category")
    if tc in TASK_CATEGORIES:
        return tc
    k = task_class(case)
    if k != "retrieval":
        return k
    return "retrieval_hard" if hard_constraints(case) else "retrieval_soft"


# --------------------------------------------------------------------------- #
def check_hard(case: dict) -> List[str]:
    """H：明确 + 可验证 + 互不矛盾。"""
    problems: List[str] = []
    hcs = hard_constraints(case)
    if not hcs:
        return ["H1 该题被计入 hard 配额，但没有任何可机器核验的硬约束"]
    for c in hcs:
        need = HARD_CONSTRAINT_FIELDS[c["type"]]
        missing = [f for f in need if c.get(f) in (None, "")]
        if missing:
            problems.append(f"H2 硬约束 {c['type']} 缺少参数 {missing} -> 不可验证")

    if SCHEMA_VERSION.endswith("/v2"):
        for c in hcs:
            problems += SCHEMA.arg_domain_problems(c)      # H2b 参数值须在冻结词表内
        problems += SCHEMA.explicitness_problems(case)     # H6 逐条 user_text 复现规范化
        problems += SCHEMA.contradictions(hcs)             # H4 按语义槽位查矛盾
        return problems

    # ---- v1 路径（保留，用于复现 2026-08-05 之前的测量） ---- #
    text = (case.get("user_query", "") or "") + " ".join(
        t.get("content", "") for t in (case.get("conversation_history") or []))
    for c in hcs:
        v = c.get("value")
        if isinstance(v, (int, float)) and str(int(v)) not in text.replace(",", ""):
            problems.append(f"H3 硬约束 {c['type']}={v} 未出现在请求/历史文本中 -> 不算明确声明")
    by_field: Dict[str, List[tuple]] = defaultdict(list)
    for c in hcs:
        f = c.get("field") or c["type"]
        by_field[f].append((_DIRECTION.get(str(c.get("op")), "exact"), c.get("value")))
    for f, entries in by_field.items():
        ups = [v for d, v in entries if d == "upper" and isinstance(v, (int, float))]
        lows = [v for d, v in entries if d == "lower" and isinstance(v, (int, float))]
        if ups and lows and min(ups) < max(lows):
            problems.append(f"H4 字段 {f} 的上界 {min(ups)} 低于下界 {max(lows)} -> 互相矛盾")
        exacts = {v for d, v in entries if d == "exact" and v is not None}
        if len(exacts) > 1:
            problems.append(f"H4 字段 {f} 上有互相冲突的精确值 {sorted(map(str, exacts))}")
    return problems


def check_metadata(case: dict, seen_ids: set) -> List[str]:
    """M：held-out 出题要求的每题必备字段（任务书 §二）。"""
    problems: List[str] = []
    for f in REQUIRED_METADATA:
        if f == "hard_constraint_slots":
            # 允许为空列表：没有硬约束的题（计算/记忆/澄清/无约束检索）**必须**声明空列表，
            # 但不能省略这个键——「明确声明没有」与「忘了写」要能区分。
            if not isinstance(case.get(f), list):
                problems.append("M1 缺少必备字段 hard_constraint_slots（无硬约束时须写空列表）")
            continue
        if case.get(f) in (None, "", [], {}):
            problems.append(f"M1 缺少必备字段 {f}")
    cid = case.get("case_id")
    if cid in seen_ids:
        problems.append(f"M2 case_id {cid} 重复")
    tc = case.get("task_category")
    if tc is not None and tc not in TASK_CATEGORIES:
        problems.append(f"M3 task_category {tc!r} 不在 {list(TASK_CATEGORIES)} 中")
    sv = case.get("schema_version")
    if sv is not None and sv != SCHEMA_VERSION:
        problems.append(f"M4 schema_version {sv!r} != 门禁使用的 {SCHEMA_VERSION!r}")
    declared = set(case.get("hard_constraint_slots") or [])
    actual = {s for s in (slot_of(c) for c in hard_constraints(case)) if s}
    if declared != actual:
        problems.append(f"M5 hard_constraint_slots 声明 {sorted(declared)}，"
                        f"实际约束覆盖 {sorted(actual)} -> 不一致")
    if not isinstance(case.get("reference_calculations"), (dict, type(None))):
        problems.append("M6 reference_calculations 必须是对象或 null")
    return problems


# 无结果题的完成态必须说「诚实说明无匹配」，绝不能把「没有候选」当成「候选满足了约束」。
_HONEST_NO_MATCH_MARKERS = ("no match", "no results", "no listings", "none found",
                            "not available", "cannot", "could not", "couldn't",
                            "无匹配", "没有匹配", "没有房源", "无房源", "查不到", "无法",
                            "如实", "说明")


def check_no_result_completion(case: dict, fixtures_dir) -> List[str]:
    """无结果/无 listing 的题：完成态必须明确是「诚实说明无匹配」。

    **只适用于检索类**（2026-08-05 规则修正，见模块 docstring）：纯计算 / 记忆 / 澄清类
    本来就不产生 listing，它们的正确完成态也不是「说没找到房子」，对它们套这条会误伤。
    """
    if task_class(case) != "retrieval":
        return []
    if not _fixture_listings(case, fixtures_dir):
        text = " ".join(str(case.get(f) or "") for f in COMPLETION_FIELDS).lower()
        if not text.strip():
            return []            # 缺字段本身已由 C1 报出，不重复计
        if not any(m in text for m in _HONEST_NO_MATCH_MARKERS):
            return ["N1 无结果/无 listing 的题，correct_completion 未写明「诚实说明无匹配、"
                    "不得声称约束已满足」（静态只查措辞标记，是否真的写对由人工抽查确认）"]
    return []


def check_completion(case: dict) -> List[str]:
    if any(str(case.get(f) or "").strip() for f in COMPLETION_FIELDS):
        return []
    return [f"C1 未写明正确完成态（需要 {' 或 '.join(COMPLETION_FIELDS)} 之一，非空）"]


def check_evidence(case: dict) -> List[str]:
    """E：每条可评主张都要有依据来源——用户上下文 / 允许的派生计算 / 冻结的工具证据。"""
    problems: List[str] = []
    k = task_class(case)
    has_ctx = bool(case.get("user_query") or case.get("conversation_history"))
    has_calc = bool(case.get("reference_calculations"))
    has_fixture = bool(case.get("fixture"))
    expects_tools = bool(case.get("expected_tools"))
    if not has_ctx:
        problems.append("E1 既无 user_query 也无 conversation_history -> 没有用户上下文")
    if k == "calculation" and not has_calc:
        problems.append("E2 纯计算类必须带 reference_calculations（允许的派生计算）")
    if expects_tools and not has_fixture:
        problems.append("E3 声明了 expected_tools 但没有 fixture -> 工具证据未冻结，"
                        "跨轮不可复现")
    return problems


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", required=True, help="held-out 集 jsonl")
    p.add_argument("--out", required=True, help="门禁报告 json")
    p.add_argument("--checklist", default=None, help="人工抽查清单 md")
    p.add_argument("--quota", default=None, help="json 覆盖默认配额")
    p.add_argument("--manual-sample", type=int, default=15,
                   help="人工抽查清单里抽多少题（默认 15）")
    p.add_argument("--seed", type=int, default=20260805)
    p.add_argument("--fixtures", default=None,
                   help="fixture 目录（默认 evaluation/benchmark/fixtures）")
    p.add_argument("--schema", default="v2", choices=["v1", "v2"],
                   help="约束 schema 版本（默认 v2；v1 仅用于复现历史测量）")
    a = p.parse_args(argv)

    cases = [json.loads(l) for l in Path(a.cases).open(encoding="utf-8") if l.strip()]
    quota = json.loads(a.quota) if a.quota else dict(DEFAULT_QUOTA)
    FIX = Path(a.fixtures) if a.fixtures else REPO_ROOT / "evaluation" / "benchmark" / "fixtures"

    counts = Counter(stratum(c) for c in cases)
    quota_problems = []
    for k, need in quota.items():
        if counts.get(k, 0) < need:
            quota_problems.append(f"Q1 分层 {k}: {counts.get(k,0)}/{need} 未达配额")

    per_case = []
    seen_ids: set = set()
    for c in cases:
        st = stratum(c)
        probs: List[str] = []
        probs += check_metadata(c, seen_ids)
        seen_ids.add(c.get("case_id"))
        if st == "retrieval_hard":
            probs += check_hard(c)
            # H5：计入 hard 配额的题，至少要有一条约束能走确定性满足性谓词。
            # 否则它衡量的是「会不会复述用户条件」，不是「约束是否被履行」。
            if not any(constraint_is_satisfaction_verifiable(c, con, FIX)
                       for con in hard_constraints(c)):
                need = {slot_of(con) for con in hard_constraints(c)}
                probs.append(
                    f"H5 无任何可确定性判定满足性的硬约束（槽位 {sorted(x for x in need if x)} "
                    f"需要 fixture 里的结构化字段 "
                    f"{[SLOT_EVIDENCE_FIELD.get(x) for x in sorted(x for x in need if x)]}，"
                    f"格式为 (作用域, 字段)）"
                    " -> 只能计入行为覆盖，不得占 hard 配额")
        probs += check_completion(c)
        probs += check_evidence(c)
        probs += check_no_result_completion(c, FIX)
        per_case.append({"case_id": c.get("case_id"), "task_class": task_class(c),
                         "stratum": st, "passed": not probs, "problems": probs})

    failed = [r for r in per_case if not r["passed"]]
    # 未通过者替换后，还剩多少题能撑起配额
    surviving = Counter(r["stratum"] for r in per_case if r["passed"])
    residual = [f"Q2 替换掉未通过题后，分层 {k}: {surviving.get(k,0)}/{need} 仍不足"
                for k, need in quota.items() if surviving.get(k, 0) < need]

    cover = slot_coverage(cases, FIX)
    sat, beh = cover["satisfaction"], cover["behavior_only"]
    # Q4 只认满足性分母：heuristic / unknown / 无 listing 的分支一律不计入。
    quota_problems += [
        f"Q4 语义槽位 {s}: 满足性覆盖 {sat[s]}/{need} 未达最低覆盖数"
        f"（另有 {beh[s]} 题只能走无结果/未知分支，不计入本分母）"
        for s, need in SLOT_MIN_COVERAGE.items() if sat[s] < need]
    # Q5 行为覆盖单独卡，单独报，不与 Q4 合并。
    quota_problems += [
        f"Q5 行为覆盖 no_result_or_unknown: {cover['no_result_or_unknown_cases']}/{need} 未达下限"
        for k, need in BEHAVIOR_MIN_COVERAGE.items()
        if cover["no_result_or_unknown_cases"] < need]
    vocab = audit_vocabulary(c.get("type") for x in cases
                             for c in (x.get("expected_constraints") or []))
    if vocab["UNCLASSIFIED"]:
        quota_problems.append(
            f"Q3 词表里有未归类的 constraint type {vocab['UNCLASSIFIED']} —— "
            f"必须先在 hard_constraints.py 里判定它属于用户硬约束还是仪器条件")
    report = {
        "gate": "holdout_preflight",
        "schema_version": SCHEMA_VERSION,
        "rule": ("§2.13 配额 + owner ruling 2026-08-05：未通过者必须替换，"
                 "不允许在跑完之后用 not_applicable 消化"),
        "checks": {
            "Q1": "分层配额", "Q2": "替换未通过题后是否仍达标", "Q3": "词表审计（UNCLASSIFIED 必须为空）",
            "Q4": "语义槽位的满足性覆盖下限", "Q5": "无结果/未知的行为覆盖下限",
            "H1": "hard 配额题必须有可机器核验的硬约束", "H2": "硬约束参数齐全",
            "H2b": "参数值在冻结词表内（v2）", "H3": "数值出现在请求文本中（v1）",
            "H4": "同槽位互不矛盾", "H5": "至少一条约束能走确定性满足性谓词",
            "H6": "每条约束的 user_text 是原文子串且能复现规范化值（v2）",
            "C1": "写明正确完成态", "E1": "有用户上下文", "E2": "纯计算题有 reference_calculations",
            "E3": "声明了 expected_tools 就必须有冻结 fixture",
            "M1-M6": "held-out 必备元数据 / 唯一 id / 分层合法 / schema 版本一致 / 槽位声明与实际一致",
            "N1": "检索类的无结果题必须写明诚实说明无匹配",
        },
        "cases_file": a.cases,
        "n_cases": len(cases),
        "stratum_counts": dict(counts),
        "quota": quota,
        "quota_problems": quota_problems,
        "residual_after_replacement": residual,
        "n_failed": len(failed),
        "failed_case_ids": [r["case_id"] for r in failed],
        "problem_kind_counts": dict(Counter(p.split()[0] for r in per_case for p in r["problems"])),
        "vocabulary_audit": vocab,
        "slot_coverage": cover,
        "slot_min_coverage": SLOT_MIN_COVERAGE,
        "behavior_min_coverage": BEHAVIOR_MIN_COVERAGE,
        "slot_evidence_field": SLOT_EVIDENCE_FIELD,
        "denominator_rule": ("满足性分母只收「fixture 有结构化字段 + 有 listing + 谓词可判定」"
                             "的题；heuristic/unknown/无 listing 分支只计入行为覆盖"),
        "semantic_slots_without_a_type": list(MISSING_TYPES),
        "per_case": per_case,
        "gate_passed": not (quota_problems or failed or residual),
        "static_only_caveat": ("静态检查只能确认「有没有写」和「能不能机器核验」；"
                               "「写得对不对」必须由人工抽查确认，见 checklist"),
    }
    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if a.checklist:
        import random
        rng = random.Random(a.seed)
        pool = [r for r in per_case if r["passed"]] or per_case
        pick = rng.sample(pool, min(a.manual_sample, len(pool)))
        by_id = {c.get("case_id"): c for c in cases}
        L = ["# held-out preflight —— 人工抽查清单", "",
             f"来源：`{a.cases}`，静态门禁报告：`{a.out}`，抽样 seed `{a.seed}`。", "",
             "静态检查查不了「写得对不对」。以下每题请人工确认四点，"
             "**任一项不通过 → 替换该题，不要在跑完之后用 N/A 消化**。", "",
             "| # | case_id | 分层 | 硬约束明确可验证且不矛盾 | 已写明正确完成态 | 可评主张都有依据来源 | 通过? |",
             "|---|---|---|---|---|---|---|"]
        for i, r in enumerate(pick, 1):
            L.append(f"| {i} | `{r['case_id']}` | {r['stratum']} |  |  |  |  |")
        L += ["", "---", ""]
        for r in pick:
            c = by_id[r["case_id"]]
            L += [f"## {r['case_id']}  ({r['stratum']})", "",
                  f"- 请求：`{(c.get('user_query') or '')[:300]}`",
                  f"- 硬约束：`{json.dumps(hard_constraints(c), ensure_ascii=False)[:400]}`",
                  f"- 正确完成态：`{next((str(c.get(f)) for f in COMPLETION_FIELDS if c.get(f)), '（未写）')[:300]}`",
                  f"- fixture：`{c.get('fixture') or '（无）'}` ｜ reference_calculations："
                  f"`{'有' if c.get('reference_calculations') else '无'}`", ""]
        Path(a.checklist).write_text("\n".join(L), encoding="utf-8")

    print(json.dumps({k: report[k] for k in
                      ("n_cases", "stratum_counts", "quota_problems", "n_failed",
                       "residual_after_replacement", "problem_kind_counts", "gate_passed")},
                     indent=2, ensure_ascii=False))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
