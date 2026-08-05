"""实验 B — 第三次设计：rubric / evidence-packet 口径验证（NOT a quality re-evaluation）.

这不是质量重评，也不是泛化评估。重做要求的第 5 条（未参与开发的 held-out 集）在本仓库
无法满足，因此本脚本产出的任何质量比例都禁止进入 CV 与 fact-ledger。它的唯一目的是：
把修好的测量工具跑一遍，看新口径是否稳定，并把 harness 固化下来，等真正的 held-out 集
建好后原样复用。

相对 blind_review.py（第一次设计）的四处修复，全部在 PROGRESS.log 2026-08-05 04:30 冻结：

  1. 任务分四类（retrieval / calculation / memory / clarify），**分类计分，不合并**；
  2. 证据包补齐：当前请求 + 完整历史 + reconstructed_context + reference_calculations
     + 全部已执行工具结果（含 recall_memory/remember）+ 任务类别；
  3. 新增 not_applicable 与 cannot_assess 两个标签，且在规定情形下**必须**使用；
  4. 派生量规则前置：金额公式逐字给出，其余一律禁止推导。

输出目录与报告小节都标注「第三次设计」，且不与第一次设计的分布/κ/比例合并。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

EVIDENCE_CHAR_CAP = 12000
ANSWER_CHAR_CAP = 6000
MEMORY_TOOLS = {"recall_memory", "remember"}

# judge prompt 的硬约束语义说明来自**同一份冻结枚举**（配额 / preflight / judge 三处同源）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hard_constraints import JUDGE_PROMPT_SCOPE as HARD_SCOPE  # noqa: E402

# ---- FROZEN RULE 1: task classes ------------------------------------------ #
CLASS_DESC = {
    "retrieval": "检索推荐类：用户在找房/找区域，正确行为是基于工具证据给出可执行的候选或说明为何没有候选。",
    "calculation": "纯计算类：正确行为是用给定公式算出数字并说明口径；本类通常不调用任何工具，"
                   "没有工具证据是正常的，不构成缺陷。",
    "memory": "记忆类：正确行为是读取或写入用户偏好并据此回应；本类不以「是否给出房源」评价。",
    "clarify": "拒答/澄清类：信息不足或超出覆盖范围，正确行为是提出一个澄清问题或如实说明做不到；"
               "本类不以「是否给出房源」评价。",
}


def task_class(case: dict) -> str:
    et = set(case.get("expected_tools") or [])
    if case.get("expected_route") == "clarification":
        return "clarify"
    if case.get("category") == "G_memory" or (et and et <= MEMORY_TOOLS):
        return "memory"
    if not et and (case.get("reference_calculations") or case.get("category") == "B_money"):
        return "calculation"
    return "retrieval"


# ---- FROZEN RULE 4: derived quantities ------------------------------------ #
DERIVED_RULES = """允许的派生计算（只有这几条，公式来自 evaluation/benchmark/README.md，按此判定即视为有依据）：
  monthly_rent = weekly_rent * 52 / 12
  weekly_rent  = monthly_rent * 12 / 52
  deposit      = weekly_rent * 5   （年租 < £50,000）
  deposit      = weekly_rent * 6   （年租 >= £50,000）
  搬入总成本    = 首月租金 + 押金
除上述之外，**禁止任何派生**。步行时间、通勤时间、距离、犯罪率、价格走势等，
只要没有在证据里逐字出现，就是无依据 —— 不要因为"听起来合理"而放行。"""

# ---- FROZEN RULE 3: label vocabulary -------------------------------------- #
CRITERIA = {
    "hard_constraints": ("hard_constraints_satisfied",
        "回答是否满足用户本轮明确提出的每一条硬约束？\n"
        f"     {HARD_SCOPE}\n"
        "     yes / no / not_applicable / cannot_assess。\n"
        "     **本轮用户没有提出任何上述住房条件时，必须答 not_applicable**，不要答 yes 也不要答 unclear。"),
    "claims_supported": ("claims_evidence_supported",
        "回答里每一个数字与每一个地点主张，是否都能在下面展示的证据（或允许的派生公式）中找到依据？\n"
        "     yes / partial / no / not_applicable / cannot_assess。\n"
        "     回答里不含任何事实性数字或地点主张时（例如纯澄清问题），必须答 not_applicable。\n"
        "     证据被截断且判断依赖被截断的部分时，必须答 cannot_assess。"),
    "contradictions": ("contradicted_claim_count",
        "回答里有多少条主张与证据直接矛盾？答一个 >=0 的整数；无法判断时答字符串 cannot_assess。"),
    "actionable": ("directly_actionable",
        "对**这一类任务**而言，回答是否完成了它应该完成的事？\n"
        "     yes / no / not_applicable / cannot_assess。\n"
        "     注意：只有检索推荐类才以「是否给出具体候选或明确下一步」评价。\n"
        "     纯计算 / 记忆 / 拒答·澄清类，如果正确行为本就不是给房源，必须答 not_applicable，"
        "不要因为「没给房源」而判 no。"),
}

SYSTEM = (
    "You are an impartial evaluator of a UK rental-search assistant. You are shown a user "
    "request, the full conversation context, the raw tool evidence the assistant had, and "
    "the assistant's answer. You do not know which system or configuration produced the "
    "answer and must not speculate about it.\n"
    "Two rules override everything else:\n"
    "1. An absence of tool evidence is NOT automatically a defect. For some task types the "
    "correct behaviour produces no tool evidence at all, and for some requests the tool "
    "honestly has no data — in which case the correct answer is to say so.\n"
    "2. When a judgment does not apply to this task type, answer not_applicable. When the "
    "evidence you were shown is truncated and the judgment depends on the missing part, "
    "answer cannot_assess. Do NOT force a positive or negative verdict in either case.\n"
    "Reply with a single JSON object and nothing else."
)


def _fmt_item(item: dict, order: List[str], nonce: str) -> str:
    L = [f"REVIEW TASK {nonce}", "",
         f"=== 任务类别：{item['task_class']} ===", CLASS_DESC[item["task_class"]], ""]
    if item.get("history"):
        L += ["=== 之前的对话轮次（完整）==="]
        for t in item["history"]:
            L.append(f"  {t['role']}: {t['content']}")
        L.append("")
    L += ["=== 用户本轮请求 ===", item["request"], ""]
    if item.get("reconstructed_context"):
        L += ["=== 会话已累积的上下文（指代对象 / 粘性检索条件）===",
              item["reconstructed_context"], ""]
    if item.get("reference_calculations"):
        L += ["=== 本例允许的参考计算 ===", item["reference_calculations"], ""]
    L += ["=== 助手实际执行的工具 ===",
          (", ".join(item["tools_executed"]) or "（本轮没有执行任何工具）"), ""]
    if item.get("memory_evidence"):
        L += ["=== 记忆读写结果 ===", item["memory_evidence"], ""]
    L += ["=== 工具证据 ===",
          (item["evidence_text"] if item["evidence_text"].strip() not in ("", "[]")
           else "（空：本轮没有产生任何工具证据）"), ""]
    if item.get("evidence_truncated"):
        L += ["⚠️ 上面的证据已被截断到 12,000 字符。凡是依赖被截断部分才能下的判断，"
              "一律答 cannot_assess。", ""]
    L += [DERIVED_RULES, "", "=== 助手的回答 ===", item["answer_text"], "", "=== 问题 ==="]
    for i, k in enumerate(order, 1):
        f, q = CRITERIA[k]
        L.append(f"{i}. ({f}) {q}")
    fields = [f'  "{CRITERIA[k][0]}": ..., "{CRITERIA[k][0]}_reason": "<一句话依据>"'
              for k in order]
    L += ["", "只回复这个 JSON 对象（不要 markdown 代码围栏）：", "{", ",\n".join(fields), "}"]
    return "\n".join(L)


_JSON_RE = re.compile(r"\{.*\}", re.S)
_ALLOWED = {
    "hard_constraints_satisfied": {"yes", "no", "not_applicable", "cannot_assess"},
    "claims_evidence_supported": {"yes", "partial", "no", "not_applicable", "cannot_assess"},
    "directly_actionable": {"yes", "no", "not_applicable", "cannot_assess"},
}


def _parse(text: str) -> Optional[dict]:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    for cand in (m.group(0), m.group(0).replace("\n", " ")):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def _norm(field: str, value) -> Optional[str]:
    """Normalise to the frozen vocabulary. An out-of-vocabulary answer is recorded as
    ``OUT_OF_VOCAB:<raw>`` rather than coerced — the whole point of this round is to find
    out whether the rubric is followed, so a violation must stay visible."""
    if value is None:
        return None
    raw = str(value).strip()
    if field == "contradicted_claim_count":
        if raw.lower().replace(" ", "_") == "cannot_assess":
            return "cannot_assess"
        try:
            return str(max(0, int(raw.split()[0])))
        except (ValueError, IndexError):
            return f"OUT_OF_VOCAB:{raw[:24]}"
    v = raw.lower().strip(".").replace(" ", "_").replace("-", "_")
    v = {"true": "yes", "y": "yes", "false": "no", "n": "no", "partially": "partial",
         "n/a": "not_applicable", "na": "not_applicable", "unclear": "cannot_assess"}.get(v, v)
    if v not in _ALLOWED[field]:
        return f"OUT_OF_VOCAB:{raw[:24]}"
    return v


# --------------------------------------------------------------------------- #
def build_items(v1_items: List[dict], cases: Dict[str, dict],
                grader_rows: Dict[str, dict]) -> List[dict]:
    """Rebuild the packets for the SAME 50 items (no re-draw, same sample + seed)."""
    out = []
    for it in v1_items:
        cid = it["case_id"]
        case = cases[cid]
        g = grader_rows.get(it["source_run_id"]) or {}
        gi = g.get("grader_input") or {}
        evidence = g.get("evidence") or []
        mem = [e for e in evidence if e.get("tool") in MEMORY_TOOLS]
        ev = json.dumps(evidence, ensure_ascii=False, indent=1, default=str)
        rc = case.get("reference_calculations")
        ctx = gi.get("reconstructed_context") or {}
        ctx = {k: v for k, v in ctx.items() if k != "current_message"}
        out.append({
            "item_id": it["item_id"],
            "case_id": cid,
            "category": case.get("category"),
            "task_class": task_class(case),
            "source_run_id": it["source_run_id"],
            "request": case.get("user_query", ""),
            "history": [{"role": t.get("role"), "content": t.get("content", "")}
                        for t in (case.get("conversation_history") or [])],
            "reconstructed_context": (json.dumps(ctx, ensure_ascii=False, indent=1,
                                                 default=str)[:3000] if ctx else ""),
            "reference_calculations": (json.dumps(rc, ensure_ascii=False, indent=1,
                                                  default=str)[:2000] if rc else ""),
            "tools_executed": gi.get("tools_called") or [],
            "memory_evidence": (json.dumps(mem, ensure_ascii=False, indent=1,
                                           default=str)[:4000] if mem else ""),
            "evidence_text": ev[:EVIDENCE_CHAR_CAP],
            "evidence_truncated": len(ev) > EVIDENCE_CHAR_CAP,
            "evidence_chars_full": len(ev),
            "answer_text": (gi.get("final_answer") or "")[:ANSWER_CHAR_CAP],
        })
    return out


async def run_round(items, *, purpose, order_seed, criterion_seed, nonce,
                    concurrency, gap_ms, progress, tag) -> dict:
    from uk_rent_agent.llm.router import ModelRouter
    router = ModelRouter()
    route = router.route(purpose)
    llm = router.create(purpose)
    order = list(range(len(items)))
    random.Random(order_seed).shuffle(order)
    sem = asyncio.Semaphore(concurrency)
    verdicts, errors = {}, []

    async def one(idx, pos):
        it = items[idx]
        keys = list(CRITERIA)
        random.Random(criterion_seed * 1000 + idx).shuffle(keys)
        prompt = _fmt_item(it, keys, nonce)
        async with sem:
            await asyncio.sleep(gap_ms / 1000.0)
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    llm.ainvoke([("system", SYSTEM), ("human", prompt)]), timeout=120)
                text = getattr(resp, "content", "") or ""
                text = text if isinstance(text, str) else str(text)
            except Exception as exc:  # noqa: BLE001
                errors.append({"item_id": it["item_id"], "error": f"{type(exc).__name__}: {exc}"})
                progress(f"{tag} {it['item_id']} FAIL {type(exc).__name__}")
                return
            parsed = _parse(text)
            rec = {"item_id": it["item_id"], "case_id": it["case_id"],
                   "task_class": it["task_class"], "presented_position": pos,
                   "criterion_order": keys, "latency_ms": (time.perf_counter() - t0) * 1000,
                   "raw": text, "parsed": parsed,
                   "normalized": {CRITERIA[k][0]: _norm(CRITERIA[k][0],
                                                        (parsed or {}).get(CRITERIA[k][0]))
                                  for k in CRITERIA}}
            verdicts[it["item_id"]] = rec
            progress(f"{tag} {it['item_id']} [{it['task_class']}] OK "
                     f"{rec['latency_ms']:.0f}ms {rec['normalized']}")

    await asyncio.gather(*(one(i, p) for p, i in enumerate(order)))
    return {"design": "third_design_rubric_validation", "purpose": purpose,
            "model": route.model, "thinking": route.reasoning,
            "temperature": route.temperature, "order_seed": order_seed,
            "criterion_seed": criterion_seed, "nonce": nonce,
            "n_items": len(items), "n_verdicts": len(verdicts), "n_errors": len(errors),
            "errors": errors, "verdicts": verdicts,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def kappa(pairs) -> Optional[dict]:
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if n == 0:
        return None
    obs = sum(1 for a, b in pairs if a == b) / n
    ma, mb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    labs = {*ma, *mb}
    exp = sum((ma[l] / n) * (mb[l] / n) for l in labs)
    return {"n": n, "observed_agreement": obs, "expected_agreement": exp,
            "kappa": None if exp == 1.0 else (obs - exp) / (1 - exp),
            "distribution_a": dict(ma), "distribution_b": dict(mb)}


def agreement(r1, r2, label, items) -> dict:
    cls = {i["item_id"]: i["task_class"] for i in items}
    out = {"comparison": label, "overall": {}, "per_task_class": {}}
    for k in CRITERIA:
        f = CRITERIA[k][0]
        pairs = [(r1["verdicts"][i]["normalized"].get(f), r2["verdicts"][i]["normalized"].get(f))
                 for i in r1["verdicts"] if i in r2["verdicts"]]
        out["overall"][f] = kappa(pairs)
        per = {}
        for c in ("retrieval", "calculation", "memory", "clarify"):
            p = [(r1["verdicts"][i]["normalized"].get(f), r2["verdicts"][i]["normalized"].get(f))
                 for i in r1["verdicts"] if i in r2["verdicts"] and cls[i] == c]
            per[c] = kappa(p)
        out["per_task_class"][f] = per
    return out


def distributions(r, items) -> dict:
    cls = {i["item_id"]: i["task_class"] for i in items}
    out = {"overall": {}, "per_task_class": defaultdict(dict)}
    for k in CRITERIA:
        f = CRITERIA[k][0]
        out["overall"][f] = dict(Counter(v["normalized"].get(f) for v in r["verdicts"].values()))
        for c in ("retrieval", "calculation", "memory", "clarify"):
            out["per_task_class"][c][f] = dict(Counter(
                v["normalized"].get(f) for i, v in r["verdicts"].items() if cls[i] == c))
    out["per_task_class"] = dict(out["per_task_class"])
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--v1-items", default="evaluation/results/llm_blind_review/items.json")
    p.add_argument("--grader-inputs", nargs="+", required=True)
    p.add_argument("--cases", default=str(REPO_ROOT / "evaluation/benchmark/cases.jsonl"))
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=20260805)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--gap-ms", type=float, default=300.0)
    p.add_argument("--progress-log", default=str(REPO_ROOT / "PROGRESS.log"))
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prog = Path(a.progress_log)

    def progress(m):
        with prog.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} [B-v3design] {m}\n")

    import tempfile
    from evaluation.run_benchmark import _bootstrap_env
    _bootstrap_env(Path(tempfile.mkdtemp(prefix="rc_blind2_")), out / "_judge_events.jsonl")
    if not os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") == "offline-eval-placeholder":
        progress("ABORT: DEEPSEEK_API_KEY not loaded")
        return 2

    cases = {json.loads(l)["case_id"]: json.loads(l)
             for l in open(a.cases, encoding="utf-8") if l.strip()}
    v1 = json.load(open(a.v1_items, encoding="utf-8"))
    grader_rows = {}
    for path in a.grader_inputs:
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                d = json.loads(line)
                grader_rows[d.get("run_id")] = d

    items = build_items(v1, cases, grader_rows)
    (out / "items_v3design.json").write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    meta = {"design": "third_design_rubric_validation",
            "purpose_of_this_round": ("engineering validation that the repaired rubric + "
                                      "evidence packet give a stable reading; NOT a quality "
                                      "re-evaluation, NOT a generalization assessment"),
            "held_out_requirement_met": False,
            "must_not_merge_with": "evaluation/results/llm_blind_review/ (first design)",
            "sample": "identical 50 items, same draw, seed 20260804 — NOT re-drawn",
            "task_class_counts": dict(Counter(i["task_class"] for i in items)),
            "evidence_truncated_items": [i["case_id"] for i in items if i["evidence_truncated"]],
            "items_with_memory_evidence": sum(1 for i in items if i["memory_evidence"]),
            "items_with_reference_calculations": sum(1 for i in items if i["reference_calculations"]),
            "items_with_history": sum(1 for i in items if i["history"])}
    (out / "design_v3.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    progress(f"third design packets built: {meta['task_class_counts']}")
    if a.dry_run:
        print(json.dumps(meta, indent=2, ensure_ascii=False))
        print("\n---- sample prompt ----\n")
        print(_fmt_item(items[0], list(CRITERIA), "DRYRUN")[:3500])
        return 0

    async def go():
        r1 = await run_round(items, purpose="judge", order_seed=a.seed + 1,
                             criterion_seed=a.seed + 2, nonce="RC-B-V3-R1",
                             concurrency=a.concurrency, gap_ms=a.gap_ms,
                             progress=progress, tag="v3r1")
        (out / "round1.json").write_text(json.dumps(r1, indent=2, ensure_ascii=False), encoding="utf-8")
        r2 = await run_round(items, purpose="judge", order_seed=a.seed + 77,
                             criterion_seed=a.seed + 99, nonce="RC-B-V3-R2",
                             concurrency=a.concurrency, gap_ms=a.gap_ms,
                             progress=progress, tag="v3r2")
        (out / "round2.json").write_text(json.dumps(r2, indent=2, ensure_ascii=False), encoding="utf-8")
        r3 = await run_round(items, purpose="pro", order_seed=a.seed + 555,
                             criterion_seed=a.seed + 777, nonce="RC-B-V3-R3",
                             concurrency=a.concurrency, gap_ms=a.gap_ms,
                             progress=progress, tag="v3r3pro")
        (out / "round3_pro.json").write_text(json.dumps(r3, indent=2, ensure_ascii=False), encoding="utf-8")
        res = {"design": "third_design_rubric_validation",
               "held_out_requirement_met": False,
               "reminder": ("口径验证，不是质量重评/泛化评估；本文件中的任何比例都禁止进入 "
                            "CV 与 fact-ledger；不得与第一次设计的分布/κ 合并"),
               "same_model_two_rounds_self_consistency":
                   agreement(r1, r2, "v3 round1 vs round2 (SAME model deepseek-v4-flash) — "
                                     "SELF-CONSISTENCY, not inter-rater reliability", items),
               "cross_model_round1_vs_round3":
                   agreement(r1, r3, "v3 round1 (deepseek-v4-flash) vs round3 "
                                     "(deepseek-v4-pro) — cross-model, same vendor", items),
               "distributions": {"round1": distributions(r1, items),
                                 "round2": distributions(r2, items),
                                 "round3_pro": distributions(r3, items)},
               "out_of_vocab_answers": {
                   n: [f"{i}:{f}={v}" for i, rec in r["verdicts"].items()
                       for f, v in rec["normalized"].items()
                       if isinstance(v, str) and v.startswith("OUT_OF_VOCAB")]
                   for n, r in (("round1", r1), ("round2", r2), ("round3_pro", r3))}}
        (out / "agreement_v3.json").write_text(json.dumps(res, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
    asyncio.run(go())
    progress("third design (rubric validation) DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
