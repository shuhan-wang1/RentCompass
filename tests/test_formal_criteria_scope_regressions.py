"""Offline regressions for formal acceptance F02/F03/F04/F05/F06/F10.

Tests replay the submitted persona messages and exercise the same deterministic
slots with synonyms, negative/read-only examples and adversarial tool echoes.
No HTTP, language model or production state is used.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from core.constraint_updates import cat_permission_update, extract_office_days, extract_max_changes
from core.context_assembler import detect_history_conflicts
from core.langgraph_agent import _apply_explicit_criteria_updates, update_search_criteria
from core.tools.search_properties import (
    _extract_budget, _extract_total_budget, _extract_areas, _extract_no_commute,
    _extract_move_in_date, ground_hard_constraints, _property_feature_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evaluation/results/formal_live_2026_10_19'


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('This constraint regression suite must remain offline')
    import requests
    import httpx
    import socket
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def persona_messages(persona, filename):
    return [json.loads(line)['message'] for line in (EVIDENCE / persona / filename).read_text().splitlines()]


def test_exact_professional_three_turn_replay_preserves_distinct_caps_and_commute():
    messages = [(EVIDENCE / 'professional' / f'turn{i}_message.txt').read_text() for i in (1, 2, 3)]
    acc = {}
    history = []
    for index, message in enumerate(messages):
        assert not detect_history_conflicts(history, message)
        acc = _apply_explicit_criteria_updates(acc, message)
        assert acc['monthly_total_budget'] == 2100
        assert acc['office_days_per_week'] == 3
        assert acc['no_commute'] is False
        assert acc['commute_destination'] == acc['destination'] == 'Bank station, London'
        assert acc['max_travel_time'] == 35
        assert acc['max_changes'] == 1
        assert 'move_in_date' not in acc
        if index:
            assert acc['max_budget'] == 1900
            assert acc['area'] == 'Lewisham' and acc['areas'] == ['Lewisham']
        else:
            assert acc.get('max_budget') is None
        # Model/tool-generated values cannot revise the user's confirmed slots.
        acc = update_search_criteria(acc, {'max_budget': 2100, 'no_commute': True,
            'commute_destination': 'London', 'area': 'Greenwich', 'areas': ['Greenwich']})
        assert acc['no_commute'] is False
        assert acc['destination'] == 'Bank station, London'
        history.append({'user': message, 'assistant': ''})


@pytest.mark.parametrize('message', [
    'Rent ceiling £1900 pcm; £2100 is a separate total rent-plus-commuting cap.',
    'My combined budget is £2100 a month for rent and transport; rent at most £1900 pcm.',
    '£1900 pcm is the rent search ceiling, while £2100 is a separate total rent-plus-commuting affordability cap.',
    '月租最多£1900，£2100是包含租金和通勤的总预算。',
])
def test_amount_noun_phrase_scope_is_independent_of_order(message):
    assert _extract_budget(message) == (1900, 'month')
    assert _extract_total_budget(message) == 2100
    assert not detect_history_conflicts([], message)


@pytest.mark.parametrize('message,days', [
    ('I go into the office Tuesday to Thursday and work from home the other days.', 3),
    ('I commute Tue–Thu; WFH on Monday and Friday.', 3),
    ('Office on Monday, Wednesday and Friday; work from home on other days.', 3),
    ('I commute two days per week and WFH otherwise.', 2),
    ('I work from home Tuesday to Thursday.', None),
    ('Check the train every three days per week.', None),
])
def test_office_frequency_is_scoped_to_attendance(message, days):
    assert extract_office_days(message) == days


@pytest.mark.parametrize('message', [
    'I commute Tue–Thu and work from home on the other days.',
    'I go into the office two days a week, working from home otherwise.',
    '每周三天到公司，其余时间在家办公。',
    "I'm not fully remote; I commute to the office.",
])
def test_hybrid_work_does_not_clear_commute(message):
    assert _extract_no_commute(message) is False


@pytest.mark.parametrize('message', ['I work from home.', 'I am fully remote.', '我不需要通勤。'])
def test_explicit_no_commute_still_clears_limit(message):
    assert _extract_no_commute(message) is True
    acc = _apply_explicit_criteria_updates({'max_travel_time': 35}, message)
    assert acc['no_commute'] is True and acc['max_travel_time'] is None


@pytest.mark.parametrize('message,changes', [
    ('At most one change.', 1), ('no more than two transfers', 2),
    ('direct only', 0), ('最多一次换乘', 1), ('check the changes tomorrow', None),
])
def test_explicit_transfer_slot_keeps_zero_and_absence_distinct(message, changes):
    assert extract_max_changes(message) == changes


@pytest.mark.parametrize('message', [
    'TfL route from Lewisham station to Bank arriving on 20 October at 08:45.',
    'The train starts from Lewisham on 2026-10-20; please check the journey.',
    'Check weather from 20 October 2026.',
    '2026-10-20',
    '明天2026年10月20日去Bank，08:45到达。',
])
def test_journey_and_weather_dates_are_not_move_in_dates(message):
    assert _extract_move_in_date(message, now=date(2026, 10, 19)) is None
    original = {'move_in_date': '2026-11-15'}
    assert _apply_explicit_criteria_updates(original, message)['move_in_date'] == '2026-11-15'


def test_move_in_date_only_reads_the_housing_predicate():
    message = 'Journey from Lewisham arriving 20 October. Move in on 15 November 2026.'
    assert _extract_move_in_date(message, now=date(2026, 10, 19)) == '2026-11-15'
    message = 'Move in on 15 November 2026 and check the route arriving 20 October.'
    assert _extract_move_in_date(message, now=date(2026, 10, 19)) == '2026-11-15'


def test_missing_clear_and_unchanged_date_remain_distinct():
    original = _apply_explicit_criteria_updates({}, 'Move in on 15 November 2026.')
    assert _apply_explicit_criteria_updates(original, 'Please keep my move-in date unchanged.') == original
    cleared = _apply_explicit_criteria_updates(original, 'Clear the move-in date.')
    assert 'move_in_date' in cleared and cleared['move_in_date'] is None
    assert update_search_criteria(cleared, {'move_in_date': '2026-10-20'})['move_in_date'] is None


@pytest.mark.parametrize('message,areas', [
    ('请找Stratford或Leyton的整租两卧住宅', ['Stratford', 'Leyton']),
    ('Please search BOTH Stratford and Leyton again.', ['Stratford', 'Leyton']),
    ('Find a flat in Camden or Greenwich.', ['Camden', 'Greenwich']),
    ('I work in Stratford but want to live in Leyton.', ['Leyton']),
    ('What is the weather in Stratford and Leyton?', []),
    ('Do not change my search criteria. Compare Stratford and Leyton.', []),
])
def test_residential_area_list_has_one_canonical_order(message, areas):
    assert _extract_areas(message) == areas


def test_family_raw_messages_preserve_hard_requirements_and_soft_lift():
    messages = persona_messages('family', 'family-turns.jsonl')
    acc = _apply_explicit_criteria_updates({}, messages[0])
    assert acc['areas'] == ['Stratford', 'Leyton'] and acc['area'] == 'Stratford'
    assert {'step-free', 'wheelchair-accessible', 'cats allowed'} <= set(acc['property_features'])
    assert 'lift' not in acc['property_features']
    assert any('lift' in item.lower() for item in acc['soft_preferences'])
    assert not any('step-free' in item.lower() for item in acc['soft_preferences'])
    assert _apply_explicit_criteria_updates(acc, messages[1]) == acc
    acc = _apply_explicit_criteria_updates(acc, messages[2])
    assert acc['areas'] == ['Stratford', 'Leyton'] and acc['max_budget'] == 2700
    assert {'step-free', 'wheelchair-accessible', 'cats allowed'} <= set(acc['property_features'])
    # Tool fan-out and stale echoes must preserve the full confirmed request.
    acc = update_search_criteria(acc, {'area': 'Leyton', 'areas': ['Leyton'],
        'property_features': ['lift'], 'soft_preferences': ['Would like step-free']})
    assert acc['areas'] == ['Stratford', 'Leyton'] and acc['area'] == 'Stratford'
    assert 'lift' not in acc['property_features']
    assert not any('step-free' in item.lower() for item in acc['soft_preferences'])
    for feature in acc['property_features']:
        assert _property_feature_evidence({'description': 'Lift access; pets considered.'}, feature) == 'unverified'


@pytest.mark.parametrize('message,requirement', [
    ('允许这只猫也是必需条件，需要业主书面许可。', True),
    ('We require the landlord to allow our cat in writing.', True),
    ('HARD requirements: permission for this specific cat in writing from the landlord.', True),
    ('We have a cat.', None),
    ('Does this property allow cats?', None),
    ('If the landlord allows a cat, would it be suitable?', None),
    ('Do not assume cats are allowed.', None),
    ('Remove the cat permission requirement.', False),
])
def test_cat_ownership_permission_question_and_requirement_are_distinct(message, requirement):
    assert cat_permission_update(message) is requirement


@pytest.mark.parametrize('message', [
    'We need enough internal doorway and corridor width and turning space for a wheelchair.',
    '室内必须有足够轮椅通行和转身的空间。',
    'Find a home with doors wide enough for the wheelchair and room to turn inside.',
])
def test_functional_internal_access_is_a_hard_property_slot(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert 'wheelchair-accessible' in acc['property_features']


def test_lift_or_cat_ownership_does_not_assert_functional_internal_access():
    acc = _apply_explicit_criteria_updates({}, 'Find a home with a lift. We have a cat.')
    assert 'wheelchair-accessible' not in acc.get('property_features', [])
    assert 'cats allowed' not in acc.get('property_features', [])


def test_student_exact_message_keeps_pennies_and_never_rounds_up_to_a_pound():
    message = persona_messages('student', 'student_ucl-turns.jsonl')[1]
    assert _extract_budget(message) == (2166.67, 'month')
    acc = _apply_explicit_criteria_updates({}, message)
    assert acc['max_budget'] == 2166.67
    params, corrected = ground_hard_constraints({'max_budget': 2167}, acc, message)
    assert params['max_budget'] == 2166.67 and 'max_budget' in corrected
    assert update_search_criteria(acc, {'max_budget': 2167})['max_budget'] == 2166.67


def test_model_cannot_replace_confirmed_budget_bedrooms_area_destination_or_clear():
    acc = _apply_explicit_criteria_updates({},
        'Find a whole flat in Stratford or Leyton. Rent ceiling £2166.67 per month. '
        'Keep 2 bedrooms. My workplace is near Bank station; maximum commute 35 minutes.')
    supplied = {'area': 'Leyton', 'areas': ['Leyton'], 'commute_destination': 'London',
        'max_budget': 3000, 'bedrooms': 1, 'room_type': 'shared', 'move_in_date': '2026-10-20',
        'max_commute_time': 55, 'no_commute': True}
    params, _ = ground_hard_constraints(supplied, acc, '')
    assert params['max_budget'] == 2166.67 and params['bedrooms'] == 2
    assert params['area'] == 'Stratford' and params['areas'] == ['Stratford', 'Leyton']
    assert params['commute_destination'] == 'Bank station, London'
    assert params['max_commute_time'] == 35 and params['no_commute'] is False
    assert params['room_type'] == 'whole'


def test_source_retrieval_survives_turn_snapshot_without_becoming_empty_market():
    from core.context_assembler import build_turn_snapshot, snapshot_to_session_patch
    retrieval = {'source_status': {'Stratford': 'source_failed'}, 'source_message': 'Source unavailable',
                 'retrieval_complete': False, 'partial': True, 'data_source': 'real_time_scraping'}
    snapshot = build_turn_snapshot(turn_id='offline', persistent_state={
        'extracted_context': {'search_retrieval': retrieval, 'current_message': 'not durable'}})
    patch = snapshot_to_session_patch(snapshot)
    assert patch['search_retrieval'] == retrieval
    snapshot['search_retrieval']['source_status']['Stratford'] = 'tampered'
    assert patch['search_retrieval']['source_status']['Stratford'] == 'source_failed'
    assert 'current_message' not in snapshot


def test_source_health_fields_survive_structured_search_output():
    from core.langgraph_agent import _structured_search_tool_data
    source = {'recommendations': [], 'candidate_validation': {}, 'source_status': {'Leyton': 'source_failed'},
              'source_errors': [{'area': 'Leyton', 'status_code': 403}],
              'source_message': 'Source unavailable', 'retrieval_complete': False,
              'retrieval_outcome': 'source_failed', 'partial': True, 'data_source': 'real_time_scraping'}
    _, data = _structured_search_tool_data(source, {})
    for key in ('source_status', 'source_errors', 'source_message', 'retrieval_complete',
                'retrieval_outcome', 'partial', 'data_source'):
        assert data[key] == source[key]


def test_weekly_budget_conversion_keeps_fractional_monthly_ceiling():
    from core.tenancy_reference import monthly_from_weekly
    acc = _apply_explicit_criteria_updates({}, 'Rent at most £350 a week.')
    assert acc['max_budget'] == monthly_from_weekly(350)
    assert acc['max_budget'] < 1517


@pytest.mark.parametrize('supplied', [350, 1517, 1516.6666666666667])
def test_grounding_weekly_budget_always_returns_matching_canonical_month_unit(supplied):
    from core.tenancy_reference import monthly_from_weekly
    params, _ = ground_hard_constraints({'max_budget': supplied, 'budget_period': 'week'}, {}, 'My budget is £350 per week.')
    assert params['max_budget'] == monthly_from_weekly(350) and params['budget_period'] == 'month'


def test_confirmed_month_budget_cannot_be_reinterpreted_as_weekly():
    acc = _apply_explicit_criteria_updates({}, 'Rent at most £1900 per month.')
    params, _ = ground_hard_constraints({'max_budget': 1900, 'budget_period': 'week'}, acc, '')
    assert params['max_budget'] == 1900 and params['budget_period'] == 'month'


def test_leading_journey_date_does_not_own_later_move_in_predicate():
    assert _extract_move_in_date('Journey arriving 20 October and I want to move in on 15 November 2026.',
                                 now=date(2026, 10, 19)) == '2026-11-15'


def test_explicit_area_and_destination_clear_both_canonical_aliases():
    acc = _apply_explicit_criteria_updates({}, 'Find a flat in Stratford or Leyton. My office is near Bank station.')
    assert _apply_explicit_criteria_updates(acc, 'Keep the areas unchanged.') == acc
    cleared = _apply_explicit_criteria_updates(acc, 'Clear the search areas. Clear the commute destination.')
    assert cleared['area'] is None and cleared['areas'] == []
    assert cleared['destination'] is None and cleared['commute_destination'] is None
    stale = update_search_criteria(cleared, {'area': 'Leyton', 'areas': ['Leyton'], 'commute_destination': 'Bank'})
    assert stale['area'] is None and stale['areas'] == []
    assert stale['destination'] is None and stale['commute_destination'] is None


@pytest.mark.parametrize('message', [
    'Search Camden only, a whole home. Our cat needs permission.',
    'Find a whole home. Our cat requires written consent from the landlord.',
])
def test_cat_permission_requirement_accepts_inflected_need_and_require(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert 'cats allowed' in acc['property_features']


@pytest.mark.parametrize('message', [
    'Our cat needs food. Find a home in Camden.',
    'Do not assume our cat needs permission. Find a home in Camden.',
    'Our cat needs no permission. Find a home in Camden.',
    'Whether our cat needs permission is unknown. Find a home in Camden.',
])
def test_cat_need_word_alone_or_uncertainty_does_not_invent_permission_requirement(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert 'cats allowed' not in acc.get('property_features', [])


@pytest.mark.parametrize('message,expected', [
    ('我下学期要来伦敦读研，想在Camden找个住处。能帮我看看吗？', 'Camden'),
    ('我要来伦敦工作，希望在Leyton租一套公寓。', 'Leyton'),
    ('我在伦敦上班，计划在Greenwich附近找住房。', 'Greenwich'),
])
def test_chinese_trailing_residential_predicate_beats_background_city(message, expected):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) == expected
    assert _extract_areas(message) == [expected]
    acc = _apply_explicit_criteria_updates({}, message)
    assert acc['area'] == expected and acc['areas'] == [expected]
    assert acc.get('max_budget') is None and acc.get('bedrooms') is None


@pytest.mark.parametrize('message', [
    '我来伦敦读研，在Camden找工作。',
    '我来伦敦读研，不要在Camden找住处。',
    '我来伦敦读研，假设在Camden找住处，会怎么样？',
])
def test_chinese_locative_work_negative_or_hypothetical_does_not_select_residence(message):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) != 'Camden'


def test_blackbox_student_mixed_language_criteria_are_complete_without_model_fill():
    opening = '我下学期要来伦敦读研，想在Camden找个住处。能帮我看看吗？'
    message = ('只在Camden找，不是伦敦全区：整租一居室，整套房租每月最高£2166.67，'
        '坐公共交通到UCL Bloomsbury主校区（Gower Street）45分钟以内。入住日暂未确定。'
        '请按这些条件找实际房源，明确哪些已核实、哪些仍不确定。')
    acc = _apply_explicit_criteria_updates({}, opening)
    acc = _apply_explicit_criteria_updates(acc, message)
    assert acc['area'] == 'Camden' and acc['areas'] == ['Camden']
    assert acc['room_type'] == 'whole' and acc['bedrooms'] == 1
    assert acc['bedrooms_minimum'] is False and acc['max_budget'] == 2166.67
    assert acc['commute_destination'] == acc['destination'] == 'University College London, Gower Street, London WC1E 6BT'
    assert acc['max_travel_time'] == 45 and acc['no_commute'] is False
    assert acc['move_in_date'] is None
    params, _ = ground_hard_constraints({'area': 'London', 'areas': ['London'],
        'room_type': 'shared', 'bedrooms': 0, 'commute_destination': None, 'max_budget': 2200}, acc, message)
    assert params['area'] == 'Camden' and params['areas'] == ['Camden']
    assert params['room_type'] == 'whole' and params['bedrooms'] == 1
    assert params['commute_destination'] == acc['commute_destination']
    assert params['max_budget'] == 2166.67 and params['budget_period'] == 'month'


@pytest.mark.parametrize('message', [
    '只在Camden找，不是伦敦全区：整租一居室。',
    '在Camden找，不要伦敦其他地方。整套一卧。',
    'Search Camden only, not all London: a whole one-bedroom flat.',
])
def test_negated_broader_area_cannot_negate_positive_room_slots(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert acc['area'] == 'Camden' and acc['areas'] == ['Camden']
    assert acc['bedrooms'] == 1 and acc['room_type'] == 'whole'


@pytest.mark.parametrize('message', ['通勤到UCL不超过45分钟', '坐车去UCL Bloomsbury，45分钟以内'])
def test_ascii_destination_alias_next_to_chinese_is_still_an_explicit_slot(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert acc['commute_destination'] == 'University College London, Gower Street, London WC1E 6BT'
    assert acc['destination'] == acc['commute_destination'] and acc['no_commute'] is False


def test_unknown_move_in_date_explicitly_removes_previous_date():
    acc = _apply_explicit_criteria_updates({'move_in_date': '2026-11-15'}, '入住日暂未确定。')
    assert 'move_in_date' in acc and acc['move_in_date'] is None


@pytest.mark.parametrize('message', ["Our cat doesn't need permission.", "We don't need cat permission."])
def test_inflected_cat_negation_cannot_add_a_hard_permission_filter(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert 'cats allowed' not in acc.get('property_features', [])


R5_SAVE_DEFAULTS = ('我准备和同学整租一套，各有一间卧室。请记住并保存我的长期默认租房偏好，'
    '替换此前默认：只在Camden，整套住宅whole，正好2间卧室，整套月租最高£2400；'
    '坐公共交通到UCL Bloomsbury主校区（Gower Street）30分钟以内。入住日未定。'
    '这条消息只保存默认，不要搜索房源。')


def test_exact_saved_default_replacement_uses_total_bedrooms_and_revises_ceiling():
    from core.constraint_updates import independent_memory_task, explicit_criteria_revision
    from core.tools.search_properties import _extract_bedrooms
    previous = _apply_explicit_criteria_updates({},
        '只在Camden找，不是伦敦全区：整租一居室，整套房租每月最高£2166.67，'
        '坐公共交通到UCL Bloomsbury主校区（Gower Street）45分钟以内。入住日暂未确定。')
    assert _extract_bedrooms(R5_SAVE_DEFAULTS) == (2, 2)
    assert explicit_criteria_revision(R5_SAVE_DEFAULTS) is True
    assert detect_history_conflicts([{'user': '整套房租每月最高£2166.67。', 'assistant': ''}], R5_SAVE_DEFAULTS) == []
    result = _apply_explicit_criteria_updates(previous, R5_SAVE_DEFAULTS)
    restored = _apply_explicit_criteria_updates({}, R5_SAVE_DEFAULTS)
    for acc in (result, restored):
        assert acc['area'] == 'Camden' and acc['areas'] == ['Camden']
        assert acc['bedrooms'] == 2 and acc['bedrooms_minimum'] is False
        assert acc['room_type'] == 'whole' and acc['max_budget'] == 2400
        assert acc['max_travel_time'] == 30 and acc['move_in_date'] is None
        assert acc['commute_destination'] == 'University College London, Gower Street, London WC1E 6BT'
    assert independent_memory_task(R5_SAVE_DEFAULTS) is False


@pytest.mark.parametrize('message,expected', [
    ('我们每人各有一间卧室，整套正好2间卧室。', (2, 2)),
    ('正好三间卧室的整套住宅，每人一间卧室。', (3, 3)),
    ('Each of us needs one bedroom. The whole property needs exactly two bedrooms.', (2, 2)),
    ('A whole home with three bedrooms; one bedroom each.', (3, 3)),
    ('我们各有一间卧室。', None),
    ('Each of us needs one bedroom.', None),
    ('不是正好2间卧室，至少三间卧室。', (3, 6)),
])
def test_total_bedroom_count_outranks_per_person_allocation(message, expected):
    from core.tools.search_properties import _extract_bedrooms
    assert _extract_bedrooms(message) == expected


@pytest.mark.parametrize('message', [
    '替换此前的默认：整套月租最高£2400。',
    '请覆盖旧的默认偏好，房租上限为£2400每月。',
    'Replace my previous defaults: the rent ceiling is £2400 per month.',
    'Supersede the existing saved preferences; rent at most £2400 pcm.',
])
def test_explicit_default_replacement_is_a_budget_revision(message):
    from core.constraint_updates import explicit_criteria_revision
    assert explicit_criteria_revision(message) is True
    assert not detect_history_conflicts([{'user': 'My absolute maximum rent is £2166.67 per month.'}], message)


@pytest.mark.parametrize('message', [
    '不要替换此前默认。', '是否替换此前默认？', '假如替换此前默认，会有什么影响？',
    'Do not replace my previous defaults.', 'Should I replace my previous defaults?',
    'We have existing saved preferences.',
])
def test_negated_question_or_reference_does_not_authorize_default_replacement(message):
    from core.constraint_updates import explicit_criteria_revision
    assert explicit_criteria_revision(message) is False


@pytest.mark.parametrize('message,expected', [
    ('这条消息只保存默认，不要搜索房源。', False),
    ('只保存，不用比较或搜索房源。', False),
    ('Only save my defaults; do not search for properties.', False),
    ("Save this only. Don't run a new search.", False),
    ('Do not search, but compare Camden and Leyton.', True),
    ('请保存默认，不要搜索；但请复述当前条件。', True),
    ('Save my defaults and please compare the two areas.', True),
    ('保存偏好并搜索Camden的房源。', True),
])
def test_only_positive_independent_tasks_survive_memory_save_boundary(message, expected):
    from core.constraint_updates import independent_memory_task
    assert independent_memory_task(message) is expected


@pytest.mark.parametrize('message,expected', [
    ('请记住安静偏好，并查明天Camden天气。', True),
    ('Remember quiet homes and check the weather.', True),
    ('保存我的偏好，再查一下明天的天气预报。', True),
    ('Remember my budget and look up the current peak single fare.', True),
    ('记住喜欢安静，并查询Central line的线路状态。', True),
    ('记住公共交通30分钟以内的偏好。', False),
    ('只保存偏好，不要查明天Camden天气。', False),
    ('Remember quiet homes. Do not check the weather.', False),
    ('Save the commute preference; do not check the fare or search.', False),
    ('不要搜索，也不要查天气，但请解释票价的计算方式。', True),
])
def test_positive_ancillary_tasks_remain_independent_of_memory_save(message, expected):
    from core.constraint_updates import independent_memory_task
    assert independent_memory_task(message) is expected


PROFESSIONAL_COORDINATED_CAPS = ("I'm moving to London for a job near Bank station. I'll be in the office three days a week. "
    "Please help me find a self-contained one-bedroom flat in Lewisham or Stratford, "
    "with rent up to £1,850 per month and rent plus commuting no more than £2,150 per month. "
    "I'd like a public-transport commute of about 45 minutes or less. What suitable places can you show me?")


@pytest.mark.parametrize('message,rent,total', [
    (PROFESSIONAL_COORDINATED_CAPS, 1850, 2150),
    ('Rent up to £1850 per month and rent plus commuting at most £2150 per month.', 1850, 2150),
    ('Rent plus commuting up to £2150 a month and rent alone at most £1850 a month.', 1850, 2150),
    ('£2150 a month for rent and commuting and £1850 pcm for rent.', 1850, 2150),
    ('My budget is £1850 per month and the combined rent-plus-transport cap is £2150 per month.', 1850, 2150),
    ('租金最高£1850/月和租金加交通总预算最高£2150/月。', 1850, 2150),
])
def test_conjoined_amounts_do_not_borrow_the_next_quantitys_role(message, rent, total):
    assert _extract_budget(message) == (rent, 'month')
    assert _extract_total_budget(message) == total
    result = _apply_explicit_criteria_updates({}, message)
    assert result['max_budget'] == rent and result['monthly_total_budget'] == total
    assert not detect_history_conflicts([], message)


@pytest.mark.parametrize('message', [
    'My budget is £2150 per month for rent and commuting.',
    '£2150 per month is the total rent-plus-commuting cap.',
    'Rent and transport must together be no more than £2150 a month.',
])
def test_one_combined_amount_does_not_create_a_rent_ceiling(message):
    assert _extract_total_budget(message) == 2150
    assert _extract_budget(message) == (None, None)


def test_actual_executor_registry_and_tool_wrapper_receive_distinct_rent_and_total_caps(monkeypatch, tmp_path):
    import asyncio
    from core.tool_system import ToolRegistry
    from core.tools.search_properties import search_properties_tool
    from core.langgraph_agent import _make_execute_tool_node
    from uk_rent_agent.tools.idempotency import IdempotencyStore

    received = []
    async def capture_search(**params):
        received.append(params)
        return {'success': True, 'status': 'no_results', 'recommendations': [],
                'search_criteria': {'area': params['area'], 'areas': params['areas'],
                    'max_budget': params['max_budget'], 'monthly_total_budget': params['monthly_total_budget'],
                    'office_days_per_week': params['office_days_per_week']}}
    # Preserve the real Tool instance and its strict input model; only the external
    # search implementation is replaced. The executor, registry and validation run.
    monkeypatch.setattr(search_properties_tool, 'func', capture_search)
    registry = ToolRegistry(idempotency_store=IdempotencyStore(tmp_path / 'idempotency.sqlite3'))
    registry.register(search_properties_tool)
    accumulated = _apply_explicit_criteria_updates({}, PROFESSIONAL_COORDINATED_CAPS)
    command = asyncio.run(_make_execute_tool_node(registry)({
        'tool_decision': {'tool': 'search_properties', 'params': {
            'area': 'Stratford', 'areas': ['Stratford'], 'max_budget': 2150, 'budget_period': 'week'}},
        'accumulated_search_criteria': accumulated,
        'extracted_context': {'current_message': PROFESSIONAL_COORDINATED_CAPS},
        'user_query': PROFESSIONAL_COORDINATED_CAPS,
    }))
    assert len(received) == 1, command.update.get('tool_observation')
    params = received[0]
    assert params['max_budget'] == 1850 and params['budget_period'] == 'month'
    assert params['monthly_total_budget'] == 2150 and params['office_days_per_week'] == 3
    assert params['area'] == 'Lewisham' and params['areas'] == ['Lewisham', 'Stratford']
    assert registry.get_stats()['search_properties']['successful_calls'] == 1
    assert command.update['tool_raw_data']['search_criteria']['max_budget'] == 1850


FAMILY_SCOPE_R1 = ("Could you help us find a family home in Stratford or Leyton? There are two adults, our seven-year-old and a cat. "
    "We need a whole place with at least two bedrooms, paying no more than £2,800 a month in rent before bills, "
    "and a public-transport journey to King's Cross of about 45 minutes or less. "
    "I use a manual wheelchair that's 68 cm wide: step-free entry and usable space through the doors and bathroom are essential. "
    "Our cat must be allowed, and we'd need written consent before committing. "
    "A balcony or small garden would be lovely but isn't essential. "
    "Please show available candidates and be clear about which of those needs the listings actually evidence.")
FAMILY_SCOPE_R2 = ("Let's look at Tennyson Road, Stratford first, and compare it with Borthwick Road. "
    "For our family the step-free route in, enough space for my 68 cm wheelchair through the doors and bathroom, "
    "and permission for our cat are requirements, not preferences. "
    "What do the actual details for these two homes confirm, and what is still unknown? "
    "Does either have a written cat permission statement or measurements for wheelchair access? "
    "A lift on its own would not answer the space question. Please keep our other search limits.")


def test_exact_family_feature_scopes_preserve_hard_essential_and_soft_balcony():
    from core.langgraph_agent import extract_preferences_from_message
    acc = _apply_explicit_criteria_updates({}, FAMILY_SCOPE_R1)
    assert {'step-free', 'wheelchair-accessible', 'cats allowed'} <= set(acc['property_features'])
    assert 'lift' not in acc['property_features'] and 'balcony' not in acc['property_features']
    assert acc['areas'] == ['Stratford', 'Leyton']
    prefs = extract_preferences_from_message(FAMILY_SCOPE_R1, {})
    assert not any('step-free' in item for item in prefs.get('soft_preferences', []))
    assert 'balcony' not in prefs.get('required_amenities', [])
    assert any('balcony' in item for item in prefs.get('soft_preferences', []))


def test_exact_read_only_family_comparison_can_reaffirm_omitted_hard_features_only():
    from copy import deepcopy
    previous = {'area': 'Stratford', 'areas': ['Stratford', 'Leyton'], 'max_budget': 2800,
        'bedrooms': 2, 'bedrooms_minimum': True, 'room_type': 'whole', 'max_travel_time': 45,
        'commute_destination': "King's Cross", 'property_features': [],
        'soft_preferences': ['Would like step-free', 'Would like balcony']}
    before = deepcopy(previous)
    acc = _apply_explicit_criteria_updates(previous, FAMILY_SCOPE_R2)
    assert {'step-free', 'wheelchair-accessible', 'cats allowed'} <= set(acc['property_features'])
    assert 'lift' not in acc['property_features']
    assert not any('step-free' in item for item in acc['soft_preferences'])
    for field in ('area', 'areas', 'max_budget', 'bedrooms', 'bedrooms_minimum', 'room_type',
                  'max_travel_time', 'commute_destination'):
        assert acc[field] == before[field]
    assert previous == before


@pytest.mark.parametrize('message,hard,soft', [
    ('Step-free entry is essential. A balcony would be lovely but is not essential.', {'step-free'}, set()),
    ("A step-free entrance isn't essential; I would like a lift.", set(), {'step-free', 'lift'}),
    ('A lift on its own would not answer the space question.', set(), set()),
    ('A lift alone does not prove wheelchair access.', set(), set()),
    ('I use a 68 cm wheelchair. Enough internal space through the doors and bathroom is required.', {'wheelchair-accessible'}, set()),
    ('Does either have measurements for wheelchair access?', set(), set()),
    ("Our cat must be allowed, and we'd need written consent.", set(), set()),
])
def test_feature_predicate_strength_and_negation_scope(message, hard, soft):
    from core.langgraph_agent import _extract_accessibility_requirements
    result = _extract_accessibility_requirements(message)
    assert set(result['required']) == hard and set(result['soft']) == soft


def test_cat_contracted_need_for_written_consent_is_a_requirement():
    acc = _apply_explicit_criteria_updates({}, "Find a home. Our cat must be allowed, and we'd need written consent.")
    assert 'cats allowed' in acc['property_features']


def test_family_explicit_journey_destination_is_persisted_without_inventing_an_address():
    acc = _apply_explicit_criteria_updates({}, FAMILY_SCOPE_R1)
    assert acc['commute_destination'] == acc['destination'] == "King's Cross"
    assert acc['max_travel_time'] == 45 and acc['no_commute'] is False


def test_exact_family_requires_68cm_usable_space_and_written_cat_consent_separately():
    acc = _apply_explicit_criteria_updates({}, FAMILY_SCOPE_R1)
    assert {'step-free', 'wheelchair-accessible', 'cats allowed', 'wheelchair_space_68cm', 'cat_written_consent'} <= set(acc['property_features'])
    # Generic advertising or a lift cannot certify the declared width and written consent.
    broad_advert = {'Detailed_Amenities': ['Wheelchair accessible', 'Step-free', 'Lift'], 'Cats_Allowed': True}
    assert _property_feature_evidence(broad_advert, 'wheelchair_space_68cm') == 'unverified'
    assert _property_feature_evidence(broad_advert, 'cat_written_consent') == 'unverified'
    reaffirmed = _apply_explicit_criteria_updates(acc, FAMILY_SCOPE_R2)
    assert {'wheelchair_space_68cm', 'cat_written_consent'} <= set(reaffirmed['property_features'])
    assert 'lift' not in reaffirmed['property_features']


@pytest.mark.parametrize('message', [
    'Find a wheelchair-accessible home. Our cat must be allowed.',
    'We need wide doors. I have a wheelchair and a cat.',
    'Does this home have space for a 68 cm wheelchair or written cat consent?',
])
def test_precise_fit_and_written_consent_are_never_invented_from_generic_needs(message):
    acc = _apply_explicit_criteria_updates({}, message)
    assert 'wheelchair_space_68cm' not in acc.get('property_features', [])
    assert 'cat_written_consent' not in acc.get('property_features', [])


R6_SEPARATE_CAPS = ('Keep my current £2,000/month rent cap separate from the £2,150/month '
                    'rent-plus-commute ceiling, based on two office days a week')


@pytest.mark.parametrize('message', [
    R6_SEPARATE_CAPS,
    'Keep the £2000/month rent cap apart from the £2150/month rent-plus-commuting cap.',
    'The £2000 pcm rent ceiling is separate from the £2150 pcm rent-and-transport budget.',
    'The £2150/month rent-plus-commute ceiling is separate from the £2000/month rent cap.',
])
def test_separate_from_and_hyphenated_commute_budget_own_their_amounts(message):
    assert _extract_budget(message) == (2000, 'month')
    assert _extract_total_budget(message) == 2150
    acc = _apply_explicit_criteria_updates({'max_budget': 2150, 'max_budget_monthly': 2150,
                                          'budget_period': 'month'}, message)
    assert acc['max_budget'] == acc['max_budget_monthly'] == 2000
    assert acc['monthly_total_budget'] == 2150


def test_r6_bad_source_repairs_without_a_model_and_keeps_the_monthly_alias_coherent():
    from core.criteria_recovery import repair_criteria_sources
    from core.criteria_contract import reconcile_search_criteria
    original = {'max_budget': 2150, 'max_budget_monthly': 2000, 'budget_period': 'month',
        'monthly_total_budget': 2150, 'constraint_sources': {
            'max_budget': {'source': 'user_message', 'value': 2150, 'text': R6_SEPARATE_CAPS},
            'budget_period': {'source': 'form', 'value': 'month'}}}
    repaired = repair_criteria_sources(original, [{'id': 42, 'role': 'user', 'content': R6_SEPARATE_CAPS}])
    assert repaired['max_budget'] == repaired['max_budget_monthly'] == 2000
    assert repaired['monthly_total_budget'] == 2150
    assert repaired['constraint_sources']['max_budget']['source'] == 'criteria_recovery'
    assert repaired['constraint_sources']['max_budget']['original_source'] == original['constraint_sources']['max_budget']
    assert reconcile_search_criteria(repaired, {'max_budget': 2150, 'max_budget_monthly': 2150})['max_budget_monthly'] == 2000
    assert original['max_budget'] == 2150


def test_r6_already_recovered_source_normalizes_an_old_monthly_alias_without_replaying():
    from core.criteria_recovery import repair_criteria_sources
    old = {'max_budget': 2000, 'max_budget_monthly': 2150, 'budget_period': 'month',
           'constraint_sources': {'max_budget': {'source': 'criteria_recovery', 'value': 2000}}}
    result = repair_criteria_sources(old, [])
    assert result['max_budget'] == result['max_budget_monthly'] == 2000


def test_month_budget_alias_respects_confirmed_clear_and_does_not_guess_week_units():
    from core.criteria_contract import reconcile_search_criteria, synchronize_monthly_budget_alias, apply_form_criteria
    cleared = {'max_budget': None, 'max_budget_monthly': 2150,
               'constraint_sources': {'max_budget': {'source': 'form', 'value': None}}}
    out = reconcile_search_criteria(cleared, {'max_budget': 2150, 'max_budget_monthly': 2150, 'budget_period': 'month'})
    assert out['max_budget'] is None and out['max_budget_monthly'] is None
    weekly = {'max_budget': 350, 'max_budget_monthly': 2000, 'budget_period': 'week',
              'constraint_sources': {'max_budget': {'source': 'form', 'value': 350}}}
    assert synchronize_monthly_budget_alias(weekly) is weekly
    form = apply_form_criteria({'max_budget_monthly': 2000}, {'max_budget': 350, 'budget_period': 'week'})
    assert 'max_budget_monthly' not in form


def test_unproven_bad_user_source_cannot_widen_a_stricter_monthly_alias():
    from core.criteria_contract import synchronize_monthly_budget_alias
    original = {'max_budget': 2150, 'max_budget_monthly': 2000, 'budget_period': 'month',
        'constraint_sources': {'max_budget': {'source': 'user_message', 'value': 2150, 'text': R6_SEPARATE_CAPS}}}
    assert synchronize_monthly_budget_alias(original) is original
