from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from uk_rent_agent.evals.metrics import EvalReport, mrr, ndcg_at_k, recall_at_k


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip() and not line.lstrip().startswith("#")]


def run_intent_eval(
    cases: Iterable[dict[str, Any]], classify: Callable[[str], dict[str, Any]]
) -> EvalReport:
    rows = list(cases)
    correct = 0
    slot_correct = 0
    for case in rows:
        result = classify(case["query"])
        correct += result.get("intent") == case["expected_intent"]
        expected_slots = case.get("expected_slots", {})
        slot_correct += all(result.get("slots", {}).get(key) == value for key, value in expected_slots.items())
    total = len(rows) or 1
    return EvalReport({"intent_accuracy": correct / total, "slot_accuracy": slot_correct / total})


def run_retrieval_eval(
    cases: Iterable[dict[str, Any]],
    retrieve: Callable[[dict[str, Any]], list[str]],
    *,
    k: int = 5,
) -> EvalReport:
    rows = list(cases)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for case in rows:
        ranked = retrieve(case)
        relevant = set(case["relevant_ids"])
        recalls.append(recall_at_k(ranked, relevant, k))
        reciprocal_ranks.append(mrr(ranked, relevant))
        ndcgs.append(ndcg_at_k(ranked, relevant, k))
    denominator = len(rows) or 1
    return EvalReport(
        {
            f"recall@{k}": sum(recalls) / denominator,
            "mrr": sum(reciprocal_ranks) / denominator,
            f"ndcg@{k}": sum(ndcgs) / denominator,
        }
    )
