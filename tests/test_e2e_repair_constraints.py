"""User-visible failures from the three-persona audit, with independent expectations."""
import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates, _make_extract_preferences_node
from core.tools.search_properties import _extract_budget, _extract_area, _extract_room_type
from core.constraint_updates import restore_saved_criteria
from core.context_assembler import detect_history_conflicts
from core.tool_policy import standalone_rent_conversion, read_tool_denial


@pytest.mark.parametrize('message,expected', [
    ('整套恰好两卧室，总租金最高£2400/月，也就是每人最多£1200。', (2400, 'month')),
    ('Keep rent capped at £1800 per month; rent plus transport total budget £2100 per month.', (1800, 'month')),
    ('Now two office days weekly; total budget £1950 monthly including rent and transport.', (None, None)),
    ('我们三人，每人最多£650/月，希望整租三卧。', (1950, 'month')),
    ('Two people, per person at most £700 per month, a whole two-bedroom flat.', (1400, 'month')),
])
def test_money_amounts_keep_their_meaning(message, expected):
    assert _extract_budget(message) == expected


def test_combined_budget_does_not_raise_rent_limit():
    result = _apply_explicit_criteria_updates({'max_budget': 1800},
        'Keep rent capped at £1800 per month; rent plus transport total budget £2100 per month.')
    assert result['max_budget'] == 1800 and result['monthly_total_budget'] == 2100
    next_result = _apply_explicit_criteria_updates(result,
        'Now two office days weekly; total budget £1950 monthly including rent and transport.')
    assert next_result['max_budget'] == 1800 and next_result['monthly_total_budget'] == 1950


def test_home_area_wins_over_school_city_and_old_tool_area():
    message = '我在伦敦UCL读研，先看看Watford，预算£1800/月，整套一卧室，不要合租。'
    assert _extract_area(message) == 'Watford'
    result = _apply_explicit_criteria_updates({}, message)
    assert result['area'] == 'Watford' and result['areas'] == ['Watford']
    switched = _apply_explicit_criteria_updates(result, 'Search Camden instead of Watford.')
    assert switched['area'] == 'Camden' and switched['areas'] == ['Camden']


@pytest.mark.parametrize('message', [
    'Check weather in Camden. Leave housing search criteria unchanged.',
    'What is the weather in Camden tomorrow?',
    'Please keep the separate Camden studio search out of this decision.',
    '现在另外帮一位新同学，她的学校、区域、预算和房型都还没确认。先不要搜索，先确认她要住整套还是合租。',
])
def test_auxiliary_and_unconfirmed_mentions_do_not_mutate_criteria(message):
    previous = {'area': 'Watford', 'room_type': 'whole', 'bedrooms': 1, 'max_budget': 1800}
    assert _apply_explicit_criteria_updates(previous, message) == previous
    if '新同学' in message or 'separate' in message:
        assert _extract_room_type(message) is None


def test_discarded_experiment_does_not_override_restored_conditions():
    message = 'Discard that £500/four-bedroom experiment. Restore Watford only up to £2500 per month, at least two bedrooms.'
    result = _apply_explicit_criteria_updates({'area': 'Camden', 'max_budget': 500, 'bedrooms': 4}, message)
    assert result['max_budget'] == 2500 and result['bedrooms'] == 2 and result['bedrooms_minimum']
    assert result['area'] == 'Watford'
    history = [{'user': 'My absolute max is £500 per month'}, {'user': 'Budget £3000 monthly'}]
    assert detect_history_conflicts(history, message) == []


def test_lift_optional_demotes_existing_hard_requirement():
    previous = {'property_features': ['step-free', 'lift']}
    result = _apply_explicit_criteria_updates(previous, 'A lift is optional, not required; keep step-free access.')
    assert 'lift' not in result['property_features'] and 'step-free' in result['property_features']
    assert _apply_explicit_criteria_updates({}, "Don't assume lift means accessible.").get('property_features', []) == []


def test_explicit_default_reference_enforces_saved_filters_without_automatic_bleed():
    memories = [{'id': 'mine', 'mtype': 'semantic', 'text': '用户默认找房偏好（长期）：在 UCL 主校区读研，希望用中文交流；默认先看 Watford；月租最高 £1800；要求独住整租一卧室（不要合租房间）；通勤时间暂不设硬限制；靠近超市只是加分项，不是必须。此前讨论的两人两卧整租和合租单间只是临时比较，不属于长期偏好。'}]
    result = restore_saved_criteria({}, memories, '按你刚复述的长期默认条件帮我搜三套房。')
    assert result['area'] == 'Watford' and result['max_budget'] == 1800
    assert result['room_type'] == 'whole' and result['bedrooms'] == 1
    assert restore_saved_criteria({}, memories, '帮一个新同学找房。') == {}
    assert restore_saved_criteria({}, [], '按长期默认条件找房。') == {}


def test_preferences_only_read_the_current_housing_message():
    state = {'user_query': 'Previous: must have lift. Current: What is weather in Camden?',
             'extracted_context': {'current_message': 'What is weather in Camden?'},
             'user_preferences': {}, 'accumulated_search_criteria': {'area': 'Watford'}}
    result = _make_extract_preferences_node()(state)
    assert not result['user_preferences'].get('required_amenities')
    assert 'accumulated_search_criteria' not in result


def test_mixed_commute_cost_is_not_a_rent_conversion():
    assert standalone_rent_conversion('Rent is £1800 per month. Three return trips per week: what is my total monthly cost including transport?') is None
    assert standalone_rent_conversion('Convert £1800 per month into weekly rent.')[0] == 'month_to_week'


@pytest.mark.parametrize('tool,args', [
    ('calculate_commute', {}), ('calculate_commute_cost', {}),
    ('get_transport_info', {'query_type': 'journey'}),
])
def test_explicit_no_commute_is_a_dispatch_boundary(tool, args):
    assert read_tool_denial(tool, args, current_message="Don't run commute. Check weather only.") is not None
    assert read_tool_denial(tool, args, current_message='No hard commute filter. Check the journey.') is None


def test_explicit_fare_query_gets_one_bounded_lookup_without_rent_search():
    from core.agent_loop import _explicit_transport_reads
    message = "Please look up adult peak and off-peak single fares from Watford Junction to King's Cross St Pancras on Oyster."
    calls = _explicit_transport_reads({}, [], message)
    assert len(calls) == 1 and calls[0][0] == 'get_transport_info'
    assert calls[0][1]['query_type'] == 'fare'
    assert calls[0][1]['from_location'] == 'Watford Junction'
    assert calls[0][1]['to_location'] == "King's Cross St Pancras"
    assert _explicit_transport_reads({'tool_artifacts': [{'tool': 'get_transport_info', 'raw_data': {'query_type': 'fare'}}]}, [], message) == []


def test_latest_office_days_are_structured():
    result = _apply_explicit_criteria_updates({}, 'I work at Kings Cross three days a week. Budget £1800 monthly.')
    assert result['office_days_per_week'] == 3
    result = _apply_explicit_criteria_updates(result, 'My employer reduced office attendance to two days per week. Keep rent below £1800.')
    assert result['office_days_per_week'] == 2 and result['max_budget'] == 1800


def test_total_only_allowance_and_named_office_are_distinct_slots():
    message = ("I'm moving to London on my own and need a self-contained whole flat, not a room in a shared place. "
               "My office is near Bank station and I'll be there three days a week. "
               "My maximum is £2,200 a month including rent and my commute. Can you find a few suitable options?")
    result = _apply_explicit_criteria_updates({}, message)
    assert result['commute_destination'] == 'Bank station, London'
    assert result['monthly_total_budget'] == 2200
    assert result.get('max_budget') is None
    corrected = _apply_explicit_criteria_updates({'commute_destination': 'My office'},
        'Yes, use Bank station as the exact commute destination. My total monthly budget is £2200 including rent plus travel.')
    assert corrected['commute_destination'] == 'Bank station, London'


def test_lift_preference_not_requirement_or_proof_of_accessibility():
    message = ('Step-free access from the street and enough internal space are essential. '
               'A lift would be a preference, not a requirement; a genuinely step-free ground-floor home is fine. '
               'A lift alone must not be treated as proof of accessibility.')
    result = _apply_explicit_criteria_updates({'property_features': ['step-free', 'lift']}, message)
    assert 'lift' not in result['property_features']
    assert any('lift' in value for value in result['soft_preferences'])


def test_studio_details_question_is_read_only_and_not_a_refinement():
    message = ('Tell me more about the £1300/month studio on The Vale, London, listing 14672649: '
               'the full rent, deposit or fees if known, furnishing, availability, and anything missing '
               'from the listing that I should check before viewing. Please use its actual listing details and mark unknowns.')
    prior = {'room_type': 'whole', 'max_budget': 1800, 'bedrooms': 1}
    assert _apply_explicit_criteria_updates(prior, message) == prior
    from core.tools.search_properties import _extract_bedrooms
    assert _extract_bedrooms(message) is None and _extract_room_type(message) is None


def test_budget_clarification_preserves_independent_bedroom_change():
    message = ('有一个同学不来了，现在只剩我们两个人，想改为2居整套，预算也改成£1,200。'
               '我说得可能不清楚，你先给我明确的预算口径选项让我选，不要擅自决定是每人还是整套。')
    result = _apply_explicit_criteria_updates({'bedrooms': 3, 'max_budget': 3000, 'room_type': 'whole'}, message)
    assert result['bedrooms'] == 2 and result['max_budget'] == 3000
    assert _extract_budget(message) == (None, None)


def test_option_code_resolves_only_to_latest_owned_question_label():
    from core.constraint_updates import resolve_clarification_reply
    messages = [{'role': 'assistant', 'response_type': 'clarification', 'options': [
        {'label': '每人每月 £1,200（整套合计 £2,400）', 'value': 'per_person_1200'}]}]
    resolved = resolve_clarification_reply('per_person_1200', messages)
    assert _extract_budget(resolved) == (2400, 'month')
    assert resolve_clarification_reply('per_person_1200', messages + [{'role': 'assistant', 'content': 'Done'}]) == 'per_person_1200'


def test_saved_default_without_residential_area_still_restores_known_filters():
    text = ('长期默认租房偏好：在伦敦找整租2居室（whole flat, 2 bedrooms），整套月租上限 £2,400/月（不含账单），'
            '两人平均分摊（每人约 £1,200/月）；坐公共交通到 UCL Bloomsbury 主校区'
            '（Gower Street, London WC1E 6BT）通勤 30 分钟以内；目前尚未决定居住区域，UCL 只是上课地点。')
    result = restore_saved_criteria({}, [{'id': 'own', 'mtype': 'semantic', 'text': text}],
        '请按我之前保存的长期默认继续找房，说明实际读取条件。')
    assert result['max_budget'] == 2400 and result['bedrooms'] == 2 and result['room_type'] == 'whole'
    assert result['max_travel_time'] == 30


def test_housing_must_stay_is_not_a_weather_area_switch():
    prior = {'area': 'Watford', 'areas': ['Watford'], 'max_budget': 2500}
    result = _apply_explicit_criteria_updates(prior,
        "Don't run commute. Camden is only a weather question: housing must stay Watford with all criteria unchanged.")
    assert result == prior


def test_replacing_minimum_bedroom_count_in_words():
    from core.tools.search_properties import _extract_bedrooms
    assert _extract_bedrooms('Replace the minimum bedroom count with four.') == (4, 6)


def test_recap_uses_current_frequency_and_native_favourites():
    from core.constraint_updates import validate_current_criteria_recap
    out = validate_current_criteria_recap(
        'Nothing was saved.\nThe practical route is to favourite it yourself on the OnTheMarket listing page.\n'
        'One correction: your criteria block currently records one office day a week, while this note says five.',
        {'office_days_per_week': 5}, message='Save this flat as a favourite.')
    assert 'Nothing was saved.' in out
    assert 'records one' not in out and '5 office days' in out
    assert 'OnTheMarket listing page' not in out and 'heart/Save button' in out


def test_runtime_criteria_do_not_repeat_old_plan_or_source_values():
    from core.loop_prompts import render_accumulated_criteria
    out = render_accumulated_criteria({'office_days_per_week': 5,
        'plans': {'old': {'office_days_per_week': 1}},
        'constraint_sources': {'office_days_per_week': {'value': 1}}})
    assert 'office_days_per_week: 5' in out and 'old' not in out and "'value': 1" not in out


def test_unchosen_alternative_amounts_do_not_set_a_budget():
    assert _extract_budget('Budget £1200 or £2400 per person; I have not chosen yet.') == (None, None)


def test_search_uses_total_as_necessary_rent_bound_and_respects_no_commute(tmp_path):
    from core.agent_loop import build_fc_nodes
    from core.tool_system import Tool
    from core.tools.search_properties import search_properties_tool
    from tests.test_manager_v1_specialist_dispatch import _registry, _state, _tc, _execute
    observed = []

    async def search(**kwargs):
        observed.append(kwargs)
        return {'status': 'found', 'recommendations': [], 'search_criteria': {
            'max_budget': kwargs.get('max_budget'), 'max_commute_time': 50,
            'commute_destination': 'UCL, London', 'no_commute': kwargs.get('no_commute')}}

    registry = _registry(tmp_path, [Tool(name='search_properties', description='probe', func=search,
        parameters=search_properties_tool.parameters, input_model=search_properties_tool.input_model)])
    state = _state([_tc('search_properties', {'area': 'London'}, 'search')],
        message="Find whole flats in London. My total monthly budget is £2200 including rent plus travel. Don't run commute.")
    state['accumulated_search_criteria'] = {'area': 'London', 'monthly_total_budget': 2200,
        'max_travel_time': 50, 'commute_destination': 'UCL, London', 'no_commute': False}
    final = _execute(build_fc_nodes(registry), state)
    assert len(observed) == 1 and observed[0]['max_budget'] == 2200
    assert observed[0]['no_commute'] and not observed[0].get('max_commute_time')
    assert [a['tool'] for a in final['tool_artifacts']] == ['search_properties']
    assert final['accumulated_search_criteria']['no_commute'] is False


def test_explicit_memory_save_cannot_be_swallowed_by_refinement(monkeypatch):
    from types import SimpleNamespace
    import core.agent_loop as loop
    from tests.test_dimension_fanout import _provider, _state_for
    called = []
    monkeypatch.setattr(loop, '_load_memory_gate', lambda: SimpleNamespace(user_authorizes_memory=lambda _m: True))
    monkeypatch.setattr(loop.refine_results, 'plan_refinement', lambda *_a: called.append(True))
    state = _state_for('记住这个长期默认：预算£2400、整租2居，请以后沿用。')
    state['extracted_context']['last_results_full'] = [{'address': 'Camden', 'price': 1500}]
    command = loop.build_fc_nodes(_provider())['guard'](state)
    assert command.goto == 'agent' and called == []
