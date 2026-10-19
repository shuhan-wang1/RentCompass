"""Small, deterministic boundaries for explicit changes to housing requirements.

These helpers deliberately return no update for questions and hypothetical examples.
They do not infer personal attributes or silently broaden a hard requirement.
"""
from __future__ import annotations

import re


_CLAUSE_START = r'(?<![^。.!?;；\n])'
_WITHDRAWN_CLAUSE = re.compile(
    _CLAUSE_START + r'[^。.!?;；\n]*(?:\b(?:discard|disregard|ignore|withdraw)\b|作废|撤销|丢弃)'
    r'[^。.!?;；\n]*(?:[。.!?;；\n]|$)', re.I)
_KEPT_OUT_CLAUSE = re.compile(
    _CLAUSE_START + r'[^。.!?;；\n]*\bkeep\b[^。.!?;；\n]*\bout of (?:this|the) (?:decision|search)\b'
    r'[^。.!?;；\n]*(?:[。.!?;；\n]|$)', re.I)


def scoped_update_text(text: str) -> str:
    text = str(text or '')
    # Withdrawn experiments and other people's plans are references, not updates.
    # Each clause pattern is anchored at a clause START (``_CLAUSE_START``). Unanchored, the
    # leading ``[^…]*`` let the engine retry from every character of a long clause, each
    # attempt re-scanning to its end: O(n²) on a long paste with no full stop, run several
    # times per turn on the event loop (2026-10-19, B-01 class). Leftmost-match semantics
    # already started every real match at a clause start, so the result is unchanged.
    text = _WITHDRAWN_CLAUSE.sub('', text)
    text = _KEPT_OUT_CLAUSE.sub('', text)
    # A comparison can quote another plan without changing it. Keep the explicit
    # modification, excluding the clause marked as unchanged.
    parts = re.split(r'[;；\n]', text)
    if len(parts) > 1 and re.search(r'只(?:修改|改|调整)|only (?:change|update)', text, re.I):
        parts = [p for p in parts if not re.search(r'不变|保持原|unchanged|leave .* as is', p, re.I)]
    return '; '.join(parts)


def is_hypothetical(text: str) -> bool:
    return bool(re.search(
        r'\b(?:what if|suppose|hypothetically|for example|if (?:a|the|we|i|it|my|there))\b|假设|假如|举例|比如说|如果', text, re.I))


def explicit_criteria_revision(text: str) -> bool:
    """A positive instruction replacing old defaults is an update, not a conflict."""
    pattern = re.compile(
        r'(?:替换|覆盖|取代|重设|重置)(?:我(?:的)?)?(?:(?:此前|之前|原来|原有|先前|旧)(?:的)?)?'
        r'(?:长期)?(?:默认(?:租房)?(?:偏好|条件|设置)?|租房偏好|预算|上限|筛选条件)'
        r'|\b(?:replace|supersede|override|reset|overwrite)\s+(?:(?:my|the|our)\s+)?'
        r'(?:(?:previous|old|existing|earlier|current)\s+)?(?:(?:saved|rental|search)\s+)?'
        r'(?:defaults?|preferences|criteria|filters|(?:rent )?budget)\b', re.I)
    for match in pattern.finditer(text or ''):
        clause = clause_at(text, match.start(), match.end())
        if (is_hypothetical(clause) or re.search(r'[?？]|是否|会不会|能否|\b(?:should|would|could|can)\b', clause, re.I)):
            continue
        if mention_is_requirement(text, match.start(), match.end()):
            return True
    return False


def is_criteria_consultation(text: str) -> bool:
    """An explicit read-only request must also bypass cached-result refinement."""
    if re.search(r"(?:don.t|do not|please don.t) change (?:my |the )?(?:search )?(?:criteria|filters)"
                 r'|leave (?:my |the )?(?:housing |search |housing search )?(?:criteria|filters) unchanged'
                 r'|不要(?:改变|修改|调整)(?:我的|当前|搜索)?(?:筛选|条件|需求)', text, re.I):
        return True
    if re.search(r'(?:housing|search) (?:must|should|will) (?:stay|remain)|(?:only|just) a weather question|'
                 r'(?:找房|住房|搜索)(?:条件|区域|需求)?(?:必须|应当|仍然|还是|保持)[^。.!?]{0,25}(?:不变|维持|watford)', text, re.I):
        return True
    if re.search(r'\b(?:save|favourite|favorite)\b|收藏', text, re.I) and re.search(r'\bfavou?rites?\b|收藏', text, re.I) and not re.search(
            r'\b(?:find|search|set|change|raise|lower|remove|drop|switch|restore)\b|改为|改回|更新|找房|搜索', text, re.I):
        return True
    update = re.search(r'\b(?:set|change|raise|lower|remove|drop|find|search|show|available)\b|改为|改回|更新|取消|删除|移除|找房|搜索', text, re.I)
    if re.search(r'\b(?:details?|furniture|furnished|bills|availability|tenancy|epc)\b|房源详情|家具|账单', text, re.I) and re.search(
            r'^\s*(?:please|can |could |what|tell |explain|get |check |verify|for the)|请|这套|这个房源|告诉|查询|[?？]', text, re.I) and not re.search(
            r'\b(?:find|search|set|change|raise|lower|remove|drop|switch|restore)\b|改为|改回|更新|取消|删除|移除|找房|搜索', text, re.I):
        return True
    # "Please don't search; give me a summary" is still a read-only request.
    cleaned = re.sub(r'请?不要搜索|don.t search|do not search', '', text, flags=re.I)
    if re.search(r'摘要|需求总结|\b(?:summary|recap)\b', cleaned, re.I) and not re.search(
            r'改为|改回|更新|取消|删除|移除|\b(?:set|change|raise|lower|remove|drop)\b', cleaned, re.I):
        return True
    return bool(re.search(r'^\s*(?:is|does|has)\b', text, re.I) and not update)


_CLAUSE_BOUNDARY = re.compile(r'[。！？!?;；\n，,:：]|(?<!\d)\.(?!\d)')
# How far a clause may extend either side of a mention. Every caller runs once per MENTION,
# and scanning the whole prefix each time made a long unpunctuated paste O(mentions x
# length) — 11 s for 28 000 characters of repeated Chinese criteria, on the event loop
# (2026-10-19, B-01 class). No real clause is longer than this.
_CLAUSE_WINDOW = 400


def clause_at(text: str, start: int, end: int) -> str:
    lo = max(0, start - _CLAUSE_WINDOW)
    # finditer(text, pos, endpos) reports absolute offsets.
    left = max([m.end() for m in _CLAUSE_BOUNDARY.finditer(text, lo, start)] or [lo])
    # The tail is searched in a SLICE, exactly as before: with pos=end the look-behind
    # would see the digit before `end`, so "My budget is 1500. If I could ..." stopped
    # treating that full stop as a boundary and read the next sentence's hypothetical.
    tail = _CLAUSE_BOUNDARY.search(text[end:end + _CLAUSE_WINDOW])
    right = end + tail.start() if tail else min(len(text), end + _CLAUSE_WINDOW)
    return text[left:right]


def mention_is_requirement(text: str, start: int, end: int) -> bool:
    clause = clause_at(text, start, end)
    # Negation belongs to this clause. In particular, "not all London: a whole
    # one-bedroom flat" negates the area, not the room type or bedroom count.
    # Only the last 45 characters are read, so a bounded window is exactly equivalent.
    window = text[max(0, start - _CLAUSE_WINDOW):start]
    before = _CLAUSE_BOUNDARY.split(window)[-1][-45:]
    if is_hypothetical(clause):
        return False
    if re.search(r'\b(?:don.t|do not) assume\b|不要假设|不能假设|out of (?:this|the) decision', clause, re.I):
        return False
    if re.search(r'\b(?:on its own|alone)\b[^.!?;\n]{0,35}\b(?:not|cannot|can.t|doesn.t|wouldn.t)\b'
                 r'[^.!?;\n]{0,35}\b(?:answer|prove|establish|mean|satisfy|guarantee)\b', clause, re.I):
        return False
    if re.search(r'\b(?:must|should|can) not (?:be )?(?:treated|assumed|considered|taken)|'
                 r'\bdoes not (?:mean|prove|guarantee)|不能(?:视为|证明)|不代表', clause, re.I):
        return False
    if re.search(r'(?:not(?: a| an)?|no|without|remove|exclude|avoid|delete|don.t want)\s+(?:\w+\s+){0,3}$'
                 r'|(?:不(?:要|是|需要|想|考虑)|取消|删除|移除|去掉|排除)[^，。；;!?]{0,14}$', before, re.I):
        return False
    if re.search(r'是否|有没有|能否|\b(?:does it|is there|are there|do they|can you explain)\b', clause, re.I):
        return False
    if re.search(r'是什么|怎么样|允许.*[吗？]|\b(?:is this|is that|does this|does that|tell me about|what does)\b', clause, re.I):
        return False
    return True


def housing_update_text(text: str) -> str:
    """Exclude standalone ancillary requests from the housing mutation channel."""
    text = scoped_update_text(text)
    # Negative/preservation clauses in an ancillary question are not housing
    # instructions. Require a positive housing action before accepting its places.
    if is_criteria_consultation(text):
        return ''
    auxiliary = re.search(r'天气|气温|票价|线路状态|\b(?:weather|temperature|fare|travelcard|line status|journey)\b', text, re.I)
    if auxiliary:
        from core.tools.search_properties import _KNOWN_AREAS
        affirmative = []
        for clause in re.split(r'[。.!?;；\n]', text):
            if re.search(r'\b(?:don.t|do not|never)\b|不要|无需|不用|别', clause, re.I):
                continue
            if re.search(r'\b(?:unchanged|remain|stay|only a weather|only weather)\b|不变|保持原', clause, re.I):
                continue
            area_action = any(any(re.match(re.escape(name) + r'(?![a-z])', match.group(1), re.I)
                                  for name in _KNOWN_AREAS)
                for match in re.finditer(r'\b(?:search(?: in| around)?|keep|restore|switch to|change to)\s+([a-z][a-z\s\-]{0,35})', clause, re.I))
            explicit_slot = re.search(
                r'\b(?:rent|budget|ceiling|bedrooms?|whole (?:flat|property)|self.contained|workplace)\b'
                r'|\b(?:commute|commuting|office)\b.{0,65}\b(?:days?|minutes?|change|workplace)\b'
                r'|\b(?:my (?:job|work)|a job)\b.{0,35}\b(?:near|next to|at|in)\b'
                r'|\b(?:wheelchair|step.free|doorways?|corridors?|bathroom|cats?|pets?|landlord|permission|consent|lifts?|elevators?|balcony|garden)\b'
                r'|\bfocus on\b|月租|租金|预算|卧室|整租|到岗|入住|起租|轮椅|无台阶|猫|许可|电梯|阳台|花园', clause, re.I)
            if area_action or explicit_slot or re.search(r'\b(?:search|find|rent)\b.{0,35}\b(?:flat|home|house|property|properties|studio|bedroom)|'
                         r'\b(?:set|change|raise|lower|remove|drop)\b.{0,30}\b(?:budget|bedroom|area|room type|commute destination|my office)|'
                         r'找房|搜索房源|找.{0,8}(?:公寓|卧|住处)|预算改|改为|改回|恢复', clause, re.I):
                affirmative.append(clause)
        if not affirmative:
            return ''
        text = '; '.join(affirmative)
    if is_criteria_consultation(text):
        return ''
    housing_intent = re.search(
        r'找房|帮我找|寻找|搜(?:索|三套|房)|看(?:看)?房|整租|预算|月租|房租|改为|改回|改成|切换|恢复|备用方案|主方案'
        r'|\b(?:find|search|show|rent|budget|ceiling|bedroom|move in|restore|switch|instead|make it|change|set|keep|remove|drop)\b', text, re.I)
    if not housing_intent and re.search(
        r'天气|气温|犯罪|治安|票价|票卡|线路状态|\b(?:weather|temperature|crime|safety|fare|travelcard|line status|journey)\b', text, re.I):
        return ''
    return text


def explicit_feature_requirement_text(text: str) -> str:
    """Declarative hard property needs may reaffirm missing slots during a review.

    Only these sentences are returned. Questions about a listing, hypothetical
    requirements and explicit instructions to preserve criteria remain read-only.
    """
    selected = []
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        sentence = match.group().strip()
        if (is_hypothetical(sentence) or re.search(r'[?？]|\b(?:does|is|has) (?:this|that|the|either)\b|是否|有没有|能否', sentence, re.I)):
            continue
        hard = re.search(r'\b(?:must|needs?|require(?:s|d)?|requirements?|essential|mandatory)\b|必须|必需|需要|硬条件', sentence, re.I)
        feature = re.search(r'wheelchair|step.free|doorways?|corridors?|bathroom|cats?|pets?|permission|consent|lifts?|elevators?'
                            r'|轮椅|无台阶|猫|许可|电梯', sentence, re.I)
        if not (hard and feature):
            continue
        if re.search(r'\b(?:don.t|do not) (?:assume|add|require|need)\b|不要假设|不要添加|不再需要', sentence, re.I):
            continue
        selected.append(sentence)
    return ' '.join(selected)


def references_saved_criteria(text: str) -> bool:
    return bool(re.search(
        r'(?:按|用|恢复|复述|记得)[^。！？]{0,35}(?:长期|默认|保存|偏好)|长期默认|saved (?:search |default )?(?:criteria|preferences)|'
        r'(?:use|apply|restore|recall|repeat)[^.!?]{0,35}(?:defaults|remembered|preferences)', text or '', re.I))


def restore_saved_criteria(accumulated: dict, memories: list, message: str) -> dict:
    """Use only this authenticated user's retrieved memory, on an explicit request.

    Prompt recollection alone does not enforce filters. Project the selected semantic
    preference into the same structured state the search executor validates.
    """
    if not references_saved_criteria(message) or not re.search(
            r'搜|找|恢复|使用|按|\b(?:use|apply|restore|find|search|show)\b', message, re.I):
        return accumulated
    from core.langgraph_agent import _apply_explicit_criteria_updates
    candidates = sorted(memories or [], key=lambda m: str(m.get('created_at') or ''), reverse=True)
    candidates.sort(key=lambda m: (m.get('mtype') != 'semantic',
        not bool(re.search(r'默认|长期|\bdefaults?\b|saved preferences', str(m.get('text') or ''), re.I))))
    for memory in candidates:
        text = str(memory.get('text') or '')
        parsed = _apply_explicit_criteria_updates({}, text)
        fields = {k: v for k, v in parsed.items() if k in {
            'area', 'areas', 'max_budget', 'budget_period', 'room_type', 'bedrooms',
            'bedrooms_minimum', 'commute_destination', 'destination', 'no_commute',
            'max_travel_time', 'property_features', 'soft_preferences', 'move_in_date',
            'monthly_total_budget', 'office_days_per_week'}}
        if fields.get('max_travel_time'):
            fields.setdefault('no_commute', False)
        if sum(k in fields for k in ('room_type', 'bedrooms')) + bool(
                fields.get('max_budget') or fields.get('monthly_total_budget')) >= 2:
            restored = dict(accumulated)
            restored.update(fields)
            restored['constraint_sources'] = {
                **(accumulated.get('constraint_sources') or {}),
                **{k: {'source': 'saved_preference', 'memory_id': memory.get('id'), 'value': v}
                   for k, v in fields.items()},
            }
            return _apply_explicit_criteria_updates(restored, message)
    return accumulated


def resolve_clarification_reply(message: str, messages: list) -> str:
    """Interpret only a value from this conversation's latest pending question.

    Options may carry opaque UI values. Its stored display label supplies the
    meaning, without trusting client-supplied criteria or another conversation.
    """
    for item in reversed(messages or []):
        if item.get('role') != 'assistant':
            continue
        if item.get('response_type') not in ('clarification', 'ask_user'):
            return message
        choices = item.get('options') or (item.get('metadata') or {}).get('options') or []
        for choice in choices:
            if isinstance(choice, dict) and choice.get('value') == message:
                return str(choice.get('label') or message)
        return message
    return message


def validate_current_criteria_recap(response: str, criteria: dict, *, message: str = '', language: str = 'en') -> str:
    """Correct narrow claims about current state from the persisted state itself."""
    text = str(response or '')
    days = (criteria or {}).get('office_days_per_week')
    words = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7)
    corrected = False
    lines = []
    for line in text.splitlines():
        match = re.search(
            r"(?:criteria block|current criteria|current work pattern|office attendance|your work pattern|you(?:'re| are) (?:still )?commuting)"
            r'[^.!\n]{0,90}?\b([0-7]|one|two|three|four|five|six|seven)\s+(?:office\s+|work\s+)?days?\s+(?:a|per)\s+week', line, re.I)
        if match and isinstance(days, (int, float)):
            value = match.group(1).lower()
            count = int(value) if value.isdigit() else words[value]
            if count != days:
                corrected = True
                continue
        if re.search(r'favou?rites?|收藏', message, re.I) and re.search(
                r'favou?rite.{0,60}(?:yourself|visit|listing page|onthemarket)|(?:yourself|visit).{0,60}favou?rite', line, re.I) and re.search(r'onthemarket|external|外部', line, re.I):
            continue
        lines.append(line)
    if corrected:
        lines.append(f'当前已保存的到岗频率是每周 {days:g} 天。' if language == 'zh' else
                     f'Your current criteria record {days:g} office days per week.')
    if re.search(r'favou?rites?|收藏', message, re.I) and not re.search(r'heart|Saved view|Save button|收藏按钮|爱心', '\n'.join(lines), re.I):
        lines.append('可点击房源卡片的爱心收藏按钮，再到“收藏夹”查看；聊天助手不能代您点击。' if language == 'zh' else
                     'Use the heart/Save button on the listing card, then open the Saved view. The chat assistant cannot click it for you.')
    return '\n'.join(lines).strip()


def commute_query_forbidden(text: str) -> bool:
    """A per-turn tool boundary, independent of the user's lasting commute filter."""
    return bool(re.search(
        r'(?:don.t|do not|never)\s+(?:run|calculate|check|query|call|recalculate)(?:\s+\w+){0,3}\s+(?:commut\w*|journey)'
        r'|(?:不要|别|无需|不用)(?:再|运行|调用|进行|计算|查询|查|算){0,4}通勤', text or '', re.I))


def cat_permission_update(text: str) -> bool | None:
    """Cat ownership is a fact; requiring landlord permission is a housing slot.

    Permission may precede or follow the animal and the hard cue. Inspect each
    statement independently so a later evidence warning cannot negate a requirement.
    None means unchanged, False an explicit withdrawal, True a requirement.
    """
    for clause in re.split(r'[。.!?;；\n]', text or ''):
        # "pets" too: "I do not need pets allowed any more" left the hard "cats allowed"
        # filter in place (2026-10-19 A-07). A withdrawal may name pets generally; a
        # requirement still has to pass the hard-cue test below.
        animal = re.search(r'\b(?:cats?|pets?)\b|猫|宠物', clause, re.I)
        permission = re.search(r'permission|permitt|allow|accept|consent|许可|允许|同意|养猫|带猫', clause, re.I)
        if not (animal and permission):
            continue
        if re.search(r'(?:remove|drop|clear|(?:no longer|don.t|do not|doesn.t|does not) (?:need|require))'
                     r'[^.!?]{0,50}(?:permission|requirement|filter|cats?|pets?)'
                     r'|(?:cats?|pets?)\s+allowed\s+(?:is\s+)?(?:no longer|not)\s+(?:needed|necessary|required)'
                     r'|(?:取消|去掉|不再需要)[^。；]{0,18}(?:猫|宠物)(?:许可|筛选|条件)', clause, re.I):
            return False
        if not re.search(r'\bcats?\b|猫', clause, re.I):
            continue        # a pet requirement without a cat is not this slot
        if is_hypothetical(clause) or re.search(
                r'\b(?:unknown|whether|does|can you|is it|do not assume|don.t assume|no permission|not required)\b'
                r'|是否|未知|不要|暂时不|没有.{0,15}(?:许可|同意)', clause, re.I):
            continue
        hard = re.search(r'\b(?:must|needs?|require(?:s|d)?|essential|mandatory|hard requirements?)\b|必须|必需|需要|硬条件', clause, re.I)
        if hard or re.search(r'\b(?:permission|consent) for (?:this |that |our |my |a |the )?(?:specific )?cat\b', clause, re.I):
            return True
    return None


_PET_WORD = r'\b(?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies))\b|猫|狗|宠物'
_PET_WITHDRAWAL = re.compile(
    r'\b(?:forget|never\s*mind|ignore|drop|remove|scrap|skip|disregard|cancel)\b[^.!?;]{0,30}'
    r'(?:\b(?:cats?|pets?|dogs?)\b|pet[- ]friendly)'
    r'|\bno\s+longer\s+(?:have|own|got|need|require|want|keep)\b[^.!?;]{0,25}\b(?:cats?|pets?|dogs?|kittens?)\b'
    r'|\b(?:don.t|do\s+not)\s+(?:have|own)\s+(?:a\s+|any\s+|the\s+)?(?:cats?|pets?|dogs?)\s+any\s*(?:more|longer)\b'
    r'|\b(?:cats?|pets?|dogs?)\b[^.!?;]{0,25}\b(?:doesn.t|does\s+not|don.t|do\s+not|no\s+longer)\s+matter'
    r'|\b(?:the\s+)?(?:cat|pet|dog)\s+(?:is|has)\s+(?:gone|rehomed|no\s+longer\s+with\s+me)\b'
    r'|\bno\s+(?:need\s+for\s+)?pets?\s+(?:needed|required|requirement)\b'
    r'|不再养|不养(?:猫|狗|宠物)了|没有(?:猫|狗|宠物)了'
    r'|(?:猫|狗|宠物)(?:的事|的要求|的条件|这个条件)?(?:不用|不需要|算了|不重要了|取消)'
    r'|(?:取消|去掉|删除|忽略)[^。；]{0,10}(?:猫|狗|宠物)', re.I)


def pet_requirement_update(text: str) -> bool | None:
    """A pet-permission requirement or its withdrawal, in the user's own words.

    True: "It must allow pets." / "Find a 1-bed in Hackney that allows pets" — the home
    has to permit pets, so a listing without that evidence is unverified, never a
    verified match (R1-06). False: "Actually forget the pets thing, I no longer have a
    cat." — every pet requirement and pet preference goes (R1-05). None: unchanged.
    Cats keep their own, stricter slot (``cat_permission_update``)."""
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        clause = match.group()
        if not re.search(_PET_WORD, clause, re.I):
            continue
        if _PET_WITHDRAWAL.search(clause):
            return False
        if is_hypothetical(clause) or re.search(
                r'[?？]|是否|吗|\b(?:do not assume|don.t assume|unknown|whether)\b', clause, re.I):
            continue
        if re.search(r'\b(?:no|without|don.t want|do not want)\s+(?:any\s+)?(?:pets?|dogs?|cats?)\b', clause, re.I):
            continue    # "no pets in the building please" is not a permission requirement
        permission = re.search(r'\b(?:allow(?:s|ed|ing)?|accept(?:s|ed|ing)?|permit(?:s|ted)?|friendly|ok(?:ay)?\s+with)\b'
                               r'|允许|可以养|能养|接受|可养', clause, re.I)
        if not permission:
            continue
        hard = re.search(r'\b(?:must|needs?\s+to|has\s+to|have\s+to|required?|requires|essential|mandatory|only)\b'
                         r'|必须|一定要|需要|要求', clause, re.I)
        restrictive = (re.search(r'\b(?:that|which|where)\s+(?:allows?|accepts?|permits?)\s+(?:pets?|cats?|dogs?)\b'
                                 r'|\bpet[- ]friendly\s+(?:flat|home|place|property|apartment|studio|house|\d|one|two)'
                                 r'|允许养|可以养|能养|可养', clause, re.I)
                       and re.search(r'\b(?:find|search|show|look(?:ing)?\s+for|want|need)\b|找|搜|要', clause, re.I))
        if hard or restrictive:
            return True
    return None


def precise_property_requirements(text: str, required: list[str]) -> list[str]:
    """Keep explicitly stated fit/written-permission requirements independently.

    A wheelchair's stated width is not a measured doorway clearance. The fit tag
    remains unknown until a source verifies usable space for that wheelchair.
    """
    from decimal import Decimal
    features = []
    if 'wheelchair-accessible' in required:
        width = None
        patterns = (
            r"\bwheelchair\s+(?:(?:that(?:'s| is)|is|measures|with a width of)\s+)?(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimet(?:er|re)s?)\s+wide\b",
            r"\b(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimet(?:er|re)s?)\s+(?:wide\s+)?wheelchair\b",
            r'轮椅\s*宽(?:度)?(?:为|是)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:cm|厘米)',
        )
        for pattern in patterns:
            match = re.search(pattern, text or '', re.I)
            if match:
                width = format(Decimal(match.group(1)).normalize(), 'f')
                break
        if width:
            features.append(f'wheelchair_space_{width}cm')
    if 'cats allowed' in required and re.search(r'\bwritten (?:consent|permission)\b|\b(?:consent|permission)\b[^.!?;]{0,25}\bin writing\b|书面(?:许可|同意)|业主书面', text or '', re.I):
        features.append('cat_written_consent')
    return features


def extract_office_days(text: str) -> int | None:
    """Read a declared office frequency or named office weekdays, never WFH days."""
    words = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7)
    weekday = r'(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)'
    indexes = dict(mon=0, tue=1, wed=2, thu=3, fri=4, sat=5, sun=6)
    for clause in re.split(r'[。.!?;；\n]', text or ''):
        if not re.search(r'\b(?:office|commut\w*|workplace|on.site)\b|\bi work (?:at|in|near)\b|到岗|到公司|通勤', clause, re.I):
            continue
        # WFH occupies a different predicate even when linked with "and".
        office = re.split(r'\band (?:i )?(?:work(?:ing)? from home|wfh)\b|其余.*(?:在家|居家)', clause, maxsplit=1, flags=re.I)[0]
        match = re.search(r'\b([1-7]|one|two|three|four|five|six|seven)\s+(?:office\s+)?days?\s+(?:a|per)\s+week\b', office, re.I)
        if match:
            value = match.group(1).lower()
            return int(value) if value.isdigit() else words[value]
        match = re.search(rf'\b({weekday})\s*(?:to|through|[-–—])\s*({weekday})\b', office, re.I)
        if match:
            first, last = (indexes[match.group(i).lower()[:3]] for i in (1, 2))
            return (last - first) % 7 + 1
        named = {indexes[m.group().lower()[:3]] for m in re.finditer(rf'\b{weekday}\b', office, re.I)}
        if named:
            return len(named)
    return None


def extract_max_changes(text: str) -> int | None:
    """Explicit transfer upper bound. None means absent, zero means direct only."""
    words = dict(zero=0, one=1, two=2, three=3)
    match = re.search(r'\b(?:at most|no more than|up to|maximum(?: of)?)\s+(\d|zero|one|two|three)\s+(?:changes?|transfers?)\b', text or '', re.I)
    if match:
        value = match.group(1).lower()
        return int(value) if value.isdigit() else words[value]
    if re.search(r'\b(?:direct only|no (?:changes?|transfers?))\b|不换乘|无需换乘', text or '', re.I):
        return 0
    match = re.search(r'(?:最多|至多|不超过)\s*([0-3零一二两三])\s*次?换乘', text or '')
    if match:
        value = match.group(1)
        return int(value) if value.isdigit() else {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3}[value]
    return None


def canonical_criteria(criteria: dict) -> dict:
    """Project the residential and destination aliases from their canonical slots.

    Missing keys carry no update. Explicit None remains a clear, including stale
    legacy aliases; ordinary tool payloads cannot decide to clear user criteria.
    """
    result = dict(criteria or {})
    if 'areas' in result and isinstance(result['areas'], list):
        result['areas'] = list(dict.fromkeys(a.strip() for a in result['areas'] if isinstance(a, str) and a.strip()))
        result['area'] = result['areas'][0] if result['areas'] else None
    elif result.get('area'):
        result['areas'] = [result['area']]
    if 'commute_destination' in result:
        result['destination'] = result['commute_destination']
    elif 'destination' in result:
        result['commute_destination'] = result['destination']
    return result


def protect_confirmed_criteria(result: dict, confirmed: dict) -> dict:
    """Tool extraction is evidence, not authority to revise user-confirmed slots."""
    result = dict(result or {})
    for key, source in (confirmed.get('constraint_sources') or {}).items():
        if isinstance(source, dict) and source.get('source') in ('user_message', 'saved_preference', 'user_form', 'criteria_recovery') and key in confirmed:
            value = confirmed[key]
            result[key] = list(value) if isinstance(value, list) else value
    # A confirmed area list and destination own both of their compatibility aliases.
    if result.get('no_commute') and result.get('commute_destination') is None and 'commute_destination' not in (confirmed.get('constraint_sources') or {}):
        # Legacy no-commute payloads use destination as the search area only.
        residential = canonical_criteria({k: v for k, v in result.items() if k in ('area', 'areas')})
        result.update(residential)
        return result
    return canonical_criteria(result)


def date_is_cleared(text: str) -> bool:
    return bool(re.search(
        r'(?:取消|清空|删除|去掉|不(?:要|需要))(?:任何)?(?:入住|起租|搬入)(?:日期|时间)?'
        r'|(?:remove|clear|drop|no) (?:the |any )?(?:move.in|tenancy.start|start) date'
        r'|(?:入住|起租|搬入)(?:日|日期|时间)(?:暂(?:时)?|尚|还)?(?:未确定|未定|没确定|不确定|待定)', text, re.I))


def cleared_fields(text: str) -> set[str]:
    result = set()
    for field, names in {
        'room_type': r'房型|room.type',
        'bedrooms': r'卧室(?:数(?:量)?)?|bedrooms?',
        'property_features': r'设施|房屋特征|property.features|amenities',
        'area': r'(?:居住|搜索)?区域|(?:residential |search )?areas?',
        'commute_destination': r'通勤目的地|commute destination',
    }.items():
        if re.search(r'(?:取消|清空|删除|去掉|不限|不限制)[^，。;；]{0,6}(?:' + names + r')'
                     r'|(?:remove|clear|drop|no) (?:the |any )?(?:' + names + r')(?: filter| limit| requirement)?', text, re.I):
            result.add(field)
    return result


def independent_memory_task(message: str) -> bool:
    """A positive independent request survives a memory-only turn boundary.

    Negated search/compare predicates do not become tasks merely because their
    verb occurs. Contrast/please clauses remain separate, preserving real mixed
    requests such as "do not search, but compare these two areas".
    """
    action = (r'复述|总结|摘要|逐项|列出|解释|恢复|怎么|如何|计算|比较|推荐|搜索|找房'
              r'|\b(?:summari[sz]e|recap|repeat|list|explain|restore|recover|calculate|compare|find|search|show)\b')
    ancillary_topic = (r'天气|气温|天气预报|票价|线路状态|服务状态|'
                       r'\b(?:weather|forecast|temperature|fares?|line status|service status|disruptions?)\b')
    ancillary_action = (r'(?:查(?:询|看)?|看看|核实|获取|\b(?:check|look up|get|what|how)\b)'
                        r'(?=[^。.!?;；\n，,]{0,65}(?:' + ancillary_topic + r'))')
    action = '(?:' + action + ')|(?:' + ancillary_action + ')'
    for clause in re.split(r'[。.!?;；\n，,]|\bbut\b|\b(?:and|then) please\b|但是|不过|但|并(?:且)?请', message or '', flags=re.I):
        for match in re.finditer(action, clause, re.I):
            prefix = clause[:match.start()]
            if re.search(r'(?:不要|别|无需|不用|不必)[^，。;；!?]{0,14}$'
                         r'|\b(?:do not|don.t|never|no need to|no)\s+(?:\w+\s+){0,8}$', prefix, re.I):
                continue
            return True
    return False
