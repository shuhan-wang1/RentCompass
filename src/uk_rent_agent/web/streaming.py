from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

NODE_LABELS = {
    "memory_read": "Reading relevant memory",
    "intent": "Understanding your request",
    "planner": "Planning searches",
    "retrieval_subgraph": "Searching property sources",
    "execute_tool": "Checking live data",
    "ranker": "Ranking matching listings",
    "critic": "Verifying the answer",
    "responder": "Writing the response",
    # Compatibility labels while the legacy graph is incrementally split.
    "decide_tool": "Understanding your request",
    "build_execution_plan": "Planning the work",
    "dispatch_tasks": "Running tasks",
    "gather_wave": "Combining results",
    "generate_response": "Writing the response",
}


def sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def stream_graph_events(graph, state: dict, config: dict) -> AsyncIterator[bytes]:
    """Translate LangGraph v2 events into the stable public SSE protocol."""
    final_state: dict[str, Any] | None = None
    try:
        async for event in graph.astream_events(state, config=config, version="v2"):
            kind = event.get("event")
            name = event.get("name", "")
            data = event.get("data") or {}
            if kind == "on_chain_start" and name in NODE_LABELS:
                yield sse("step", {"node": name, "message": NODE_LABELS[name]})
            elif kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                content = getattr(chunk, "content", "") if chunk is not None else ""
                if content:
                    yield sse("token", {"content": content})
            elif kind == "on_chain_end" and name in {"ranker", "format_output"}:
                output = data.get("output") or {}
                recommendations = output.get("ranked") or output.get("tool_data", {}).get("recommendations")
                if recommendations:
                    yield sse("listings", recommendations)
            if kind == "on_chain_end" and name in {"LangGraph", "StateGraph", "graph"}:
                candidate = data.get("output")
                if isinstance(candidate, dict):
                    final_state = candidate
        if final_state is None and hasattr(graph, "aget_state"):
            snapshot = await graph.aget_state(config)
            final_state = getattr(snapshot, "values", None)
        yield sse("final", final_state or {"status": "complete"})
    except Exception as exc:
        yield sse("error", {"error": f"{type(exc).__name__}: {exc}"})
