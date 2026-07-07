from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    """Base for state slices crossing node and tool boundaries."""

    model_config = ConfigDict(extra="allow")


class ToolInvocation(ContractModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    version: str = "1"

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        node_id: str,
        tool: str,
        params: dict[str, Any] | None = None,
        version: str = "1",
    ) -> "ToolInvocation":
        values = params or {}
        canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        raw = f"{run_id}:{node_id}:{tool}:{canonical}".encode("utf-8")
        return cls(
            tool=tool,
            params=values,
            idempotency_key=hashlib.sha256(raw).hexdigest(),
            version=version,
        )


class Listing(ContractModel):
    id: str | None = Field(default=None, validation_alias=AliasChoices("id", "ID"))
    url: str | None = Field(default=None, validation_alias=AliasChoices("url", "URL"))
    address: str | None = Field(default=None, validation_alias=AliasChoices("address", "Address"))
    price: float | str | None = Field(default=None, validation_alias=AliasChoices("price", "Price"))
    source: str | None = Field(default=None, validation_alias=AliasChoices("source", "Source"))
    freshness: float | None = None
    description: str | None = Field(
        default=None,
        validation_alias=AliasChoices("description", "Description", "Enhanced_Description"),
    )


class SourceHealth(ContractModel):
    source: str
    ok: bool
    latency_ms: float = 0
    count: int = 0
    error: str | None = None
    cache_status: Literal["live", "fresh_cache", "stale_cache", "fixture", "skipped"] = "live"
    circuit_open: bool = False


class RetrievalResult(ContractModel):
    listings: list[Listing] = Field(default_factory=list)
    per_source: dict[str, SourceHealth] = Field(default_factory=dict)
    freshness: dict[str, float] = Field(default_factory=dict)


class ScoredListing(ContractModel):
    listing: Listing
    score: float
    reasons: list[str] = Field(default_factory=list)


class DropReason(ContractModel):
    listing_id: str | None = None
    reason: str


class RankedResult(ContractModel):
    ranked: list[ScoredListing] = Field(default_factory=list)
    dropped: list[DropReason] = Field(default_factory=list)


class CriticVerdict(ContractModel):
    grounded: bool
    answered: bool
    retrieval_hit: bool
    issues: list[str] = Field(default_factory=list)
    needs_replan: bool = False


class IntentResult(ContractModel):
    intent: str
    slots: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False


class NodeContract(ContractModel):
    reads: frozenset[str]
    writes: frozenset[str]


NODE_CONTRACTS: dict[str, NodeContract] = {
    "memory_read": NodeContract(reads={"user_query", "user_id"}, writes={"memory_context"}),
    "intent": NodeContract(
        reads={"user_query", "extracted_context", "memory_context"},
        writes={"intent", "slots"},
    ),
    "planner": NodeContract(
        reads={"intent", "slots", "accumulated_search_criteria"}, writes={"plan"}
    ),
    "retrieval_subgraph": NodeContract(reads={"plan"}, writes={"retrieval"}),
    "tool_exec": NodeContract(reads={"plan", "context_tainted"}, writes={"tool_results"}),
    "ranker": NodeContract(
        reads={"retrieval", "tool_results", "user_preferences"}, writes={"ranked"}
    ),
    "critic": NodeContract(
        reads={"ranked", "user_query", "final_response", "tool_raw_data"},
        writes={"verdict", "final_response", "critic_attempts"},
    ),
    "responder": NodeContract(
        reads={"ranked", "verdict", "memory_context"},
        writes={"final_response", "response_type", "tool_data"},
    ),
    "memory_write": NodeContract(
        reads={"user_query", "final_response", "user_id", "idempotency_key"}, writes=set()
    ),
}
