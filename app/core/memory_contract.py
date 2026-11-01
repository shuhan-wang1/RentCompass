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


RECALL_TURN_WRITE_ERROR = "denied: recall-question turn, memory write blocked"


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
        contract["policy_decision"] = str(raw.get("security_decision") or "")
    if isinstance(stored, str) and stored.strip():
        contract['stored_content'] = stored
    return contract


def memory_contract_from_artifacts(artifacts: list | None) -> dict:
    """One contract for EVERY remember call of the turn (R2-31).

    "Remember that I have a cat. 记住我对花粉过敏。" made two successful writes and the reply
    echoed only the last. Each acknowledged text is listed; a mix of saved and failed writes
    says so."""
    # A write the model tried on a pure recall turn was no request of the user's: the notice
    # would replace the recall answer (V-14). A question the write path refused is the same: the
    # reply said "only durable facts, not instructions" and answered nothing (D-02).
    contracts = [memory_contract_from_artifact(a) for a in (artifacts or []) if isinstance(a, dict)
                 and a.get("error") != RECALL_TURN_WRITE_ERROR]
    contracts = [c for c in contracts if c and c.get("policy_decision") != "rejected_question_not_fact"]
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


def _mostly_cjk(text: str) -> bool:
    """True for Chinese text, a Latin place name beside it included. One Chinese word quoted in
    an English sentence does not make it the user's original wording (A-F8)."""
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    return cjk > 0 and 2 * cjk >= len(re.findall(r'[A-Za-z]', text))


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
        label = '已保存的原文：' if all(_mostly_cjk(c) for c in contents) else '已保存的内容：'
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
    if (contract or {}).get("policy_refused") and contract.get("policy_decision") == "rejected_secret":
        # A credential, card detail or id number: say what was withheld, never the value (R1-05).
        return ("这次没有保存到记忆：我不会保存密码、PIN、银行卡信息或护照、国民保险号、驾照等证件号码。" if zh
                else "I didn't save that to memory: I never store passwords, PINs, payment card "
                     "details or ID numbers such as passport, National Insurance or driving "
                     "licence numbers.")
    if (contract or {}).get("policy_refused") and contract.get("policy_decision") == "rejected_reference_only":
        return ("这次没有保存到记忆：这句话没有说明要记住的具体内容。" if zh
                else "I didn't save that to memory: it doesn't say what to keep.")
    if (contract or {}).get("policy_refused"):
        return ("这次没有保存到记忆：我只保存关于你的长期信息（偏好、预算、限制条件），不保存指令。" if zh
                else "I didn't save that to memory: I only save durable facts about you "
                     "(preferences, budget, constraints), not instructions.")
    return ("记忆保存失败，本轮没有确认写入。" if zh
            else "I could not save that to memory because the memory tool failed.")


# The composer runs at up to three sites of one turn. A notice from an earlier pass is not a
# model claim, so the patterns above did not remove it and each pass put another copy on top
# (U-05). Every fixed text, in both languages, is taken out first.
_FIXED_NOTICES = tuple(sorted({
    memory_notice(contract, language) for language in ("en", "zh") for contract in (
        {"requested": True, "success": True},
        {"requested": True, "success": True, "failed_count": 1},
        {"requested": True, "attempted": False, "error": "confirmation"},
        {"requested": True, "attempted": False},
        {"requested": True, "attempted": True, "policy_refused": True,
         "policy_decision": "rejected_secret"},
        {"requested": True, "attempted": True, "policy_refused": True,
         "policy_decision": "rejected_reference_only"},
        {"requested": True, "attempted": True, "policy_refused": True},
        {"requested": True, "attempted": True},
    )}, key=len, reverse=True))
_WITHHELD_NOTE = re.compile(
    r"I did not save [^\n]*? from your message; personal details like these are only kept if you "
    r"confirm them in a message of their own\.|补充：我没有保存[^\n]*?这类个人信息只有在你单独发消息确认后才会保存。")


def compose_memory_contract_response(response: str, contract: dict | None, *,
                                     language: str = "", preserve_content: bool = False) -> str:
    """Compose an authoritative write notice with any independent turn result.

    Model-generated save/failure claims are removed first so a failed write can never be
    followed by contradictory prose.  A memory-only turn returns only the deterministic
    notice; a multi-intent turn keeps its non-memory content. Composing twice changes nothing.
    """
    notice = memory_notice(contract, language)
    if not notice:
        return str(response or "")
    quote = saved_content_quote(contract, language)
    notice = notice + ('\n\n' + quote if quote else '')
    # The withheld-details note is the same kind of fixed text, and stands in every case.
    withheld = "\n\n".join(dict.fromkeys(_WITHHELD_NOTE.findall(str(response or ""))))
    if not preserve_content:
        return f"{notice}\n\n{withheld}" if withheld else notice
    remainder = _WITHHELD_NOTE.sub("", strip_saved_content_quote(response, contract, language))
    for fixed in _FIXED_NOTICES:
        remainder = remainder.replace(fixed, "")
    if (contract or {}).get("policy_decision") == "rejected_secret":
        # The rest of a multi-intent answer may not repeat the refused value either (R1-05).
        from rag.agent_memory import redact_secrets
        remainder = redact_secrets(remainder)[0]
    contents = list((contract or {}).get('stored_contents') or [])
    if isinstance((contract or {}).get('stored_content'), str):
        contents.append(contract['stored_content'])
    for content in contents:
        for left, right in (('「', '」'), ('“', '”'), ('"', '"')):
            remainder = remainder.replace(left + content + right, '')
    for pattern in _MEMORY_CLAIM_PATTERNS:
        remainder = pattern.sub("", remainder)
    remainder = re.sub(r"[ \t]+", " ", remainder)
    remainder = re.sub(r"\n{3,}", "\n\n", re.sub(r" *\n *", "\n", remainder)).strip(" \n.;，。")
    composed = f"{notice}\n\n{remainder}" if remainder else notice
    return f"{composed}\n\n{withheld}" if withheld else composed


# "Please remember that I prefer quiet streets. Also remember that my landlord's name is ...
# and my phone number is ... and my home address is ..." saved only the quiet streets and
# replied "I've saved that to memory." (S-06): the parts that were deliberately not saved are
# named, never their values.
_WITHHELD_LABELS = {
    "identity": ("the name", "姓名"), "contact": ("the phone number or email", "电话号码或邮箱"),
    "address": ("your home address", "家庭住址"),
}


def with_withheld_details_note(response: str, contract: dict | None, user_message: str,
                               language: str = "") -> str:
    """``response`` plus a note naming the personal details of a save request that were not
    saved, when the write succeeded without them."""
    body = str(response or "")
    if not (contract or {}).get("success") or not str(user_message or "").strip():
        return body
    try:
        from rag.agent_memory import _SAVE_REQUEST_CLAUSE, _SENSITIVE_CLAUSE_PATTERNS, redact_sensitive
    except Exception:
        return body
    from core.memory_gate import remember_reference_text
    clauses = " ".join([m.group(0) for m in _SAVE_REQUEST_CLAUSE.finditer(str(user_message)[:4000])]
                       + [remember_reference_text(str(user_message)[:4000])])
    if not clauses.strip():
        return body
    identity = dict(_SENSITIVE_CLAUSE_PATTERNS)["identity"]

    def categories(text):
        found = set(redact_sensitive(text)[1]) & {"contact", "address"}
        return found | ({"identity"} if identity.search(text) else set())
    stored = " ".join(str(c) for c in ((contract or {}).get("stored_contents")
                                       or [(contract or {}).get("stored_content") or ""]))
    # A detail the write did store (the user confirmed it) is not "not saved".
    missing = categories(clauses) - categories(stored)
    kept_out = [key for key in ("identity", "contact", "address") if key in missing]
    if not kept_out:
        return body
    zh = str(language or "").lower().startswith("zh")
    names = [(_WITHHELD_LABELS[key][1] if zh else _WITHHELD_LABELS[key][0]) for key in kept_out]
    listed = (", ".join(names[:-1]) + (", or " if len(names) > 2 else " or ") + names[-1]
              if len(names) > 1 else names[0])
    note = (f"补充：我没有保存{'、'.join(names)}。这类个人信息只有在你单独发消息确认后才会保存。" if zh
            else f"I did not save {listed} from your message; personal details like these are "
                 "only kept if you confirm them in a message of their own.")
    return body if note in body else f"{body.rstrip()}\n\n{note}"

