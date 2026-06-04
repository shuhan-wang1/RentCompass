"""
MCP server for the UK rental recommendation tools.

Exposes the same tools the LangGraph agent uses (search_properties, check_safety,
calculate_commute, calculate_commute_cost, check_transport_cost, search_nearby_pois,
get_property_details, web_search, get_weather) over the Model Context Protocol via
**stdio**.

The tool *implementations* are the existing ones in ``core/tools`` (reused through
``core.tool_system.create_tool_registry``), so this server and the in-process agent
share a single source of truth.

Run standalone (speaks MCP over stdio):

    cd local_data_demo
    python mcp_server.py

Register in an external MCP client (e.g. Claude Desktop) with
command=<python>, args=["mcp_server.py"], cwd=<this directory>. See MCP.md.
"""
import sys
from io import TextIOWrapper

# MCP speaks JSON-RPC over *stdout*. The tool/registry code prints diagnostics with
# print(), which would corrupt that protocol channel (and crash on a non-UTF-8
# Windows console). Capture the real stdout for the transport, then route every
# print() to (UTF-8) stderr.
_REAL_STDOUT_BUFFER = sys.stdout.buffer
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.stdout = sys.stderr

import asyncio
import json

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from core.tool_system import create_tool_registry

# Build the registry once (single source of truth shared with the in-process agent).
_registry = create_tool_registry()

# Let web_search fan out to sibling tools, exactly as app.py wires it.
try:
    from core.tools.web_search import set_tool_registry
    set_tool_registry(_registry)
except Exception as _e:  # pragma: no cover - non-fatal
    print(f"[mcp_server] warning: could not wire web_search registry: {_e}")

server = Server("uk-rent-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Advertise every registered tool with its existing OpenAI-format JSON schema."""
    return [
        types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.parameters,
        )
        for tool in _registry.tools.values()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Dispatch to the in-process registry and return a structured JSON envelope.

    The envelope preserves the project's ToolResult shape so the MCP client can
    reconstruct it losslessly: {success, data, error, tool_name, execution_time_ms}.
    """
    result = await _registry.execute_tool(name, **(arguments or {}))
    envelope = {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "tool_name": result.tool_name,
        "execution_time_ms": result.execution_time_ms,
    }
    text = json.dumps(envelope, ensure_ascii=False, default=str)
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    # Hand the MCP transport the REAL stdout (print() was redirected to stderr above).
    real_stdout = anyio.wrap_file(TextIOWrapper(_REAL_STDOUT_BUFFER, encoding="utf-8"))
    async with stdio_server(stdout=real_stdout) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
