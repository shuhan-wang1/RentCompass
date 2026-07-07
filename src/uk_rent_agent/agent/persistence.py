from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CHECKPOINTERS: dict[Path, Any] = {}


def thread_id(user_id: str, session_id: str) -> str:
    return f"{user_id}:{session_id}"


def graph_config(user_id: str, session_id: str, *, request_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id(user_id, session_id)}
    if request_id:
        configurable["request_id"] = request_id
    return {"configurable": configurable, "metadata": {"user_id": user_id, "request_id": request_id}}


def get_sqlite_checkpointer(path: Path) -> Any | None:
    """Return a process-wide SqliteSaver; None keeps optional installs importable."""
    resolved = Path(path).resolve()
    with _LOCK:
        if resolved in _CHECKPOINTERS:
            return _CHECKPOINTERS[resolved]
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:
            return None

        class AsyncCompatibleSqliteSaver(SqliteSaver):
            """Use the locked sync saver in worker threads for LangGraph.ainvoke.

            Unlike AsyncSqliteSaver this connection is not bound to one event loop,
            which matters while the Flask compatibility app is served through ASGI.
            """

            async def aget_tuple(self, config):
                return await asyncio.to_thread(self.get_tuple, config)

            async def alist(self, config, *, filter=None, before=None, limit=None):
                items = await asyncio.to_thread(
                    lambda: list(self.list(config, filter=filter, before=before, limit=limit))
                )
                for item in items:
                    yield item

            async def aput(self, config, checkpoint, metadata, new_versions):
                return await asyncio.to_thread(
                    self.put, config, checkpoint, metadata, new_versions
                )

            async def aput_writes(self, config, writes, task_id, task_path=""):
                await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

            async def adelete_thread(self, thread_id):
                await asyncio.to_thread(self.delete_thread, thread_id)

        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved, check_same_thread=False)
        saver = AsyncCompatibleSqliteSaver(connection)
        if hasattr(saver, "setup"):
            saver.setup()
        _CHECKPOINTERS[resolved] = saver
        return saver
