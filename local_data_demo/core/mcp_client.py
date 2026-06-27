"""
MCPToolClient - executes tools by calling an MCP server over stdio instead of
running them in-process.

It duck-types the *only* method the LangGraph agent uses on the tool registry:

    async execute_tool(name, **kwargs) -> ToolResult

so ``build_agent_graph(mcp_client)`` works with **no changes** to the agent.

Why a background thread? An MCP stdio ``ClientSession`` is bound to the event loop
it was created in. Flask runs each async view in its own (per-request) event loop,
so a session created at startup cannot be awaited from a request loop. We therefore
own the session in a dedicated background event-loop thread and bridge calls to it
with ``run_coroutine_threadsafe``.

Resilience: if the server is unavailable or a call fails, the client transparently
falls back to an in-process ``ToolRegistry`` (when one is supplied).
"""
import asyncio
import json
import os
import threading
from contextlib import AsyncExitStack
from typing import List, Optional

from core.tool_system import ToolResult


class MCPToolClient:
    def __init__(
        self,
        command: str,
        args: List[str],
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        fallback_registry=None,
        connect_timeout: float = 60.0,
        call_timeout: float = 120.0,
    ):
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.fallback_registry = fallback_registry
        self.connect_timeout = connect_timeout
        # Per-call ceiling. If the background loop / MCP subprocess hangs (e.g. the
        # server dies mid-call), ``wrap_future`` would otherwise await forever and
        # block the request. On timeout we cancel the call and fall back in-process.
        self.call_timeout = call_timeout

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._stack: Optional[AsyncExitStack] = None
        self._tool_names: List[str] = []
        self._ready = threading.Event()
        self._connect_error: Optional[BaseException] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "MCPToolClient":
        """Spawn the background loop and connect the MCP session. Returns self.

        Never raises on connection failure - it logs and leaves the client in
        fallback-only mode so the app can still start.
        """
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-client-loop", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=self.connect_timeout):
            self._connect_error = TimeoutError("MCP connect timed out")
        if self._connect_error is not None:
            print(f"[MCP] connect failed: {self._connect_error!r}; using fallback tools")
        return self

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except BaseException as e:  # noqa: BLE001 - surface any connect failure
            self._connect_error = e
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        # Ensure the server subprocess uses UTF-8 (Windows consoles default to a
        # legacy codec that crashes on emoji prints).
        sub_env = dict(self.env) if self.env else dict(os.environ)
        sub_env.setdefault("PYTHONUTF8", "1")
        sub_env.setdefault("PYTHONIOENCODING", "utf-8")
        params = StdioServerParameters(
            command=self.command, args=self.args, cwd=self.cwd, env=sub_env
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        resp = await self._session.list_tools()
        self._tool_names = [t.name for t in resp.tools]
        print(f"[MCP] connected; {len(self._tool_names)} tools: {', '.join(self._tool_names)}")

    def close(self) -> None:
        """Tear down the session and stop the background loop."""
        if self._loop is None:
            return

        async def _shutdown():
            if self._stack is not None:
                await self._stack.aclose()

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            fut.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def connected(self) -> bool:
        return self._session is not None and self._connect_error is None

    # -------------------------------------------------- duck-typed registry API
    def list_tool_names(self) -> List[str]:
        if self._tool_names:
            return list(self._tool_names)
        if self.fallback_registry is not None:
            return self.fallback_registry.list_tool_names()
        return []

    async def execute_tool(self, name: str, **kwargs) -> ToolResult:
        """Run ``name`` via the MCP server. Falls back to the in-process registry
        on any failure. Safe to call from any event loop."""
        if not self.connected:
            return await self._fallback(name, kwargs, "not connected")
        cfut = None
        try:
            cfut = asyncio.run_coroutine_threadsafe(self._call(name, kwargs), self._loop)
            return await asyncio.wait_for(asyncio.wrap_future(cfut), timeout=self.call_timeout)
        except asyncio.TimeoutError:
            # Background call is hung (commonly a dead MCP subprocess). Detach and
            # fall back so the request event loop is never blocked indefinitely.
            if cfut is not None:
                cfut.cancel()
            return await self._fallback(name, kwargs, f"timed out after {self.call_timeout}s")
        except Exception as e:  # noqa: BLE001
            return await self._fallback(name, kwargs, repr(e))

    async def _call(self, name: str, kwargs: dict) -> ToolResult:
        # Runs INSIDE the background loop that owns the session.
        res = await self._session.call_tool(name, kwargs or {})
        text = self._extract_text(res)
        try:
            env = json.loads(text) if text else {}
        except json.JSONDecodeError:
            env = {"success": not getattr(res, "isError", False), "data": text}
        return ToolResult(
            success=bool(env.get("success", not getattr(res, "isError", False))),
            data=env.get("data"),
            error=env.get("error"),
            execution_time_ms=env.get("execution_time_ms"),
            tool_name=name,
        )

    @staticmethod
    def _extract_text(call_result) -> str:
        parts = []
        for item in getattr(call_result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    async def _fallback(self, name: str, kwargs: dict, why: str) -> ToolResult:
        if self.fallback_registry is not None:
            print(f"[MCP] '{name}' -> in-process fallback ({why})")
            return await self.fallback_registry.execute_tool(name, **kwargs)
        return ToolResult(
            success=False,
            data=None,
            error=f"MCP unavailable ({why}) and no fallback registry",
            tool_name=name,
        )
