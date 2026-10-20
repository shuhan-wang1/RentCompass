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
    if isinstance(raw, dict) and raw.get("security_decision") and raw.get("success") is False:
        # The tool ran and REFUSED by policy ("I can only save durable facts ... not
        # instructions"): not a tool failure (R2-30).
        contract["policy_refused"] = True
        contract["policy_reason"] = str(raw.get("error") or "")
    if isinstance(stored, str) and stored.strip():
        contract['stored_content'] = stored
    return contract


def memory_contract_from_artifacts(artifacts: list | None) -> dict:
    """One contract for EVERY remember call of the turn (R2-31).

    "Remember that I have a cat. 记住我对花粉过敏。" made two successful writes and the reply
    echoed only the last. Each acknowledged text is listed; a mix of saved and failed writes
    says so."""
    contracts = [memory_contract_from_artifact(a) for a in (artifacts or []) if isinstance(a, dict)]
    contracts = [c for c in contracts if c]
    if not contracts:
        return {}
    if len(contracts) == 1:
        return contracts[0]
    saved = [c for c in contracts if c.get('success')]
    if not saved:
        return contracts[-1]
    combined = dict(saved[-1])
    texts = [c['stored_content'] for c in saved if isinstance(c.get('stored_content'), str)]
    combined['stored_contents'] = list(dict.fromkeys(texts))
    combined.pop('stored_content', None)
    if len(saved) < len(contracts):
        combined['failed_count'] = len(contracts) - len(saved)
    return combined


def saved_content_quote(contract: dict | None, language: str = '') -> str:
    """Only text acknowledged by the write result may be presented as saved.

    The model may store a paraphrase: an English paraphrase of a Chinese sentence is not
    labelled 原文 (the original wording)."""
    contents = (contract or {}).get('stored_contents')
    if not contents:
        content = (contract or {}).get('stored_content')
        contents = [content] if isinstance(content, str) and content.strip() else []
    contents = [c for c in contents if isinstance(c, str) and c.strip()]
    if not (contract or {}).get('success') or not contents:
        return ''
    if str(language).lower().startswith('zh'):
        # 原文 only when every saved text is itself Chinese; an English paraphrase of a
        # Chinese sentence is "saved content", not the original wording (R2-31).
        label = ('已保存的原文：' if all(re.search(r'[\u4e00-\u9fff]', c) for c in contents)
                 else '已保存的内容：')
        return label + '\n' + '\n'.join('「' + c + '」' for c in contents)
    return 'Saved text:\n' + '\n'.join('“' + c + '”' for c in contents)


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
        if (contract or {}).get("failed_count"):
            return ("部分内容已记住，另有一项没有保存成功。" if zh
                    else "I saved part of that to memory; one item could not be saved.")
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
    if (contract or {}).get("policy_refused"):
        return ("这次没有保存到记忆：我只保存关于你的长期信息（偏好、预算、限制条件），不保存指令。" if zh
                else "I didn't save that to memory: I only save durable facts about you "
                     "(preferences, budget, constraints), not instructions.")
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
    contents = list((contract or {}).get('stored_contents') or [])
    if isinstance((contract or {}).get('stored_content'), str):
        contents.append(contract['stored_content'])
    for content in contents:
        for left, right in (('「', '」'), ('“', '”'), ('"', '"')):
            remainder = remainder.replace(left + content + right, '')
    for pattern in _MEMORY_CLAIM_PATTERNS:
        remainder = pattern.sub("", remainder)
    remainder = re.sub(r"[ \t]+", " ", remainder)
    remainder = re.sub(r" *\n *", "\n", remainder).strip(" \n.;，。")
    return f"{notice}\n\n{remainder}" if remainder else notice
