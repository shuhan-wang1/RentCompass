"""Deterministic rendering for explicit memory-write outcomes.

A model may propose prose, but only the observed write result may claim persistence.  The
composer also preserves the rest of a multi-intent answer instead of letting a ``remember``
side effect swallow a search, safety, or commute result from the same turn.
"""
from __future__ import annotations

import re
from typing import Any


_MEMORY_CLAIM_PATTERNS = (
    re.compile(
        r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?|it(?: has|'s) been)\s+"
        r"(?:saved|remembered|stored)\b[^.!?\n]*[.!?]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:could not|couldn't|was unable to|were unable to)\s+"
        r"(?:save|remember|store)\b[^.!?\n]*[.!?]?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:我)?(?:已经|已)?(?:记住|保存|存下)(?:了)?[^。！？\n]*[。！？]?"),
    re.compile(r"(?:记忆)?保存失败[^。！？\n]*[。！？]?"),
)


def memory_contract_from_artifact(artifact: dict | None) -> dict:
    """Project one write artifact onto the user-visible side-effect contract."""
    if not isinstance(artifact, dict):
        return {}
    raw = artifact.get("raw_data")
    success = bool(
        artifact.get("success") is True
        and not any(artifact.get(flag) for flag in (
            "timed_out", "denied", "abandoned", "outcome_unknown"))
        and isinstance(raw, dict)
        and raw.get("success", True) is not False
    )
    return {
        "requested": True,
        "attempted": not artifact.get("denied"),
        "success": success,
        "error": artifact.get("error"),
    }


def memory_notice(contract: dict | None, language: str = "") -> str:
    """Return the only persistence statement licensed by the observed outcome."""
    if not (contract or {}).get("requested"):
        return ""
    zh = str(language or "").lower().startswith("zh")
    if (contract or {}).get("success"):
        return "我已经记住了。" if zh else "I've saved that to memory."
    return ("记忆保存失败，本轮没有确认写入。" if zh
            else "I could not save that to memory because the memory tool failed.")


def compose_memory_contract_response(response: str, contract: dict | None, *,
                                     language: str = "", preserve_content: bool = False) -> str:
    """Compose an authoritative write notice with any independent turn result.

    Model-generated save/failure claims are removed first so a failed write can never be
    followed by contradictory prose.  A memory-only turn returns only the deterministic
    notice; a multi-intent turn keeps its non-memory content.
    """
    notice = memory_notice(contract, language)
    if not notice:
        return str(response or "")
    if not preserve_content:
        return notice
    remainder = str(response or "")
    for pattern in _MEMORY_CLAIM_PATTERNS:
        remainder = pattern.sub("", remainder)
    remainder = re.sub(r"[ \t]+", " ", remainder)
    remainder = re.sub(r" *\n *", "\n", remainder).strip(" \n.;，。")
    return f"{notice}\n\n{remainder}" if remainder else notice
