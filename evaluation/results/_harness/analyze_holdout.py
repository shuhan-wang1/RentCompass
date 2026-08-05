"""Analysis for the held-out v2 batch: deterministic grading + model-blind-review reading.

Everything reported here carries a raw numerator and denominator. Rates that come from the
judges are labelled MODEL BLIND REVIEW and never called accuracy, human review or
inter-rater reliability.

Metrics (frozen in PROGRESS.log 2026-08-05 07:20, plus the disclosed 07:26 amendment):

  D1  hard-constraint satisfaction, unit = (case, constraint) pair on the "satisfaction"
      branch. FAIL when the answer surfaces a record that breaks the constraint.
      READ IT AS: "the answer put no non-compliant option in front of the user at all".
      NOT CV-eligible — see the 07:26 amendment: an answer that names the compliant option
      AND transparently lists the rejects under a "did not make the cut" heading is scored
      FAIL here, which diverges from the case's own frozen correct_completion.
  D2  case level: every decidable constraint on the case passes.
  D3  D1 split by semantic slot.
  D5  reference-calculation match on the 20 calculation cases.
  D4  (secondary, diagnostic) no-result cases: the answer states no GBP figure other than
      the user's own. Known in advance to also trip on a suggested alternative budget, so
      offending strings are printed.
  D6  correct-option identification: the answer NAMES at least one frozen listing that
      satisfies EVERY stated condition. Added after the 6-case smoke and disclosed there;
      the PRIMARY denominator therefore EXCLUDES the two retrieval_hard cases whose
      answers had been seen when it was defined (HO2-001, HO2-023) -> n = 33.

Bootstrap: cluster bootstrap, resampling unit = CASE, 2000 resamples, seed 20260805,
95% percentile intervals.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import constraint_schema_v2 as v2  # noqa: E402

FIX = REPO / "evaluation" / "benchmark" / "fixtures"
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260805
D6_SEEN_AT_SMOKE = ("HO2-001", "HO2-023")
LLM_CALLS: Dict[str, int] = {}

CRIT = ["hard_constraints_satisfied", "claims_evidence_supported",
        "contradicted_claim_count", "task_completed_correctly"]
JUDGEABLE_EXCLUDE = {"not_applicable", "cannot_assess", None}


# --------------------------------------------------------------------------- #
def bootstrap_ci(units: List, stat, n=BOOTSTRAP_N, seed=BOOTSTRAP_SEED):
    """Cluster bootstrap over CASES. ``stat`` maps a list of units -> float or None."""
    if not units:
        return None
    rng = random.Random(seed)
    point = stat(units)
    if point is None:
        return None
    draws = []
    k = len(units)
    for _ in range(n):
        sample = [units[rng.randrange(k)] for _ in range(k)]
        v = stat(sample)
        if v is not None:
            draws.append(v)
    if not draws:
        return {"point": point, "lo": None, "hi": None, "n_units": k}
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return {"point": point, "lo": lo, "hi": hi, "n_units": k,
            "resamples": n, "seed": seed}


def _rate(units):
    num = sum(u[0] for u in units)
    den = sum(u[1] for u in units)
    return (num / den) if den else None


def _binom_cdf(k: int, n: int, p: float) -> float:
    from math import comb
    return sum(comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(0, k + 1))


def exact_binomial_ci(k: int, n: int, alpha: float = 0.05) -> dict | None:
    """Clopper-Pearson interval, by bisection on the binomial CDF (no SciPy in the image).

    Needed because a cluster bootstrap DEGENERATES at a boundary: every resample of a
    33/33 result is still 33/33, so the percentile interval comes out [100%, 100%], which
    reads as certainty the data do not support. The exact interval on 33/33 is
    [89.4%, 100%] — that is the honest statement, and it is the one quoted.
    """
    if n <= 0:
        return None
    lo = 0.0 if k == 0 else _bisect(lambda p: 1 - _binom_cdf(k - 1, n, p) - alpha / 2,
                                    0.0, 1.0)
    hi = 1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p) - alpha / 2, 0.0, 1.0)
    return {"point": k / n, "lo": lo, "hi": hi, "k": k, "n": n,
            "method": "Clopper-Pearson exact binomial, 95%"}


def _bisect(f, a: float, b: float, iters: int = 200) -> float:
    fa = f(a)
    for _ in range(iters):
        m = (a + b) / 2
        fm = f(m)
        if (fa < 0) == (fm < 0):
            a, fa = m, fm
        else:
            b = m
    return (a + b) / 2


# --------------------------------------------------------------------------- #
def _with_exact(units, ci):
    """Attach the exact binomial interval and flag a degenerate bootstrap."""
    k = sum(u[0] for u in units)
    n = sum(u[1] for u in units)
    out = {"bootstrap_ci": ci, "exact_binomial_ci": exact_binomial_ci(k, n)}
    if ci and ci.get("lo") is not None and ci["lo"] == ci["hi"]:
        out["bootstrap_degenerate"] = (
            "every resample reproduces the point estimate (the rate sits on a boundary), "
            "so the percentile interval collapses to a point and must NOT be quoted as a "
            "confidence statement — quote the exact binomial interval instead")
    return out


def deterministic(cases: Dict[str, dict], answers: Dict[str, str]) -> dict:
    per_case = {}
    for cid, case in cases.items():
        ans = answers.get(cid)
        if ans is None:
            continue
        rows = []
        for con in v2.user_hard_constraints(case):
            branch = v2.constraint_branch(case, con, FIX)
            res = v2.evaluate_constraint(case, con, ans, FIX)
            rows.append({"slot": v2.slot_of(con), "type": con["type"], "branch": branch,
                         "verdict": res["verdict"], "offenders": res["offenders"],
                         "n_surfaced": res["n_surfaced"]})
        per_case[cid] = {"task_category": case["task_category"], "constraints": rows}

    # ---- D1 : (case, constraint) pairs on the satisfaction branch ---- #
    d1_units, d1_pairs = [], []
    for cid, rec in per_case.items():
        num = den = 0
        for r in rec["constraints"]:
            if r["branch"] != "satisfaction":
                continue
            if r["verdict"] not in (v2.PASS, v2.FAIL):
                continue                       # not_surfaced -> behaviour, not this denom
            den += 1
            num += 1 if r["verdict"] == v2.PASS else 0
            d1_pairs.append({"case_id": cid, "slot": r["slot"], "verdict": r["verdict"],
                             "offenders": r["offenders"]})
        if den:
            d1_units.append((num, den))

    # ---- D2 : case level ---- #
    d2_units = []
    for cid, rec in per_case.items():
        dec = [r for r in rec["constraints"]
               if r["branch"] == "satisfaction" and r["verdict"] in (v2.PASS, v2.FAIL)]
        if dec:
            d2_units.append((1 if all(r["verdict"] == v2.PASS for r in dec) else 0, 1))

    # ---- D3 : per slot ---- #
    d3 = {}
    for slot in v2.SLOT_MIN_COVERAGE:
        units = []
        by_case = defaultdict(lambda: [0, 0])
        for p in d1_pairs:
            if p["slot"] == slot:
                by_case[p["case_id"]][1] += 1
                by_case[p["case_id"]][0] += 1 if p["verdict"] == v2.PASS else 0
        units = [tuple(v) for v in by_case.values()]
        d3[slot] = {"numerator": sum(u[0] for u in units),
                    "denominator": sum(u[1] for u in units),
                    "n_cases": len(units),
                    "ci": bootstrap_ci(units, _rate) if units else None}

    # ---- D6 : correct-option identification ---- #
    def d6_for(cid) -> int | None:
        case = cases[cid]
        if case["task_category"] != "retrieval_hard":
            return None
        listings = v2.fixture_listings(case, FIX)
        if not listings:
            return None
        cons = [c for c in v2.user_hard_constraints(case)
                if v2.TYPES[c["type"]]["scope"] == "listing"]
        commutes = [c for c in v2.user_hard_constraints(case)
                    if v2.TYPES[c["type"]]["scope"] == "tool_result"]
        legs = {}
        for c in commutes:
            for rec in v2._records_for(c, case, FIX):
                legs.setdefault(str(rec.get("origin_uid") or "").casefold(), []).append(
                    (rec, c))
        good = []
        for l in listings:
            ok = True
            for c in cons:
                verdict = v2.TYPES[c["type"]]["predicate"](l, c)
                if verdict == v2.FAIL:
                    ok = False
                    break
            if ok and commutes:
                key = str(l.get("uid_token") or "").casefold() or (
                    v2.listing_tokens(l)[0] if v2.listing_tokens(l) else "")
                for rec, c in legs.get(key, []):
                    if v2.TYPES[c["type"]]["predicate"](rec, c) == v2.FAIL:
                        ok = False
            if ok:
                good.append(l)
        if not good:
            return None                        # no compliant option -> not in this denom
        named = v2.surfaced_listings(answers[cid], good)
        return 1 if named else 0

    d6_all, d6_primary = [], []
    d6_detail = {}
    for cid in per_case:
        v = d6_for(cid)
        if v is None:
            continue
        d6_detail[cid] = v
        d6_all.append((v, 1))
        if cid not in D6_SEEN_AT_SMOKE:
            d6_primary.append((v, 1))

    # ---- D5 : reference-calculation match ---- #
    d5_units, d5_detail = [], {}
    for cid, case in cases.items():
        if case["task_category"] != "calculation" or cid not in answers:
            continue
        rc = case.get("reference_calculations") or {}
        primary = None
        for key in ("move_in_total", "deposit", "monthly_rent", "weekly_rent", "annual_rent"):
            if key in rc:
                primary = rc[key]["result"]
                break
        if primary is None:
            continue
        nums = [float(x.replace(",", "")) for x in
                re.findall(r"[0-9][0-9,]*(?:\.[0-9]+)?", answers[cid])]
        tol = max(1.0, abs(primary) * 0.01)
        hit = any(abs(n - primary) <= tol for n in nums)
        d5_units.append((1 if hit else 0, 1))
        d5_detail[cid] = {"expected": primary, "matched": hit,
                          "llm_calls": LLM_CALLS.get(cid)}

    # ---- D4 : no-result cases must state no other GBP figure ---- #
    d4_units, d4_detail = [], {}
    for cid, case in cases.items():
        if cid not in answers or not case.get("fixture"):
            continue
        if not str(case["task_category"]).startswith("retrieval"):
            continue
        if v2.fixture_listings(case, FIX):
            continue
        allowed = {float(c["value"]) for c in v2.user_hard_constraints(case)
                   if isinstance(c.get("value"), (int, float))}
        found = [float(x.replace(",", "")) for x in
                 re.findall(r"£\s*([0-9][0-9,]*(?:\.[0-9]+)?)", answers[cid])]
        bad = [f for f in found if not any(abs(f - a) < 1 for a in allowed)]
        d4_units.append((1 if not bad else 0, 1))
        d4_detail[cid] = bad

    return {
        "per_case": per_case,
        "D1_hard_constraint_satisfaction": {
            "definition": ("unit = (case, constraint) pair whose frozen evidence carries "
                           "both a satisfying and a violating record; PASS = the answer "
                           "surfaced no violating record"),
            "read_as": ("the answer put no non-compliant option in front of the user AT "
                        "ALL; an answer that names the compliant option and then lists the "
                        "rejects transparently is scored FAIL here"),
            "cv_eligible": False,
            "numerator": sum(u[0] for u in d1_units),
            "denominator": sum(u[1] for u in d1_units),
            "n_cases": len(d1_units),
            "ci": bootstrap_ci(d1_units, _rate),
            "failures": [p for p in d1_pairs if p["verdict"] == v2.FAIL],
        },
        "D2_case_all_constraints_pass": {
            "numerator": sum(u[0] for u in d2_units), "denominator": len(d2_units),
            "ci": bootstrap_ci(d2_units, _rate),
            "intervals": _with_exact(d2_units, bootstrap_ci(d2_units, _rate)), "cv_eligible": False,
        },
        "D3_per_slot": d3,
        "D6_correct_option_identified": {
            "definition": ("unit = retrieval_hard case whose frozen evidence contains at "
                           "least one listing satisfying EVERY stated condition; PASS = the "
                           "answer names such a listing (street token or exact price)"),
            "added": "after the 6-case smoke; disclosed in PROGRESS.log 2026-08-05 07:26",
            "primary": {"numerator": sum(u[0] for u in d6_primary),
                        "denominator": len(d6_primary),
                        "excludes": list(D6_SEEN_AT_SMOKE),
                        "ci": bootstrap_ci(d6_primary, _rate),
                        "intervals": _with_exact(d6_primary,
                                                 bootstrap_ci(d6_primary, _rate))},
            "all_cases": {"numerator": sum(u[0] for u in d6_all),
                          "denominator": len(d6_all),
                          "ci": bootstrap_ci(d6_all, _rate),
                          "intervals": _with_exact(d6_all, bootstrap_ci(d6_all, _rate))},
            "per_case": d6_detail,
        },
        "D5_reference_calculation_match": {
            "numerator": sum(u[0] for u in d5_units), "denominator": len(d5_units),
            "ci": bootstrap_ci(d5_units, _rate),
            "intervals": _with_exact(d5_units, bootstrap_ci(d5_units, _rate)),
            "split_by_who_answered": {
                "deterministic_module_zero_llm_calls": {
                    "numerator": sum(1 for c, d in d5_detail.items()
                                     if not LLM_CALLS.get(c) and d["matched"]),
                    "denominator": sum(1 for c in d5_detail if not LLM_CALLS.get(c)),
                    "note": ("these turns were answered by app/core/tenancy_reference.py, "
                             "a deterministic non-LLM module: 0 LLM calls, ~20 ms")},
                "model_answered": {
                    "numerator": sum(1 for c, d in d5_detail.items()
                                     if LLM_CALLS.get(c) and d["matched"]),
                    "denominator": sum(1 for c in d5_detail if LLM_CALLS.get(c))},
            },
            "per_case": d5_detail,
        },
        "D4_no_result_no_other_money": {
            "secondary_diagnostic_only": True,
            "numerator": sum(u[0] for u in d4_units), "denominator": len(d4_units),
            "ci": bootstrap_ci(d4_units, _rate),
            "intervals": _with_exact(d4_units, bootstrap_ci(d4_units, _rate)), "offending_figures": d4_detail,
        },
    }


# --------------------------------------------------------------------------- #
def agreement(a: dict, b: dict, label: str) -> dict:
    out = {"comparison": label, "note": ("MODEL vs MODEL. Observed agreement is the "
                                         "headline; kappa is auxiliary and degenerates "
                                         "when one category dominates."),
           "overall": {}, "per_task_class": {}}
    classes = {i: v["task_class"] for i, v in a.items()}
    for f in CRIT:
        pairs = [(a[i]["normalized"].get(f), b[i]["normalized"].get(f))
                 for i in a if i in b]
        out["overall"][f] = _kappa(pairs)
        per = {}
        for c in ("retrieval_hard", "retrieval_soft", "calculation", "memory", "clarify"):
            p = [(a[i]["normalized"].get(f), b[i]["normalized"].get(f))
                 for i in a if i in b and classes[i] == c]
            per[c] = _kappa(p)
        out["per_task_class"][f] = per
    return out


def _kappa(pairs) -> dict | None:
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    n = len(pairs)
    if n == 0:
        return None
    obs = sum(1 for x, y in pairs if x == y) / n
    ma, mb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    labs = {*ma, *mb}
    exp = sum((ma[l] / n) * (mb[l] / n) for l in labs)
    top = max(max(ma.values()), max(mb.values())) / n
    return {"n": n, "observed_agreement": obs, "expected_agreement": exp,
            "kappa": None if exp >= 1.0 else (obs - exp) / (1 - exp),
            "prevalence_degeneration": bool(top >= 0.90),
            "prevalence_note": ("one category holds >=90% of the mass; kappa's denominator "
                                "collapses and the value is unstable — report observed "
                                "agreement and raw counts instead"
                                if top >= 0.90 else None),
            "distribution_a": dict(ma), "distribution_b": dict(mb)}


def judged_rates(rounds: Dict[str, dict]) -> dict:
    """For each round x criterion: the positive rate among TRULY JUDGEABLE items, with
    both a case-level cluster bootstrap and an exact binomial interval. Every entry keeps
    its raw numerator and denominator — a bare percentage is never emitted."""
    POSITIVE = {"hard_constraints_satisfied": {"yes"},
                "claims_evidence_supported": {"yes"},
                "task_completed_correctly": {"yes"},
                "contradicted_claim_count": {"0"}}
    out = {}
    for f in CRIT:
        per = {}
        for name, verds in rounds.items():
            units = []
            for v in verds.values():
                lab = v["normalized"].get(f)
                if lab is None or lab in ("not_applicable", "cannot_assess") or (
                        isinstance(lab, str) and lab.startswith("OUT_OF_VOCAB")):
                    continue
                units.append((1 if lab in POSITIVE[f] else 0, 1))
            if not units:
                per[name] = None
                continue
            ci = bootstrap_ci(units, _rate)
            per[name] = {"positive_label": sorted(POSITIVE[f]),
                         "numerator": sum(u[0] for u in units),
                         "denominator": len(units),
                         "ci": ci, "intervals": _with_exact(units, ci)}
        out[f] = per
    return out


def judgeable(rounds: Dict[str, dict]) -> dict:
    out = {}
    for f in CRIT:
        per_round = {}
        for name, verds in rounds.items():
            labs = [v["normalized"].get(f) for v in verds.values()]
            na = sum(1 for x in labs if x == "not_applicable")
            ca = sum(1 for x in labs if x == "cannot_assess")
            miss = sum(1 for x in labs if x is None)
            oov = sum(1 for x in labs if isinstance(x, str) and x.startswith("OUT_OF_VOCAB"))
            per_round[name] = {"n_items": len(labs),
                               "truly_judgeable_n": len(labs) - na - ca - miss - oov,
                               "not_applicable": na, "cannot_assess": ca,
                               "unparsed_or_missing": miss, "out_of_vocab": oov,
                               "raw_counts": dict(Counter(labs))}
        out[f] = per_round
    return out


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default=str(REPO /
                   "evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl"))
    p.add_argument("--grader-inputs", nargs="+", required=True)
    p.add_argument("--review-dir", required=True)
    p.add_argument("--runs", nargs="*", default=[])
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)

    cases = {json.loads(l)["case_id"]: json.loads(l)
             for l in open(a.cases, encoding="utf-8") if l.strip()}
    answers, seen = {}, set()
    for path in a.grader_inputs:
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                d = json.loads(line)
                if d["case_id"] in seen:
                    continue
                seen.add(d["case_id"])
                answers[d["case_id"]] = (d.get("grader_input") or {}).get("final_answer") or ""

    for path in a.runs:
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                d = json.loads(line)
                LLM_CALLS[d["case_id"]] = d.get("llm_calls") or 0

    det = deterministic(cases, answers)

    rounds = {}
    for name in ("round1", "round2", "round3_pro"):
        f = Path(a.review_dir) / f"{name}.jsonl"
        if not f.exists():
            continue
        rounds[name] = {json.loads(l)["item_id"]: json.loads(l)
                        for l in f.open(encoding="utf-8") if l.strip()}

    review = {"not_human_review": ("all labels below are produced by MODELS; never report "
                                   "them as human review, answer accuracy or inter-rater "
                                   "reliability"),
              "judgeable_counts": judgeable(rounds),
              "rates_among_judgeable": judged_rates(rounds),
              "unparsed_items": {n: [i for i, r in verds.items()
                                     if r.get("parsed") is None]
                                 for n, verds in rounds.items()},
              "raw_label_counts": {n: {f: dict(Counter(v["normalized"].get(f)
                                                       for v in verds.values()))
                                       for f in CRIT}
                                   for n, verds in rounds.items()},
              "judge_failures": {n: [f"{i}:{f}={v}" for i, rec in verds.items()
                                     for f, v in rec["normalized"].items()
                                     if isinstance(v, str) and v.startswith("OUT_OF_VOCAB")]
                                 for n, verds in rounds.items()}}
    if "round1" in rounds and "round2" in rounds:
        review["same_model_two_rounds"] = agreement(
            rounds["round1"], rounds["round2"],
            "round1 vs round2 (SAME model deepseek-v4-flash) — SELF-CONSISTENCY, "
            "not inter-rater reliability")
    if "round1" in rounds and "round3_pro" in rounds:
        review["cross_model"] = agreement(
            rounds["round1"], rounds["round3_pro"],
            "round1 (deepseek-v4-flash) vs round3 (deepseek-v4-pro) — cross-model, "
            "same vendor")

    # contradicted_claim_count reported as raw 0 / 1 / 2+ counts
    contra = {}
    for n, verds in rounds.items():
        c = Counter()
        for v in verds.values():
            raw = v["normalized"].get("contradicted_claim_count")
            if raw in (None,) or (isinstance(raw, str) and raw.startswith("OUT_OF_VOCAB")):
                c["unparsed"] += 1
            elif raw == "cannot_assess":
                c["cannot_assess"] += 1
            elif raw == "0":
                c["0"] += 1
            elif raw == "1":
                c["1"] += 1
            else:
                c["2+"] += 1
        contra[n] = dict(c)
    review["contradicted_claim_count_raw"] = contra

    cost = {}
    for path in a.runs:
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                d = json.loads(line)
                cost.setdefault("n_runs", 0)
                cost["n_runs"] += 1
                cost["llm_calls"] = cost.get("llm_calls", 0) + (d.get("llm_calls") or 0)
                cost["tokens_in"] = cost.get("tokens_in", 0) + (d.get("tokens_in") or 0)
                cost["tokens_out"] = cost.get("tokens_out", 0) + (d.get("tokens_out") or 0)
                cost["cost_usd"] = round(cost.get("cost_usd", 0.0) + (d.get("cost_usd") or 0.0), 6)
                cost["failures"] = cost.get("failures", 0) + (0 if d.get("ab_ok") else 1)
                cost["tool_calls"] = cost.get("tool_calls", 0) + (d.get("tool_calls") or 0)
                cost["tool_fail"] = cost.get("tool_fail", 0) + (d.get("tool_fail") or 0)
                cost.setdefault("latencies", []).append(d.get("ab_wall_ms") or 0)
    if cost.get("latencies"):
        lat = sorted(cost.pop("latencies"))
        cost["latency_ms"] = {"mean": sum(lat) / len(lat),
                              "p50": lat[len(lat) // 2],
                              "p95": lat[min(len(lat) - 1, int(0.95 * (len(lat) - 1)))]}

    report = {"batch": "held_out_v2", "deterministic": det, "model_blind_review": review,
              "operations": cost,
              "bootstrap": {"unit": "case", "resamples": BOOTSTRAP_N,
                            "seed": BOOTSTRAP_SEED, "interval": "95% percentile"}}
    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")

    def fmt(d):
        if not d or d.get("denominator") in (0, None):
            return "n/a"
        ci = d.get("ci") or {}
        s = f"{d['numerator']}/{d['denominator']} ({100*d['numerator']/d['denominator']:.1f}%)"
        if ci.get("lo") is not None:
            s += f"  boot [{100*ci['lo']:.1f}%, {100*ci['hi']:.1f}%]"
        ex = (d.get("intervals") or {}).get("exact_binomial_ci")
        if ex:
            s += f"  exact [{100*ex['lo']:.1f}%, {100*ex['hi']:.1f}%]"
        if (d.get("intervals") or {}).get("bootstrap_degenerate"):
            s += "  (bootstrap degenerate)"
        return s

    print("D1 hard-constraint satisfaction :", fmt(det["D1_hard_constraint_satisfaction"]))
    print("D2 case all-constraints-pass   :", fmt(det["D2_case_all_constraints_pass"]))
    print("D6 correct option (primary n33):", fmt(det["D6_correct_option_identified"]["primary"]))
    print("D6 correct option (all)        :", fmt(det["D6_correct_option_identified"]["all_cases"]))
    print("D5 reference calculation match :", fmt(det["D5_reference_calculation_match"]))
    print("D4 no-result no other money    :", fmt(det["D4_no_result_no_other_money"]))
    print("\nper slot:")
    for s, d in det["D3_per_slot"].items():
        print(f"  {s:18s}", fmt(d))
    print("\njudgeable n per criterion (round1):")
    for f, per in review["judgeable_counts"].items():
        if "round1" in per:
            r = per["round1"]
            print(f"  {f:28s} judgeable {r['truly_judgeable_n']}/{r['n_items']} "
                  f"(N/A {r['not_applicable']}, cannot_assess {r['cannot_assess']}, "
                  f"oov {r['out_of_vocab']})")
    print("\nrates among judgeable (numerator/denominator, exact 95% CI):")
    for f, per in review["rates_among_judgeable"].items():
        for n, d in per.items():
            if d:
                ex = d["intervals"]["exact_binomial_ci"]
                print(f"  {f:28s} {n:11s} {d['numerator']}/{d['denominator']} "
                      f"({100*d['numerator']/d['denominator']:.1f}%) "
                      f"exact [{100*ex['lo']:.1f}%, {100*ex['hi']:.1f}%]")
    print("\noperations:", json.dumps({k: v for k, v in cost.items()}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
