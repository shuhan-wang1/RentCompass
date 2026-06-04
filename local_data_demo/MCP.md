# MCP integration (UK rent tools)

The rental tools (`search_properties`, `check_safety`, `calculate_commute`,
`calculate_commute_cost`, `check_transport_cost`, `search_nearby_pois`,
`get_property_details`, `web_search`, `get_weather`) are exposed over the
**Model Context Protocol** by `mcp_server.py`, and the LangGraph agent consumes
them through that server over **stdio**.

## Architecture

```
LangGraph agent (execute_tool node)
      │  await tool_registry.execute_tool(name, **params)
      ▼
core/mcp_client.py  MCPToolClient ── duck-types ToolRegistry.execute_tool
      │  stdio (persistent background event-loop thread)   ── fallback ─▶ in-process ToolRegistry
      ▼
mcp_server.py  (MCP stdio server)
      │  reuses core.tool_system.create_tool_registry()    ← single source of truth
      ▼
core/tools/*  (the 9 tool implementations)
```

`MCPToolClient` exposes exactly the one method the agent uses on the registry
(`execute_tool`), so **`core/langgraph_agent.py` is unchanged**. The agent just
receives the MCP client instead of the registry. If the MCP server is unavailable
or a call fails, the client transparently **falls back** to the in-process
`ToolRegistry`.

## Running (web app)

`app.py` wires this automatically at startup: it builds the in-process registry,
then starts an `MCPToolClient` (which spawns `python mcp_server.py` over stdio) and
hands it to the agent. To force the old in-process path instead, set:

```bash
USE_MCP_TOOLS=0 python app.py
```

## Running the MCP server standalone

```bash
cd local_data_demo
python mcp_server.py        # speaks MCP over stdio
```

It has the same runtime requirements as the app (valid `GOOGLE_MAPS_API_KEY` in
`.env`, Ollama running, etc.).

## Using the tools from an external MCP client (e.g. Claude Desktop)

Add to the client's MCP server config:

```json
{
  "mcpServers": {
    "uk-rent-tools": {
      "command": "<path-to-python>",
      "args": ["mcp_server.py"],
      "cwd": "<repo>/local_data_demo"
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

Adds `mcp` (the Python MCP SDK) to `requirements.txt`.
