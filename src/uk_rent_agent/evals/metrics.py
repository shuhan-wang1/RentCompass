from __future__ import annotations

import math
from dataclasses import dataclass, field


def recall_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 1.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def mrr(ranked_ids: list[str], relevant_ids: set[str]) -> float:
    for rank, identity in enumerate(ranked_ids, 1):
        if identity in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 1.0
    dcg = sum(
        1.0 / math.log2(index + 2)
        for index, identity in enumerate(ranked_ids[:k])
        if identity in relevant_ids
    )
    ideal_count = min(len(relevant_ids), k)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return dcg / ideal if ideal else 0.0


@dataclass
class EvalReport:
    metrics: dict[str, float]
    failures: list[str] = field(default_factory=list)

    def check(self, thresholds: dict[str, float]) -> bool:
        self.failures = [
            f"{name}={self.metrics.get(name, 0):.4f} < {minimum:.4f}"
            for name, minimum in thresholds.items()
            if self.metrics.get(name, 0) < minimum
        ]
        return not self.failures
