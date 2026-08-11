"""Versioned prompt specifications and SystemMessage provenance checks.

Only immutable, application-owned instructions may occupy the system role in the
function-calling loop.  Runtime context, memory, summaries, conversation text, and
tool evidence are deliberately excluded from :class:`PromptSpec` and must travel in
lower-privilege messages.

The registry is process-local metadata, not mutable prompt storage.  A spec is
registered from source code immediately before its SystemMessage is built.  The
``trace_prompt_specs`` interface lets telemetry/evaluations record the exact prompt
identity, version, variant, and SHA-256 without coupling that concern to ``app.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from threading import RLock
from typing import Any, Iterable


class PromptSpecError(RuntimeError):
    """Base error for invalid or conflicting prompt specifications."""


class UnregisteredSystemPromptError(PromptSpecError):
    """Raised when a SystemMessage did not come from a registered PromptSpec."""


class PromptAssemblyError(PromptSpecError):
    """Controlled fail-closed error for a prompt that could not be assembled safely."""


@dataclass(frozen=True)
class PromptSpec:
    """One immutable, application-owned system prompt.

    ``variant`` is for a finite source-controlled variant such as ``en``/``zh``;
    it must never contain a user value.  ``content_hash`` fingerprints the exact
    bytes sent to the model, while ``prompt_id`` and ``version`` provide the stable
    semantic identity needed by traces and evaluations.
    """

    prompt_id: str
    version: str
    purpose: str
    content: str
    variant: str = "default"

    def __post_init__(self) -> None:
        for field_name in ("prompt_id", "version", "purpose", "content", "variant"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise PromptSpecError(f"PromptSpec.{field_name} must be a non-empty string")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def trace_fields(self) -> dict[str, str]:
        """Stable metadata suitable for logs, spans, and evaluation manifests."""
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.version,
            "prompt_variant": self.variant,
            "prompt_purpose": self.purpose,
            "prompt_hash": self.content_hash,
        }


_LOCK = RLock()
_BY_KEY: dict[tuple[str, str, str], PromptSpec] = {}
_BY_HASH: dict[str, PromptSpec] = {}


def register_prompt_spec(spec: PromptSpec) -> PromptSpec:
    """Register ``spec`` and reject identity/hash collisions fail-closed."""
    key = (spec.prompt_id, spec.version, spec.variant)
    with _LOCK:
        existing = _BY_KEY.get(key)
        if existing is not None and existing != spec:
            raise PromptSpecError(
                "prompt identity reused with different content: "
                f"{spec.prompt_id}@{spec.version}/{spec.variant}"
            )
        hash_owner = _BY_HASH.get(spec.content_hash)
        if hash_owner is not None and hash_owner != spec:
            raise PromptSpecError(
                "prompt content hash is already owned by a different PromptSpec: "
                f"{hash_owner.prompt_id}@{hash_owner.version}/{hash_owner.variant}"
            )
        _BY_KEY[key] = spec
        _BY_HASH[spec.content_hash] = spec
    return spec


def system_message(spec: PromptSpec):
    """Build a SystemMessage from a registered, immutable PromptSpec."""
    from langchain_core.messages import SystemMessage

    registered = register_prompt_spec(spec)
    return SystemMessage(content=registered.content)


def prompt_spec_for_system_message(message: Any) -> PromptSpec | None:
    """Return the registered spec for ``message`` when it is an exact match."""
    from langchain_core.messages import SystemMessage

    if not isinstance(message, SystemMessage):
        return None
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _LOCK:
        spec = _BY_HASH.get(digest)
    return spec if spec is not None and spec.content == content else None


def assert_registered_system_messages(messages: Iterable[Any]) -> None:
    """Enforce that every SystemMessage is backed by a static PromptSpec."""
    from langchain_core.messages import SystemMessage

    for index, message in enumerate(messages):
        if isinstance(message, SystemMessage) and prompt_spec_for_system_message(message) is None:
            raise UnregisteredSystemPromptError(
                f"unregistered SystemMessage at index {index}; dynamic data must use a "
                "lower-privilege message"
            )


def trace_prompt_specs(messages: Iterable[Any]) -> list[dict[str, str]]:
    """Return ordered, deduplicated PromptSpec metadata for a model request.

    This is the public tracing interface.  It validates the system-role invariant
    first, so a trace can never silently bless an unregistered dynamic system row.
    """
    materialized = list(messages)
    assert_registered_system_messages(materialized)
    traces: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for message in materialized:
        spec = prompt_spec_for_system_message(message)
        if spec is None:
            continue
        key = (spec.prompt_id, spec.version, spec.variant)
        if key not in seen:
            seen.add(key)
            traces.append(spec.trace_fields())
    return traces
