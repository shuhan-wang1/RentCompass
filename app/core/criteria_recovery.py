"""Conservative repair of criteria written by older user-message parsers.

No storage, identity lookup or network access happens here. The caller supplies
one authenticated conversation's accumulated snapshot and complete messages.
Only a demonstrably invalid user-message source can authorize a scalar repair;
form/manual/saved-preference values and aggregate feature lists are preserved.
"""
from __future__ import annotations

from copy import deepcopy
import re


_FIELDS = ('area', 'max_budget', 'bedrooms', 'bedrooms_minimum', 'room_type')
_PLACEHOLDER = re.compile(
    r'(?:(?:my|the|an?|our)\s+)?(?:office|workplace|work|school|university|campus)'
    r'|(?:我的|我们(?:的)?|公司|学校的)?(?:办公室|工作地点|工作地|上班地点|公司|学校)', re.I)
_COMMUTE_CUE = re.compile(
    r'\b(?:my (?:office|workplace)|i work|work at|office|workplace|commut\w*|destination)\b'
    r'|办公室|工作地点|上班|通勤|学校是|上学', re.I)


def preferences_after_criteria_repair(preferences: dict, criteria: dict) -> dict:
    """Remove only the display labels of a proven invented lift requirement."""
    source = (criteria.get('constraint_sources') or {}).get('property_features') or {}
    if (source.get('reason') != 'lift_insufficiency_statement_is_not_requirement'
            or 'lift' in (criteria.get('property_features') or [])):
        return preferences
    result = deepcopy(preferences or {})
    for field, label in (('required_amenities', 'lift'), ('hard_preferences', 'must have lift')):
        if isinstance(result.get(field), list):
            result[field] = [value for value in result[field] if
                             not isinstance(value, str) or value.strip().casefold() != label]
    return result


def _placeholder(value) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER.fullmatch(value.strip().strip(' .!?。！？')))


def _normal(field, value):
    if value is None:
        return None
    if field == 'bedrooms_minimum' and isinstance(value, str):
        if value.lower() in ('true', 'false'):
            return value.lower() == 'true'
    if field in ('max_budget', 'bedrooms') and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return ' '.join(value.lower().split()) if isinstance(value, str) else value


def _same(field, left, right) -> bool:
    return _normal(field, left) == _normal(field, right)


def _text_key(text: str) -> str:
    return ' '.join(str(text or '').split()).casefold()


def _matches(source_text: str, candidate: str) -> bool:
    source, other = _text_key(source_text), _text_key(candidate)
    return bool(source and (source == other or (len(source) >= 700 and other.startswith(source))))


def _parse(apply, text: str) -> dict:
    from core.constraint_updates import housing_update_text
    from core.tools.search_properties import _extract_budget_clear
    parsed = apply({}, text)
    parsed = dict(parsed) if isinstance(parsed, dict) else {}
    # The ordinary updater need not write None into an already-empty state. For
    # source validation, an explicit clear is nevertheless a valid instruction.
    scoped = housing_update_text(text)
    if scoped and _extract_budget_clear(scoped):
        parsed.setdefault('max_budget', None)
    return parsed


def _invalid_reason(field: str, source: dict, text: str, parsed: dict):
    from core.constraint_updates import (
        housing_update_text, is_criteria_consultation, is_hypothetical,
        mention_is_requirement, scoped_update_text,
    )
    if field in parsed:
        return None if _same(field, parsed[field], source.get('value')) else 'parser_value_corrected'
    if is_criteria_consultation(text) or not housing_update_text(text):
        return 'read_only_or_ancillary_source'
    if is_hypothetical(text):
        return 'hypothetical_source'
    if _text_key(scoped_update_text(text)) != _text_key(text):
        return 'excluded_or_withdrawn_reference'
    if field == 'max_budget':
        if 'monthly_total_budget' in parsed:
            return 'total_budget_is_not_rent_budget'
        # Amount recognition stays in the shared parser. Its absent rent value,
        # paired with an explicit lack of choice, establishes an invalid old guess.
        if re.search(r'\b(?:not chosen|not sure|undecided|not decided|haven.t decided|have not decided)\b'
                     r'|还没(?:选择|决定)|尚未(?:选择|决定)|没选好|未决定', text, re.I):
            return 'ambiguous_budget_source'
    patterns = {
        'room_type': r'\b(?:studio|en[ -]?suite|shared|whole|rooms?)\b|整租|合租|单间|房型',
        'bedrooms': r'\b(?:studio|bedrooms?|beds?)\b|卧室|居室|[一二两三四五六七八九十]卧',
        'bedrooms_minimum': r'\b(?:studio|bedrooms?|beds?)\b|卧室|居室|[一二两三四五六七八九十]卧',
        'area': re.escape(str(source.get('value') or '')),
    }
    pattern = patterns.get(field)
    if pattern:
        mentions = list(re.finditer(pattern, text, re.I))
        if mentions and all(not mention_is_requirement(text, m.start(), m.end()) for m in mentions):
            return 'negated_or_question_reference'
    # Unparseable is not synonymous with invalid: old wording or truncated
    # history alone cannot justify deleting an otherwise legitimate value.
    return None


def _synchronise_recovered_area_alias(accumulated: dict) -> dict:
    """Finish a proven scalar repair's singleton alias, including older snapshots.

    Only the same original USER source can own the old singleton. Independent
    form/manual/saved values, multi-area choices and unrecorded edits are preserved.
    This metadata repair needs no history replay and is idempotent.
    """
    sources = accumulated.get('constraint_sources') or {}
    area = sources.get('area') if isinstance(sources, dict) else None
    areas = sources.get('areas') if isinstance(sources, dict) else None
    if not (isinstance(area, dict) and area.get('source') == 'criteria_recovery'
            and _same('area', area.get('value'), accumulated.get('area'))
            and isinstance(areas, dict) and areas.get('source') in ('user_message', 'criteria_recovery')):
        return accumulated
    original = area.get('original_source') or {}
    original_areas = areas.get('original_source') if areas.get('source') == 'criteria_recovery' else areas
    if not (isinstance(original, dict) and original.get('source') == 'user_message'
            and isinstance(original_areas, dict) and original_areas.get('source') == 'user_message'
            and original.get('text') and _text_key(original['text']) == _text_key(original_areas.get('text'))):
        return accumulated
    old, new = original.get('value'), accumulated.get('area')
    current_areas, old_source_areas = accumulated.get('areas'), areas.get('value')
    if not (old is not None and isinstance(old_source_areas, list) and len(old_source_areas) == 1
            and _same('area', old_source_areas[0], old)
            and isinstance(current_areas, list) and len(current_areas) == 1
            and (_same('area', current_areas[0], old) or _same('area', current_areas[0], new))):
        return accumulated
    new_areas = [new] if new else []
    if old_source_areas == new_areas:
        return accumulated
    result = deepcopy(accumulated)
    result['areas'] = new_areas
    result['constraint_sources']['areas'] = {
        'source': 'criteria_recovery', 'value': new_areas,
        'reason': 'canonical_area_alias_synchronized',
        'original_source': deepcopy(areas), 'message_id': area.get('message_id'),
    }
    provenance = list(result.get('criteria_repair_provenance') or [])
    provenance.append({'field': 'areas', 'reason': 'canonical_area_alias_synchronized',
                       'previous_value': current_areas, 'previous_source_value': old_source_areas,
                       'value': new_areas, 'message_id': area.get('message_id')})
    result['criteria_repair_provenance'] = provenance[-20:]
    return result


def _remove_proven_lift_reference(accumulated: dict, messages: list[dict]) -> dict:
    """Remove only a lift invented from a proven insufficiency/evidence statement.

    Aggregate lists otherwise remain intact. Any independent user requirement for
    a lift, or protected/manual ownership, prevents this narrow repair.
    """
    from core.langgraph_agent import _extract_accessibility_requirements
    from core.tools.search_properties import _accessibility_feature_mentions
    from core.constraint_updates import clause_at

    features = accumulated.get('property_features') or []
    record = (accumulated.get('constraint_sources') or {}).get('property_features')
    if not (isinstance(features, list) and 'lift' in features and isinstance(record, dict)
            and record.get('source') == 'user_message' and record.get('text')
            and isinstance(record.get('value'), list) and 'lift' in record['value']):
        return accumulated
    text = record['text']
    if 'lift' in _extract_accessibility_requirements(text)['required']:
        return accumulated
    insufficient = False
    for feature, start, end in _accessibility_feature_mentions(text):
        if feature == 'lift' and re.search(
                r'\b(?:on its own|alone)\b[^.!?;\n]{0,35}\b(?:not|cannot|can.t|doesn.t|wouldn.t)\b'
                r'[^.!?;\n]{0,35}\b(?:answer|prove|establish|mean|satisfy|guarantee)\b', clause_at(text, start, end), re.I):
            insufficient = True
    if not insufficient:
        return accumulated
    message_id = None
    for message in messages or []:
        if not isinstance(message, dict) or str(message.get('role') or '').lower() != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        if 'lift' in _extract_accessibility_requirements(content)['required']:
            return accumulated
        if _matches(text, content):
            message_id = message.get('id')
    result = deepcopy(accumulated)
    filtered = [feature for feature in features if feature != 'lift']
    result['property_features'] = filtered
    result['constraint_sources']['property_features'] = {
        'source': 'criteria_recovery', 'value': filtered,
        'reason': 'lift_insufficiency_statement_is_not_requirement',
        'original_source': deepcopy(record), 'message_id': message_id,
    }
    provenance = list(result.get('criteria_repair_provenance') or [])
    provenance.append({'field': 'property_features', 'reason': 'lift_insufficiency_statement_is_not_requirement',
                       'previous_value': features, 'value': filtered, 'message_id': message_id})
    result['criteria_repair_provenance'] = provenance[-20:]
    return result


def repair_criteria_sources(accumulated: dict, messages: list[dict]) -> dict:
    """Repair only proven bad scalar sources using this conversation's USER text.

    Returns the original object when no repair is needed. Repaired sources use
    ``source=criteria_recovery`` so repeated read-only loads are idempotent. Raw
    source provenance remains local to this supplied conversation.
    """
    from core.constraint_updates import housing_update_text, resolve_clarification_reply
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    from core.criteria_contract import synchronize_monthly_budget_alias

    if not isinstance(accumulated, dict):
        return {}
    accumulated = _synchronise_recovered_area_alias(accumulated)
    accumulated = _remove_proven_lift_reference(accumulated, messages)
    sources = accumulated.get('constraint_sources') or {}
    if not isinstance(sources, dict):
        return accumulated
    candidates = {field: record for field, record in sources.items()
                  if field in _FIELDS and isinstance(record, dict)
                  and record.get('source') == 'user_message' and record.get('text')
                  and (field not in accumulated or 'value' not in record
                       or _same(field, accumulated[field], record['value']))}
    suspects = {}
    for field, record in candidates.items():
        expected = {**record, 'value': record.get('value', accumulated.get(field))}
        try:
            parsed = _parse(apply, record['text'])
            if _invalid_reason(field, expected, record['text'], parsed):
                suspects[field] = expected
        except Exception:
            continue
    candidates = suspects

    def protected(field):
        record = sources.get(field)
        if isinstance(record, dict) and record.get('source') not in (
                None, 'user_message', 'criteria_recovery'):
            return True
        if field in ('commute_destination', 'destination'):
            mirror = 'destination' if field == 'commute_destination' else 'commute_destination'
            other = sources.get(mirror)
            return (accumulated.get(field) == accumulated.get(mirror)
                    and isinstance(other, dict) and other.get('source') not in (
                        None, 'user_message', 'criteria_recovery'))
        return False

    generic = [field for field in ('commute_destination', 'destination')
               if _placeholder(accumulated.get(field)) and not protected(field)]
    if not candidates and not generic:
        return synchronize_monthly_budget_alias(accumulated)

    events, prefix, replay, reliable = [], [], {}, {}
    destination_evidence = None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        item = dict(message)
        item['role'] = str(item.get('role') or '').lower()
        content = item.get('content')
        if item['role'] == 'user' and isinstance(content, str) and content.strip():
            try:
                text = resolve_clarification_reply(content, prefix)
            except (AttributeError, TypeError, ValueError):
                text = content
            try:
                parsed = _parse(apply, text)
                after = apply(deepcopy(replay), text)
            except Exception:
                # A parser failure is no evidence that the persisted value is wrong.
                prefix.append(item)
                continue
            if not isinstance(after, dict):
                prefix.append(item)
                continue
            event = {'raw': content, 'text': text, 'id': item.get('id'), 'parsed': parsed,
                     'scoped': housing_update_text(text)}
            events.append(event)
            for field in _FIELDS:
                if field in parsed:
                    reliable[field] = {'value': parsed[field], 'event': event}
                elif field in after and not _same(field, after[field], replay.get(field)):
                    reliable[field] = {'value': after[field], 'event': event}
            if parsed.get('no_commute') is True:
                destination_evidence = {'value': None, 'event': event}
            else:
                destination = after.get('commute_destination') or after.get('destination')
                if destination and not _placeholder(destination) and _COMMUTE_CUE.search(text):
                    destination_evidence = {'value': destination, 'event': event}
            replay = after
            # Honour a clear even when the empty-state parser omitted its write.
            if 'max_budget' in parsed and parsed['max_budget'] is None:
                replay = {**replay, 'max_budget': None}
        prefix.append(item)

    changes = {}
    for field, source in candidates.items():
        matched = [event for event in events if any(_matches(source['text'], event[key])
                                                   for key in ('raw', 'text', 'scoped'))]
        event = matched[-1] if matched else None
        text = event['text'] if event else source['text']
        try:
            parsed = _parse(apply, text)
            reason = _invalid_reason(field, source, text, parsed)
        except Exception:
            continue
        if not reason:
            continue
        replacement = reliable.get(field)
        if not event and field in parsed:
            # The current source itself is reliable evidence of the corrected
            # value even if the caller's older history was truncated.
            replacement = {'value': parsed[field], 'event': None}
        changes[field] = {'value': replacement['value'] if replacement else None,
                          'evidence': replacement, 'reason': reason}

    for field in generic:
        changes[field] = {'value': destination_evidence['value'] if destination_evidence else None,
                          'evidence': destination_evidence,
                          'reason': 'generic_destination_placeholder'}
    if not changes:
        return synchronize_monthly_budget_alias(accumulated)
    result = deepcopy(accumulated)
    repaired_sources = result.setdefault('constraint_sources', {})
    provenance = list(result.get('criteria_repair_provenance') or [])
    for field, change in changes.items():
        old_value = accumulated.get(field)
        result[field] = change['value']
        evidence = change['evidence']
        evidence_event = (evidence or {}).get('event') or {}
        repaired_sources[field] = {
            'source': 'criteria_recovery', 'value': change['value'], 'reason': change['reason'],
            'original_source': deepcopy(sources.get(field)),
            'message_id': evidence_event.get('id'),
        }
        provenance.append({'field': field, 'reason': change['reason'],
                           'previous_value': old_value, 'value': change['value'],
                           'message_id': evidence_event.get('id')})
        if (field == 'area' and accumulated.get('areas') == [old_value]
                and not protected('areas') and sources.get('areas') is None):
            result['areas'] = [change['value']] if change['value'] else []
        if field in ('commute_destination', 'destination'):
            mirror = 'destination' if field == 'commute_destination' else 'commute_destination'
            if not protected(mirror) and (not accumulated.get(mirror) or
                                           accumulated.get(mirror) == old_value):
                result[mirror] = change['value']
                repaired_sources[mirror] = deepcopy(repaired_sources[field])
    result['criteria_repair_provenance'] = provenance[-20:]
    return synchronize_monthly_budget_alias(_synchronise_recovered_area_alias(result))
