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
    stored = raw.get('stored') if success else None
    contract = {
        "requested": True,
        "attempted": not artifact.get("denied"),
        "success": success,
        "error": artifact.get("error"),
    }
    if isinstance(stored, str) and stored.strip():
        contract['stored_content'] = stored
    return contract


def saved_content_quote(contract: dict | None, language: str = '') -> str:
    """Only text acknowledged by the write result may be presented as saved."""
    content = (contract or {}).get('stored_content')
    if not (contract or {}).get('success') or not isinstance(content, str) or not content.strip():
        return ''
    if str(language).lower().startswith('zh'):
        return '已保存的原文：\n「' + content + '」'
    return 'Saved text:\n“' + content + '”'


def strip_saved_content_quote(response: str, contract: dict | None, language: str = '') -> str:
    """User preference text is not a provider claim about a measured journey."""
    quote = saved_content_quote(contract, language)
    return str(response or '').replace(quote, '').strip() if quote else str(response or '')


def memory_notice(contract: dict | None, language: str = "") -> str:
    """Return the only persistence statement licensed by the observed outcome."""
    if not (contract or {}).get("requested"):
        return ""
    zh = str(language or "").lower().startswith("zh")
    if (contract or {}).get("success"):
        return "我已经记住了。" if zh else "I've saved that to memory."
    if (contract or {}).get("attempted") is False:
        # A policy refusal, not a failure: the write was never dispatched. Saying "the
        # memory tool failed" for it was false (R1-07).
        if 'confirmation' in str((contract or {}).get('error') or ''):
            return ("这次没有保存到记忆：这不是工具故障，而是保存需要你明确确认。"
                    "请单独发一条消息让我记住这件事。" if zh
                    else "I haven't saved that to memory. This was not a tool failure: saving "
                         "it needs your explicit confirmation, so please ask me to remember it "
                         "in a message of its own.")
        return ("本轮没有执行记忆保存，所以没有保存任何内容。" if zh
                else "I didn't get to save that to memory this turn, so nothing was saved.")
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
    quote = saved_content_quote(contract, language)
    notice = notice + ('\n\n' + quote if quote else '')
    if not preserve_content:
        return notice
    remainder = strip_saved_content_quote(response, contract, language)
    content = (contract or {}).get('stored_content')
    if isinstance(content, str):
        for left, right in (('「', '」'), ('“', '”'), ('"', '"')):
            remainder = remainder.replace(left + content + right, '')
    for pattern in _MEMORY_CLAIM_PATTERNS:
        remainder = pattern.sub("", remainder)
    remainder = re.sub(r"[ \t]+", " ", remainder)
    remainder = re.sub(r" *\n *", "\n", remainder).strip(" \n.;，。")
    return f"{notice}\n\n{remainder}" if remainder else notice
