from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")


def new_request_id(value: str | None = None) -> str:
    return value or uuid.uuid4().hex


@contextlib.contextmanager
def request_context(request_id: str, user_id: str) -> Iterator[None]:
    request_token = request_id_var.set(request_id)
    user_token = user_id_var.set(user_id)
    try:
        yield
    finally:
        request_id_var.reset(request_token)
        user_id_var.reset(user_token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "user_id": getattr(record, "user_id", user_id_var.get()),
        }
        for key in ("node", "tool", "latency_ms", "cache_hit", "input_tokens", "output_tokens"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


@contextlib.contextmanager
def node_span(logger: logging.Logger, node: str, **attributes: Any) -> Iterator[None]:
    """Local structured span; upgrades to OTel without changing node call sites."""
    started = time.perf_counter()
    logger.info("node.start", extra={"node": node, **attributes})
    try:
        yield
    except Exception:
        logger.exception(
            "node.error",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **attributes},
        )
        raise
    else:
        logger.info(
            "node.end",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **attributes},
        )
