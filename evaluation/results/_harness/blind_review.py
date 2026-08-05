"""Experiment B — independent-model blind review (GOAL §实验B).

Takes the ROUTED (production) arm runs produced by Experiment A, draws a stratified
sample of cases, freezes each one's tool evidence, and has an independent model judge
four things per item WITHOUT being told anything about the configuration that produced
the answer. Two rounds with different item order, different criterion order and a
recorded nonce give a SELF-CONSISTENCY kappa (one model, twice) — NOT inter-rater
reliability, and never described as such.

Outputs (evaluation/results/llm_blind_review/):
  sample.json                 the drawn sample + seeds + stratum counts
  items.json                  the blinded items actually sent (with truncation flags)
  round1.json / round2.json   raw verdicts
  round3_pro.json             optional cross-model round (deepseek-v4-pro)
  agreement.json              Cohen's kappa + agreement rates
  human_calibration_sheet.md  15 items with the four judgments left blank
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

CRITERIA = {
    "hard_constraints": (
        "hard_constraints_satisfied",
        "Does the answer respect every HARD constraint the user stated (budget ceiling, "
        "bedroom count, area/location, move-in date, commute limit)? Answer yes / no / unclear."),
    "claims_supported": (
        "claims_evidence_supported",
        "Is every NUMBER and every PLACE claim in the answer supported by the tool evidence "
        "shown (rent figures, deposits, travel times, distances, crime scores, addresses)? "
        "Answer yes / partial / no."),
    "contradictions": (
        "contradicted_claim_count",
        "How many distinct claims in the answer CONTRADICT the tool evidence? Answer an "
        "integer (0 if none)."),
    "actionable": (
        "directly_actionable",
        "Could the user act on this answer directly — does it name specific options or a "
        "specific next step rather than only describing what it could not do? Answer yes / no."),
}

SYSTEM = (
    "You are an impartial evaluator of a UK rental-search assistant. You are shown a user "
    "request, the raw tool evidence the assistant had, and the assistant's answer. You do "
    "not know which system or configuration produced the answer and must not speculate "
    "about it. Judge ONLY what is in front of you. Ground every judgment in the evidence "
    "shown: if a number or place in the answer does not appear in the evidence, it is NOT "
    "supported, even if it sounds plausible. Reply with a single JSON object and nothing "
    "else."
)


def _fmt_item(item: dict, criterion_order: List[str], nonce: str) -> str:
    lines = [f"REVIEW TASK {nonce}", "", "=== USER REQUEST ==="]
    if item.get("history"):
        lines.append("(earlier turns in this conversation)")
        for t in item["history"]:
            lines.append(f"  {t['role']}: {t['content']}")
    lines += [item["request"], "",
              "=== TOOL EVIDENCE AVAILABLE TO THE ASSISTANT ===",
              item["evidence_text"], ""]
    if item.get("evidence_truncated"):
        lines.append("[evidence truncated for length — judge only what is shown]")
    lines += ["=== ASSISTANT ANSWER ===", item["answer_text"], "",
              "=== QUESTIONS ==="]
    for i, key in enumerate(criterion_order, 1):
        field, question = CRITERIA[key]
        lines.append(f"{i}. ({field}) {question}")
    fields = []
    for key in criterion_order:
        field, _ = CRITERIA[key]
        fields.append(f'  "{field}": ..., "{field}_reason": "<one sentence>"')
    lines += ["", "Reply with exactly this JSON object (no markdown fence):",
              "{", ",\n".join(fields), "}"]
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _parse_verdict(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        cleaned = m.group(0).replace("\n", " ")
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _norm(field: str, value) -> Optional[str]:
    if value is None:
        return None
    if field == "contradicted_claim_count":
        try:
            return str(max(0, int(str(value).strip().split()[0])))
        except (ValueError, IndexError):
            return None
    v = str(value).strip().lower()
    v = v.strip(".").strip()
    if v in ("yes", "true", "y"):
        return "yes"
    if v in ("no", "false", "n"):
        return "no"
    if v in ("partial", "partially"):
        return "partial"
    if v in ("unclear", "unknown", "n/a"):
        return "unclear"
    return v[:20] or None


# --------------------------------------------------------------------------- #
# sampling
# --------------------------------------------------------------------------- #
def draw_sample(cases: List[dict], available: set, n: int, seed: int) -> dict:
    """Proportional stratified draw over the 7 categories, with explicit floors for the
    three request shapes the GOAL requires to be covered."""
    rng = random.Random(seed)
    pool = [c for c in cases if c["case_id"] in available]
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for c in pool:
        by_cat[c.get("category", "?")].append(c)
    for v in by_cat.values():
        v.sort(key=lambda c: c["case_id"])
        rng.shuffle(v)

    total = len(pool)
    chosen: List[str] = []
    # proportional allocation, largest-remainder
    quotas = {}
    for cat, rows in by_cat.items():
        quotas[cat] = len(rows) * n / total if total else 0
    base = {c: int(q) for c, q in quotas.items()}
    rem = n - sum(base.values())
    for cat in sorted(quotas, key=lambda c: -(quotas[c] - base[c]))[:max(0, rem)]:
        base[cat] += 1
    picked: Dict[str, List[str]] = {}
    for cat, rows in by_cat.items():
        k = min(base.get(cat, 0), len(rows))
        picked[cat] = [r["case_id"] for r in rows[:k]]
        chosen += picked[cat]

    # floors: >=5 commute, >=5 multi-constraint, >=5 multi-turn (has conversation_history)
    def ensure(pred, want, label):
        have = [cid for cid in chosen if pred(next(c for c in pool if c["case_id"] == cid))]
        if len(have) >= want:
            return 0
        extra = [c["case_id"] for c in pool
                 if pred(c) and c["case_id"] not in chosen]
        rng.shuffle(extra)
        need = want - len(have)
        added = extra[:need]
        for cid in added:
            # swap out an over-represented, non-floor case to keep n fixed
            droppable = [x for x in chosen
                         if not pred(next(c for c in pool if c["case_id"] == x))]
            if droppable:
                chosen.remove(droppable[-1])
            chosen.append(cid)
        return len(added)

    floors = {
        "C_commute>=5": ensure(lambda c: c.get("category") == "C_commute", 5, "commute"),
        "E_multi_constraint>=5": ensure(
            lambda c: c.get("category") == "E_multi_constraint", 5, "multi"),
        "multi_turn>=5": ensure(lambda c: bool(c.get("conversation_history")), 5, "multiturn"),
    }
    chosen = sorted(set(chosen))[:n]
    cat_counts = Counter(next(c for c in pool if c["case_id"] == cid).get("category")
                         for cid in chosen)
    return {"case_ids": chosen, "seed": seed, "n_requested": n, "n_drawn": len(chosen),
            "pool_size": total, "category_counts": dict(cat_counts),
            "floor_additions": floors}


# --------------------------------------------------------------------------- #
def build_items(sample_ids: List[str], cases_by_id: Dict[str, dict],
                grader_rows: Dict[str, dict], runs_by_key: Dict[str, dict]) -> List[dict]:
    items = []
    for cid in sample_ids:
        key = None
        for rep in (1, 2, 3):
            k = f"{cid}#r{rep}#routed_models"
            if k in grader_rows and runs_by_key.get(k, {}).get("ab_ok"):
                key = k
                break
        if key is None:
            continue
        g = grader_rows[key]
        gi = g.get("grader_input") or {}
        case = cases_by_id[cid]
        ev = json.dumps(g.get("evidence") or [], ensure_ascii=False, indent=1, default=str)
        truncated = len(ev) > EVIDENCE_CHAR_CAP
        answer = gi.get("final_answer") or ""
        items.append({
            "item_id": f"item_{len(items)+1:03d}",
            "case_id": cid,
            "category": case.get("category"),
            "source_run_id": key,
            "request": case.get("user_query", ""),
            "history": [{"role": t.get("role"), "content": t.get("content", "")}
                        for t in (case.get("conversation_history") or [])],
            "evidence_text": ev[:EVIDENCE_CHAR_CAP],
            "evidence_truncated": truncated,
            "evidence_chars_full": len(ev),
            "answer_text": answer[:ANSWER_CHAR_CAP],
            "answer_truncated": len(answer) > ANSWER_CHAR_CAP,
            "tools_executed": gi.get("tools_called") or [],
        })
    return items


# --------------------------------------------------------------------------- #
async def run_round(items: List[dict], *, purpose: str, order_seed: int,
                    criterion_seed: int, nonce: str, concurrency: int,
                    gap_ms: float, progress, tag: str) -> dict:
    from uk_rent_agent.llm.router import ModelRouter

    router = ModelRouter()
    route = router.route(purpose)
    llm = router.create(purpose)
    rng_o = random.Random(order_seed)
    order = list(range(len(items)))
    rng_o.shuffle(order)
    rng_c = random.Random(criterion_seed)

    sem = asyncio.Semaphore(concurrency)
    verdicts: Dict[str, dict] = {}
    errors: List[dict] = []

    async def one(idx: int, pos: int):
        item = items[idx]
        keys = list(CRITERIA)
        random.Random(criterion_seed * 1000 + idx).shuffle(keys)
        prompt = _fmt_item(item, keys, nonce)
        async with sem:
            await asyncio.sleep(gap_ms / 1000.0)
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    llm.ainvoke([("system", SYSTEM), ("human", prompt)]), timeout=120)
                text = getattr(resp, "content", "") or ""
                parsed = _parse_verdict(text if isinstance(text, str) else str(text))
            except Exception as exc:  # noqa: BLE001
                errors.append({"item_id": item["item_id"], "error": f"{type(exc).__name__}: {exc}"})
                progress(f"{tag} {item['item_id']} FAIL {type(exc).__name__}")
                return
            dt = (time.perf_counter() - t0) * 1000
            rec = {"item_id": item["item_id"], "case_id": item["case_id"],
                   "presented_position": pos, "criterion_order": keys,
                   "latency_ms": dt, "raw": text if isinstance(text, str) else str(text),
                   "parsed": parsed,
                   "normalized": {f: _norm(f, (parsed or {}).get(f))
                                  for f, _ in (CRITERIA[k] for k in CRITERIA)}}
            verdicts[item["item_id"]] = rec
            progress(f"{tag} {item['item_id']} OK {dt:.0f}ms "
                     f"{ {k: v for k, v in rec['normalized'].items()} }")

    await asyncio.gather(*(one(idx, pos) for pos, idx in enumerate(order)))
    return {"purpose": purpose, "model": route.model, "thinking": route.reasoning,
            "temperature": route.temperature, "order_seed": order_seed,
            "criterion_seed": criterion_seed, "nonce": nonce,
            "presented_order": [items[i]["item_id"] for i in order],
            "n_items": len(items), "n_verdicts": len(verdicts), "n_errors": len(errors),
            "errors": errors, "verdicts": verdicts,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


# --------------------------------------------------------------------------- #
def cohens_kappa(pairs: List[tuple]) -> Optional[dict]:
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if n == 0:
        return None
    labels = sorted({x for p in pairs for x in p})
    obs = sum(1 for a, b in pairs if a == b) / n
    ma = Counter(a for a, _ in pairs)
    mb = Counter(b for _, b in pairs)
    exp = sum((ma[l] / n) * (mb[l] / n) for l in labels)
    kappa = None if exp == 1.0 else (obs - exp) / (1 - exp)
    return {"n": n, "observed_agreement": obs, "expected_agreement": exp,
            "kappa": kappa, "labels": labels,
            "distribution_a": dict(ma), "distribution_b": dict(mb)}


def agreement_block(r1: dict, r2: dict, label: str) -> dict:
    out = {"comparison": label, "per_criterion": {}}
    for key in CRITERIA:
        field, _ = CRITERIA[key]
        pairs = []
        for iid, v1 in r1["verdicts"].items():
            v2 = r2["verdicts"].get(iid)
            if not v2:
                continue
            pairs.append((v1["normalized"].get(field), v2["normalized"].get(field)))
        block = cohens_kappa(pairs)
        if field == "contradicted_claim_count" and block:
            bin_pairs = [("0" if a == "0" else "1+", "0" if b == "0" else "1+")
                         for a, b in pairs if a is not None and b is not None]
            block["binarized_zero_vs_nonzero"] = cohens_kappa(bin_pairs)
        out["per_criterion"][field] = block
    return out


# --------------------------------------------------------------------------- #
def calibration_sheet(items: List[dict], ids: List[str], out: Path, seed: int) -> None:
    lines = [
        "# 人工校准集 / Human calibration sheet — 2026-08-04",
        "",
        f"15 of the {len(items)} blind-review items, drawn with seed {seed}. "
        "Fill in the four judgments for each item WITHOUT looking at the model's verdicts "
        "(evaluation/results/llm_blind_review/round1.json, round2.json).",
        "",
        "Judgment vocabulary (identical to the one the model was given):",
        "",
        "| field | allowed values |",
        "|---|---|",
        "| hard_constraints_satisfied | yes / no / unclear |",
        "| claims_evidence_supported | yes / partial / no |",
        "| contradicted_claim_count | integer >= 0 |",
        "| directly_actionable | yes / no |",
        "",
        "---",
        "",
    ]
    chosen = [i for i in items if i["item_id"] in ids]
    for it in chosen:
        lines += [f"## {it['item_id']}  (case {it['case_id']}, {it['category']})",
                  "",
                  f"- source run: `{it['source_run_id']}`",
                  f"- tools executed: `{', '.join(it['tools_executed']) or 'none'}`",
                  f"- evidence truncated for the model: {it['evidence_truncated']}",
                  "",
                  "### Request", "", "```", it["request"], "```", ""]
        if it["history"]:
            lines += ["### Earlier turns", "", "```"]
            for t in it["history"]:
                lines.append(f"{t['role']}: {t['content']}")
            lines += ["```", ""]
        lines += ["### Tool evidence", "", "```json", it["evidence_text"][:8000], "```", "",
                  "### Assistant answer", "", "```", it["answer_text"], "```", "",
                  "### Your judgments", "",
                  "| field | value | one-sentence reason |",
                  "|---|---|---|",
                  "| hard_constraints_satisfied |  |  |",
                  "| claims_evidence_supported |  |  |",
                  "| contradicted_claim_count |  |  |",
                  "| directly_actionable |  |  |",
                  "", "---", ""]
    out.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--grader-inputs", nargs="+", required=True)
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--cases", default=str(REPO_ROOT / "evaluation/benchmark/cases.jsonl"))
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--sample-seed", type=int, default=20260804)
    p.add_argument("--calibration-n", type=int, default=15)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--gap-ms", type=float, default=300.0)
    p.add_argument("--progress-log", default=str(REPO_ROOT / "PROGRESS.log"))
    p.add_argument("--third-round", action="store_true",
                   help="also run a cross-model round with ModelRouter purpose='pro'")
    p.add_argument("--dry-run", action="store_true",
                   help="draw the sample and build the items, make NO model call")
    a = p.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prog_path = Path(a.progress_log)

    def progress(msg: str) -> None:
        with prog_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} [B] {msg}\n")

    # env bootstrap: reuse the app's own loader (no new config layer, no key copy)
    import tempfile
    from evaluation.run_benchmark import _bootstrap_env
    _bootstrap_env(Path(tempfile.mkdtemp(prefix="rc_blind_")), out / "_judge_events.jsonl")
    if not os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") == "offline-eval-placeholder":
        progress("ABORT: DEEPSEEK_API_KEY not loaded")
        return 2

    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
    cases_by_id = {c["case_id"]: c for c in cases}

    grader_rows: Dict[str, dict] = {}
    for path in a.grader_inputs:
        pth = Path(path)
        if not pth.exists():
            continue
        for line in pth.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            grader_rows[d.get("run_id")] = d

    runs_by_key: Dict[str, dict] = {}
    for path in a.runs:
        pth = Path(path)
        if not pth.exists():
            continue
        for line in pth.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            runs_by_key[d.get("ab_run_key")] = d

    available = {k.split("#")[0] for k, v in runs_by_key.items()
                 if k.endswith("#routed_models") and v.get("ab_ok")
                 and k in grader_rows}
    sample = draw_sample(cases, available, a.n, a.sample_seed)
    items = build_items(sample["case_ids"], cases_by_id, grader_rows, runs_by_key)
    sample["n_items_built"] = len(items)
    sample["base45_vs_ext53"] = _split_provenance(sample["case_ids"])
    (out / "sample.json").write_text(json.dumps(sample, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    (out / "items.json").write_text(json.dumps(items, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    progress(f"sample drawn: n={len(items)} seed={a.sample_seed} "
             f"cats={sample['category_counts']}")
    if a.dry_run:
        print(json.dumps({"n_items": len(items), "sample": sample}, indent=2,
                         ensure_ascii=False))
        return 0

    async def go():
        r1 = await run_round(items, purpose="judge", order_seed=a.sample_seed + 1,
                             criterion_seed=a.sample_seed + 2, nonce="RC-B-R1",
                             concurrency=a.concurrency, gap_ms=a.gap_ms,
                             progress=progress, tag="round1")
        (out / "round1.json").write_text(json.dumps(r1, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        r2 = await run_round(items, purpose="judge", order_seed=a.sample_seed + 77,
                             criterion_seed=a.sample_seed + 99, nonce="RC-B-R2",
                             concurrency=a.concurrency, gap_ms=a.gap_ms,
                             progress=progress, tag="round2")
        (out / "round2.json").write_text(json.dumps(r2, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        agree = {"same_model_two_rounds_self_consistency":
                 agreement_block(r1, r2, "round1 vs round2 (SAME model deepseek-v4-flash, "
                                         "two independent rounds — SELF-CONSISTENCY, "
                                         "NOT inter-rater reliability)")}
        if a.third_round:
            r3 = await run_round(items, purpose="pro", order_seed=a.sample_seed + 555,
                                 criterion_seed=a.sample_seed + 777, nonce="RC-B-R3",
                                 concurrency=a.concurrency, gap_ms=a.gap_ms,
                                 progress=progress, tag="round3pro")
            (out / "round3_pro.json").write_text(
                json.dumps(r3, indent=2, ensure_ascii=False), encoding="utf-8")
            agree["cross_model_round1_vs_round3"] = agreement_block(
                r1, r3, "round1 (deepseek-v4-flash) vs round3 (deepseek-v4-pro) — "
                        "CROSS-MODEL agreement, same vendor")
        agree["verdict_summary"] = _verdict_summary(r1, r2)
        (out / "agreement.json").write_text(json.dumps(agree, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
        return r1, r2

    asyncio.run(go())

    rng = random.Random(a.sample_seed + 4242)
    cal_ids = sorted(rng.sample([i["item_id"] for i in items],
                                min(a.calibration_n, len(items))))
    calibration_sheet(items, cal_ids, out / "human_calibration_sheet.md", a.sample_seed + 4242)
    progress(f"calibration sheet written: {len(cal_ids)} items")
    progress("Experiment B DONE")
    return 0


def _split_provenance(case_ids: List[str]) -> dict:
    base = REPO_ROOT / "evaluation/benchmark/cases_base45.jsonl"
    base_ids = set()
    if base.exists():
        for line in base.open(encoding="utf-8"):
            if line.strip():
                base_ids.add(json.loads(line)["case_id"])
    in_base = [c for c in case_ids if c in base_ids]
    return {"base45": len(in_base), "ext53": len(case_ids) - len(in_base),
            "base45_ids": in_base}


def _verdict_summary(r1: dict, r2: dict) -> dict:
    out = {}
    for rname, r in (("round1", r1), ("round2", r2)):
        counts = {}
        for key in CRITERIA:
            field, _ = CRITERIA[key]
            counts[field] = dict(Counter(v["normalized"].get(field)
                                         for v in r["verdicts"].values()))
        out[rname] = {"n_verdicts": len(r["verdicts"]), "counts": counts}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
