"""Independent MODEL blind review of the held-out v2 answers — three rounds.

NOT human review. NOT answer accuracy. NOT inter-rater reliability. Every output of this
file is a MODEL's reading of an answer against frozen evidence, and must be reported that
way. The rubric, the label vocabulary, the seeds and the request budget were all frozen in
PROGRESS.log before the first request of this batch.

What it inherits from blind_review_v2.py (the third-design instrument, §2.12):
  * four task classes scored separately, never merged;
  * an evidence packet carrying the request, the FULL history, the reconstructed context,
    the allowed derivations, the memory read/write results and every executed tool result;
  * not_applicable and cannot_assess as first-class labels, mandatory in defined cases;
  * an out-of-vocabulary answer recorded verbatim as a judge failure, never normalised.

What it adds for the held-out batch:
  * five strata (retrieval_hard / retrieval_soft / calculation / memory / clarify);
  * the two definitions §2.12.4 left open: the ``partial`` criterion for
    claims_evidence_supported, and a per-stratum definition of "did what it should"
    (task_completed_correctly, replacing the ill-defined directly_actionable);
  * per-item append to JSONL and resume, so nothing lives only in memory and a re-run
    never pays twice;
  * the case's own frozen ``allowed_evidence_sources`` are shown to the judge, so
    "what may count as support" is per-case and explicit.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import constraint_schema_v2 as v2  # noqa: E402

EVIDENCE_CHAR_CAP = 24000        # the held-out fixtures are small; nothing should truncate
ANSWER_CHAR_CAP = 8000
MEMORY_TOOLS = {"recall_memory", "remember"}

CLASS_DESC = {
    "retrieval_hard": (
        "检索推荐类（用户提出了明确住房条件）：用户在找房并说明了条件，正确行为是基于工具证据"
        "给出满足全部条件的候选，或如实说明没有候选。"),
    "retrieval_soft": (
        "检索浏览类：用户没有提出可被违反的筛选条件（或工具返回空），正确行为是基于证据概述"
        "可选项、或如实说明没有匹配并建议放宽条件。"),
    "calculation": (
        "纯计算类：正确行为是用给定公式算出数字并说明口径；本类通常不调用任何工具，"
        "没有工具证据是正常的，不构成缺陷。"),
    "memory": (
        "记忆类：正确行为是读取或写入用户偏好并据此回应；本类不以「是否给出房源」评价。"),
    "clarify": (
        "拒答/澄清类：信息不足或超出覆盖范围，正确行为是提出一个具体澄清问题、或如实说明"
        "做不到；本类不以「是否给出房源」评价。"),
}

COMPLETION_RULE = {
    "retrieval_hard": "指出至少一处满足**全部**已声明条件的冻结房源，或如实说明没有任何一处满足；"
                      "并且不把违反条件的房源当作匹配项推荐。",
    "retrieval_soft": "（开放浏览）基于冻结证据概述可选项或询问用户的条件；"
                      "（无结果）如实说明没有找到匹配。",
    "calculation": "用允许的公式算出用户要的数字并说明口径。本类**不产生工具证据是正确的**。",
    "memory": "（读）把存储的事实报回来；（写）确认存了什么。本类**不给房源是正确的**。",
    "clarify": "（信息不足）提出一个点名缺失信息的澄清问题、或明确说明缺什么；"
               "（超出范围）明确说明做不到。本类**不给房源是正确的**。",
}

DERIVED_RULES = """允许的派生计算（只有这几条，公式来自 evaluation/benchmark/README.md，按此判定即视为有依据）：
  monthly_rent = weekly_rent * 52 / 12
  weekly_rent  = monthly_rent * 12 / 52
  deposit      = weekly_rent * 5   （年租 < £50,000）
  deposit      = weekly_rent * 6   （年租 >= £50,000）
  搬入总成本    = 首月租金 + 押金
除上述之外，**禁止任何派生**。步行时间、通勤时间、距离、犯罪率、价格走势等，
只要没有在证据里逐字出现，就是无依据 —— 不要因为"听起来合理"而放行。
**物业特征只能看 features 这个结构化字段**；不得从房源描述文字里推断家具/宠物/花园等特征。"""

CRITERIA = {
    "hard_constraints": ("hard_constraints_satisfied",
        "回答是否满足用户本轮明确提出的每一条硬约束？\n"
        f"     {v2.JUDGE_PROMPT_SCOPE}\n"
        "     yes / no / not_applicable / cannot_assess。\n"
        "     **本轮用户没有提出任何上述住房条件时，必须答 not_applicable**。\n"
        "     证据里某一条件的值是未确定的（例如可入住日期写着 Contact agent），"
        "该条件既不算满足也不算违反：就其余条件作答，不要因此判 no。"),
    "claims_supported": ("claims_evidence_supported",
        "回答里每一个数字与每一个地点主张，是否都能在下面展示的证据（或允许的派生公式、"
        "或用户自己说过的数字）中找到依据？\n"
        "     yes / partial / no / not_applicable / cannot_assess。\n"
        "     yes = 每一条这样的主张都有依据；\n"
        "     partial = **至少一条有依据、且至少一条没有依据**；\n"
        "     no = 没有任何一条有依据；\n"
        "     not_applicable = 回答里不含任何事实性数字或地点主张（例如纯澄清问题）；\n"
        "     cannot_assess = 证据被截断且判断依赖被截断的部分。"),
    "contradictions": ("contradicted_claim_count",
        "回答里有多少条主张与证据**直接矛盾**（不是缺依据，是与证据里的值冲突）？"
        "答一个 >=0 的整数；无法判断时答字符串 cannot_assess。"),
    "completed": ("task_completed_correctly",
        "对**这一类任务**而言，回答是否完成了它应该完成的事？\n"
        "     yes / no / not_applicable / cannot_assess。\n"
        "     本类任务的「完成」定义见上方「本类任务的正确完成」一节，按那个定义判，"
        "不要用别的标准。\n"
        "     只有当这个判定对本任务确实不适用时才答 not_applicable。"),
}

SYSTEM = (
    "You are an impartial evaluator of a UK rental-search assistant. You are shown a user "
    "request, the full conversation context, the raw tool evidence the assistant had, and "
    "the assistant's answer. You do not know which system or configuration produced the "
    "answer and must not speculate about it.\n"
    "Three rules override everything else:\n"
    "1. An absence of tool evidence is NOT automatically a defect. For some task types the "
    "correct behaviour produces no tool evidence at all, and for some requests the tool "
    "honestly has no data — in which case the correct answer is to say so.\n"
    "2. When a judgment does not apply to this task type, answer not_applicable. When the "
    "evidence you were shown is truncated and the judgment depends on the missing part, "
    "answer cannot_assess. Do NOT force a positive or negative verdict in either case.\n"
    "3. Judge only against the evidence and the rules given. Do not use outside knowledge "
    "of the London rental market.\n"
    "Reply with a single JSON object and nothing else."
)


def _fmt_item(item: dict, order: List[str], nonce: str) -> str:
    L = [f"REVIEW TASK {nonce}", "",
         f"=== 任务类别：{item['task_class']} ===", CLASS_DESC[item["task_class"]], "",
         "=== 本类任务的正确完成 ===", COMPLETION_RULE[item["task_class"]], ""]
    if item.get("history"):
        L += ["=== 之前的对话轮次（完整）==="]
        for t in item["history"]:
            L.append(f"  {t['role']}: {t['content']}")
        L.append("")
    L += ["=== 用户本轮请求 ===", item["request"], ""]
    if item.get("reconstructed_context"):
        L += ["=== 会话已累积的上下文（指代对象 / 粘性检索条件）===",
              item["reconstructed_context"], ""]
    if item.get("allowed_evidence_sources"):
        L += ["=== 本例允许作为依据的证据来源（只有这些算依据）===",
              "\n".join(f"  - {s}" for s in item["allowed_evidence_sources"]), ""]
    if item.get("reference_calculations"):
        L += ["=== 本例允许的参考计算 ===", item["reference_calculations"], ""]
    L += ["=== 助手实际执行的工具 ===",
          (", ".join(item["tools_executed"]) or "（本轮没有执行任何工具）"), ""]
    if item.get("memory_evidence"):
        L += ["=== 记忆读写结果 ===", item["memory_evidence"], ""]
    L += ["=== 工具证据（完整结构化输出）===",
          (item["evidence_text"] if item["evidence_text"].strip() not in ("", "[]")
           else "（空：本轮没有产生任何工具证据）"), ""]
    if item.get("evidence_truncated"):
        L += [f"⚠️ 上面的证据已被截断到 {EVIDENCE_CHAR_CAP} 字符。凡是依赖被截断部分才能下的"
              "判断，一律答 cannot_assess。", ""]
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
    "task_completed_correctly": {"yes", "no", "not_applicable", "cannot_assess"},
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
    ``OUT_OF_VOCAB:<raw>`` and counted as a judge failure — never coerced into a label."""
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
         "n/a": "not_applicable", "na": "not_applicable"}.get(v, v)
    if v not in _ALLOWED[field]:
        return f"OUT_OF_VOCAB:{raw[:24]}"
    return v


# --------------------------------------------------------------------------- #
def build_items(cases: Dict[str, dict], grader_rows: List[dict]) -> List[dict]:
    out = []
    for g in grader_rows:
        cid = g.get("case_id")
        case = cases.get(cid)
        if not case:
            continue
        gi = g.get("grader_input") or {}
        evidence = g.get("evidence") or []
        mem = [e for e in evidence if e.get("tool") in MEMORY_TOOLS]
        ev = json.dumps(evidence, ensure_ascii=False, indent=1, default=str)
        rc = case.get("reference_calculations")
        ctx = gi.get("reconstructed_context") or {}
        ctx = {k: v for k, v in ctx.items() if k != "current_message"}
        out.append({
            "item_id": f"item_{cid}",
            "case_id": cid,
            "task_class": case["task_category"],
            "source_run_id": g.get("run_id"),
            "request": case.get("user_query", ""),
            "history": [{"role": t.get("role"), "content": t.get("content", "")}
                        for t in (case.get("conversation_history") or [])],
            "reconstructed_context": (json.dumps(ctx, ensure_ascii=False, indent=1,
                                                 default=str)[:3000] if ctx else ""),
            "allowed_evidence_sources": case.get("allowed_evidence_sources") or [],
            "reference_calculations": (json.dumps(rc, ensure_ascii=False, indent=1,
                                                  default=str)[:2000] if rc else ""),
            "tools_executed": gi.get("tools_called") or [],
            "memory_evidence": (json.dumps(mem, ensure_ascii=False, indent=1,
                                           default=str)[:6000] if mem else ""),
            "evidence_text": ev[:EVIDENCE_CHAR_CAP],
            "evidence_truncated": len(ev) > EVIDENCE_CHAR_CAP,
            "evidence_chars_full": len(ev),
            "answer_text": (gi.get("final_answer") or "")[:ANSWER_CHAR_CAP],
        })
    out.sort(key=lambda x: x["case_id"])
    return out


def _done(path: Path) -> set:
    done = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["item_id"])
                except Exception:                                  # noqa: BLE001
                    continue
    return done


ROUNDS = {
    "round1": {"purpose": "judge", "order_seed": 20260811, "criterion_seed": 20260812,
               "nonce": "RC-HO2-R1"},
    "round2": {"purpose": "judge", "order_seed": 20260887, "criterion_seed": 20260909,
               "nonce": "RC-HO2-R2"},
    "round3_pro": {"purpose": "pro", "order_seed": 20261365, "criterion_seed": 20261587,
                   "nonce": "RC-HO2-R3"},
}


async def run_round(items, name, cfg, out_dir: Path, *, concurrency, gap_ms, progress,
                    max_consecutive_failures=10) -> dict:
    from uk_rent_agent.llm.router import ModelRouter
    router = ModelRouter()
    route = router.route(cfg["purpose"])
    llm = router.create(cfg["purpose"])
    path = out_dir / f"{name}.jsonl"
    done = _done(path)
    todo = [i for i, it in enumerate(items) if it["item_id"] not in done]
    order = list(range(len(items)))
    random.Random(cfg["order_seed"]).shuffle(order)
    order = [i for i in order if i in set(todo)]
    sem = asyncio.Semaphore(concurrency)
    state = {"fail_streak": 0, "aborted": False, "n_ok": 0, "n_fail": 0}
    lock = asyncio.Lock()

    async def one(idx, pos):
        if state["aborted"]:
            return
        it = items[idx]
        keys = list(CRITERIA)
        random.Random(cfg["criterion_seed"] * 1000 + idx).shuffle(keys)
        prompt = _fmt_item(it, keys, cfg["nonce"])
        async with sem:
            if state["aborted"]:
                return
            await asyncio.sleep(gap_ms / 1000.0)
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    llm.ainvoke([("system", SYSTEM), ("human", prompt)]), timeout=120)
                text = getattr(resp, "content", "") or ""
                text = text if isinstance(text, str) else str(text)
            except Exception as exc:                               # noqa: BLE001
                async with lock:
                    state["fail_streak"] += 1
                    state["n_fail"] += 1
                    rec = {"item_id": it["item_id"], "case_id": it["case_id"],
                           "task_class": it["task_class"], "error":
                               f"{type(exc).__name__}: {exc}"}
                    with (out_dir / f"{name}_errors.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    progress(f"{name} {it['item_id']} FAIL {type(exc).__name__} "
                             f"(streak {state['fail_streak']})")
                    if state["fail_streak"] >= max_consecutive_failures:
                        state["aborted"] = True
                        progress(f"{name} ABORT: {state['fail_streak']} consecutive failures")
                return
            parsed = _parse(text)
            rec = {"item_id": it["item_id"], "case_id": it["case_id"],
                   "task_class": it["task_class"], "presented_position": pos,
                   "criterion_order": keys, "latency_ms": (time.perf_counter() - t0) * 1000,
                   "raw": text, "parsed": parsed,
                   "normalized": {CRITERIA[k][0]: _norm(CRITERIA[k][0],
                                                        (parsed or {}).get(CRITERIA[k][0]))
                                  for k in CRITERIA}}
            async with lock:
                state["fail_streak"] = 0
                state["n_ok"] += 1
                with path.open("a", encoding="utf-8") as fh:      # append-per-item, resumable
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    fh.flush()
                progress(f"{name} {it['item_id']} [{it['task_class']}] OK "
                         f"{rec['latency_ms']:.0f}ms {rec['normalized']}")

    await asyncio.gather(*(one(i, p) for p, i in enumerate(order)))
    verdicts = {}
    for line in path.open(encoding="utf-8") if path.exists() else []:
        if line.strip():
            r = json.loads(line)
            verdicts[r["item_id"]] = r
    return {"round": name, "purpose": cfg["purpose"], "model": route.model,
            "thinking": route.reasoning, "temperature": route.temperature,
            "order_seed": cfg["order_seed"], "criterion_seed": cfg["criterion_seed"],
            "nonce": cfg["nonce"], "n_items": len(items), "n_verdicts": len(verdicts),
            "n_new_ok": state["n_ok"], "n_new_fail": state["n_fail"],
            "aborted": state["aborted"], "verdicts": verdicts,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default=str(REPO_ROOT /
                   "evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl"))
    p.add_argument("--grader-inputs", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--rounds", default="round1,round2,round3_pro")
    p.add_argument("--case-ids", default=None)
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
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                     f"[B-holdout-v2] {m}\n")

    cases = {json.loads(l)["case_id"]: json.loads(l)
             for l in open(a.cases, encoding="utf-8") if l.strip()}
    rows = []
    seen = set()
    for path in a.grader_inputs:
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                d = json.loads(line)
                key = (d.get("case_id"), d.get("run_id"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(d)
    items = build_items(cases, rows)
    if a.case_ids:
        allow = {c.strip() for c in a.case_ids.split(",") if c.strip()}
        items = [i for i in items if i["case_id"] in allow]

    (out / "items.json").write_text(json.dumps(items, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    meta = {"design": "held_out_v2_blind_review",
            "held_out_requirement_met": True,
            "not_human_review": ("every label in this directory is produced by a MODEL; "
                                 "nothing here is human review, human evaluation, answer "
                                 "accuracy or inter-rater reliability"),
            "dataset": str(a.cases),
            "n_items": len(items),
            "task_class_counts": dict(Counter(i["task_class"] for i in items)),
            "evidence_truncated_items": [i["case_id"] for i in items if i["evidence_truncated"]],
            "evidence_chars_max": max([i["evidence_chars_full"] for i in items] or [0]),
            "rounds": ROUNDS}
    (out / "design.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    if a.dry_run:
        print(json.dumps(meta, indent=2, ensure_ascii=False))
        print("\n---- sample prompt ----\n")
        print(_fmt_item(items[0], list(CRITERIA), "DRYRUN")[:6000])
        return 0

    import tempfile
    from evaluation.run_benchmark import _bootstrap_env
    _bootstrap_env(Path(tempfile.mkdtemp(prefix="rc_ho2_judge_")), out / "_judge_events.jsonl")
    if not os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") == "offline-eval-placeholder":
        progress("ABORT: DEEPSEEK_API_KEY not loaded")
        return 2

    async def go():
        summary = {}
        for name in [r.strip() for r in a.rounds.split(",") if r.strip()]:
            res = await run_round(items, name, ROUNDS[name], out,
                                  concurrency=a.concurrency, gap_ms=a.gap_ms,
                                  progress=progress)
            (out / f"{name}_meta.json").write_text(
                json.dumps({k: v for k, v in res.items() if k != "verdicts"},
                           indent=2, ensure_ascii=False), encoding="utf-8")
            summary[name] = {k: v for k, v in res.items() if k != "verdicts"}
            progress(f"{name} done: {res['n_verdicts']}/{res['n_items']} verdicts, "
                     f"{res['n_new_ok']} new ok, {res['n_new_fail']} new failures")
            if res["aborted"]:
                progress("stage aborted; not starting the next round")
                break
        (out / "rounds_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    asyncio.run(go())
    progress("held-out v2 blind review stage DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
