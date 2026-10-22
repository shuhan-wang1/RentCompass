# MCP integration (UK rent tools)

Every tool registered in `create_tool_registry()` is exposed over the
**Model Context Protocol** by `mcp_server.py`, and the agent can consume them
through that server over **stdio** (`USE_MCP_TOOLS=1`). That is currently **14 tools**:

| Group | Tools |
|---|---|
| Search / listings | `search_properties`, `get_property_details`, `compare_or_rank_areas` |
| Transport | `calculate_commute`, `calculate_commute_cost`, `check_transport_cost`, `get_transport_info` |
| Area context | `check_safety`, `search_nearby_pois`, `get_weather`, `web_search` |
| Memory-as-tools | `recall_memory`, `remember` |
| Interaction | `ask_user` (terminal — ends the turn with one clarifying question) |

## Architecture

```
agent tool dispatch
  fc_loop : execute_tools node   (app/core/agent_loop.py)
  legacy  : execute_tool  node   (app/core/langgraph_agent.py)
      │  await tool_provider.execute_tool(name, **params)
      ▼
core/mcp_client.py  MCPToolClient ── duck-types ToolRegistry.execute_tool
      │  stdio (persistent background event-loop thread)   ── fallback ─▶ in-process ToolRegistry
      ▼
mcp_server.py  (MCP stdio server)
      │  reuses core.tool_system.create_tool_registry()    ← single source of truth
      ▼
core/tools/*  (the tool implementations)
```

`MCPToolClient` exposes exactly the one method the agent uses on the registry
(`execute_tool`), so **neither graph changes**. The agent just receives the MCP
client instead of the registry. If the MCP server is unavailable or a call fails,
the client transparently **falls back** to the in-process `ToolRegistry`.

The fc loop additionally asks the provider for `list_specs()` to bind tools to
the model; a provider without that method is wrapped in an adapter that derives
the specs from the registry's `Tool` objects, so both paths stay single-sourced.

## Running (web app)

`app.py` always builds the in-process registry. With `USE_MCP_TOOLS=1` it also
starts an `MCPToolClient` (which spawns `python mcp_server.py` over stdio) and hands
that to the agent, falling back to the registry if the server does not connect. The
default, `USE_MCP_TOOLS=0`, uses the in-process registry directly:

```bash
USE_MCP_TOOLS=1 python app/app.py
```

`AGENT_ARCH=manager_v1` with `MANAGER_V1_SPECIALISTS=1` requires `USE_MCP_TOOLS=0`; the
combination is refused at config load.

## Running the MCP server standalone

```bash
cd app
python mcp_server.py        # speaks MCP over stdio
```

It has the same runtime requirements as the app: an LLM provider configured in
`.env` — a `DEEPSEEK_API_KEY` by default, or a running Ollama if you set
`LLM_PROVIDER=ollama`. The map stack (geocoding / transport / POIs) uses free,
key-free providers (Postcodes.io, Nominatim, TfL, Overpass); `GOOGLE_MAPS_API_KEY`
is optional.

## Using the tools from an external MCP client (e.g. Claude Desktop)

Add to the client's MCP server config:

```json
{
  "mcpServers": {
    "uk-rent-tools": {
      "command": "<path-to-python>",
      "args": ["mcp_server.py"],
      "cwd": "<repo>/app"
    }
  }
}
```

## Result envelope

`call_tool` returns a JSON envelope as text content:

```json
{"success": true, "data": <tool data>, "error": null,
 "tool_name": "check_safety", "execution_time_ms": 812.4}
```

The client parses this back into the project's `ToolResult`, preserving the
structured `data` the agent's `format_output` node expects.

## Dependencies

The Python MCP SDK is a runtime dependency in `pyproject.toml`, pinned **`mcp<2`**.

mcp 2.0.0 (2026-07-28) removed the low-level `Server` decorator API this server is
built on — `@server.list_tools()` raises `AttributeError` at import — so the major
is bounded until that migration is done deliberately. An unbounded major is what
turned CI red with no code change.
