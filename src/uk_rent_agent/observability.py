from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")


def new_request_id(value: str | None = None) -> str:
    return value or uuid.uuid4().hex


def pseudonymous_user_ref(value: str | None) -> str:
    """Return a stable log correlation value without emitting the raw identity."""
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return "-"
    secret = (
        os.getenv("LOG_ID_HMAC_KEY", "").strip()
        or os.getenv("CANARY_HMAC_SECRET", "").strip()
        or os.getenv("FLASK_SECRET_KEY", "").strip()
    )
    if secret:
        digest = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    else:
        # User ids are server-generated high-entropy values. Domain separation
        # still prevents the raw value appearing when local development has no key.
        digest = hashlib.sha256(("rentcompass-log-id\0" + raw).encode("utf-8")).hexdigest()
    return digest[:20]


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
            "user_ref": pseudonymous_user_ref(
                getattr(record, "user_id", user_id_var.get())
            ),
        }
        for key in ("node", "tool", "latency_ms", "cache_hit", "input_tokens", "output_tokens"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


try:
    # interrupt() (LangGraph HITL) signals a pause by raising GraphInterrupt; it is control
    # flow, not an error, and must not be logged with an ERROR-level traceback.
    from langgraph.errors import GraphInterrupt as _GraphInterrupt
except Exception:  # pragma: no cover — keeps this module importable without langgraph
    class _GraphInterrupt(BaseException):
        pass


@contextlib.contextmanager
def node_span(logger: logging.Logger, node: str, **attributes: Any) -> Iterator[None]:
    """Local structured span; upgrades to OTel without changing node call sites."""
    started = time.perf_counter()
    logger.info("node.start", extra={"node": node, **attributes})
    try:
        yield
    except _GraphInterrupt:
        logger.info(
            "node.interrupt",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **attributes},
        )
        raise
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
