from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChatRequest:
    """Validated identity and inputs shared by the HTTP chat transports."""

    data: dict[str, Any]
    user_id: str
    message: str
    context: dict[str, Any]
    is_continuation: bool
    ui_language: str
    request_id: str
