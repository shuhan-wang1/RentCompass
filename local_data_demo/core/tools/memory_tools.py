"""
Memory tools — expose the shared long-term AgentMemory as tools so the agent (and any
sub-agent/tool, via MCP) can read & write it. This is the "memory-as-tools" pattern from
Letta/LangMem. The underlying store (rag.agent_memory) is shared on disk across the web
process and the MCP subprocess.
"""
from core.tool_system import Tool


def _mem():
    from rag.agent_memory import get_agent_memory
    return get_agent_memory()


async def recall_memory_impl(query: str, n: int = 6,
                             session_id: str = "default", user_id: str = "default") -> dict:
    mems = _mem().retrieve(query, session_id=session_id, user_id=user_id, n=int(n))
    return {
        "success": True,
        "count": len(mems),
        "memories": mems,
        "formatted": _mem().format_for_prompt(mems),
    }


async def remember_impl(content: str, kind: str = "semantic",
                        session_id: str = "default", user_id: str = "default") -> dict:
    mtype = kind if kind in ("episodic", "semantic", "reflection") else "semantic"
    mid = _mem().add(content, mtype, session_id=session_id, user_id=user_id)
    return {"success": bool(mid), "id": mid, "stored": content, "mtype": mtype}


recall_memory_tool = Tool(
    name="recall_memory",
    description=(
        "Recall what is known about the CURRENT user from long-term memory — their stated "
        "preferences, budget, target destination/university, hard constraints, areas to avoid, "
        "and earlier requests. Use this when answering a follow-up that may depend on something "
        "the user said in a previous turn or session. Returns the most relevant remembered facts."
    ),
    func=recall_memory_impl,
    parameters={
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'what to recall about the user'},
            'n': {'type': 'integer', 'description': 'max number of memories to return', 'default': 6},
            'user_id': {'type': 'string', 'description': 'current user identity', 'default': 'default'},
            'session_id': {'type': 'string', 'description': 'current conversation identity', 'default': 'default'},
        },
        'required': ['query'],
    },
    max_retries=1,
)

remember_tool = Tool(
    name="remember",
    description=(
        "Save a durable fact about the CURRENT user to long-term memory (e.g. a preference, a "
        "budget, a fixed destination, or a hard constraint) so it is available in future turns "
        "and future sessions. Only store stable, user-specific facts — not transient chit-chat."
    ),
    func=remember_impl,
    parameters={
        'type': 'object',
        'properties': {
            'content': {'type': 'string', 'description': 'the durable fact to remember about the user'},
            'kind': {'type': 'string', 'description': 'episodic, semantic, or reflection', 'default': 'semantic'},
            'user_id': {'type': 'string', 'description': 'current user identity', 'default': 'default'},
            'session_id': {'type': 'string', 'description': 'current conversation identity', 'default': 'default'},
        },
        'required': ['content'],
    },
    max_retries=1,
)
