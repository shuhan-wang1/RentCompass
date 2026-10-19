"""Replay failures seen after restart without relying on model phrasing."""
from types import SimpleNamespace

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates
from core.constraint_updates import restore_saved_criteria
from rag.agent_memory import AgentMemory


@pytest.mark.parametrize('opening', ['Search Camden only', 'Keep Camden only'])
def test_explicit_residential_place_replaces_previous_search(opening):
    result = _apply_explicit_criteria_updates({'area': 'Watford', 'areas': ['Watford']},
        opening + ', £3,000 per month, a whole home with at least two bedrooms.')
    assert result['area'] == 'Camden' and result['areas'] == ['Camden']
    assert result['max_budget'] == 3000 and result['bedrooms'] == 2


def test_day_trip_weather_and_negative_housing_instructions_are_read_only():
    previous = {'area': 'Camden', 'areas': ['Camden'], 'max_budget': 3000,
        'bedrooms': 2, 'room_type': 'whole', 'property_features': ['step-free', 'pet-friendly']}
    message = ('For a separate day trip, what is the current weather in Islington, London? '
        "Don't run a commute calculation this round. This is only a weather lookup: "
        'do not change any housing-search area, budget, bedroom, access, pet or other condition, '
        'and do not move the housing search to Islington.')
    assert _apply_explicit_criteria_updates(previous, message) == previous


def test_mixed_request_applies_positive_housing_action_without_weather_location():
    result = _apply_explicit_criteria_updates({'area': 'Watford'},
        'Search Camden only, whole flats with two bedrooms under £3000 per month. '
        'Also check the weather in Islington for a separate day trip.')
    assert result['area'] == 'Camden' and result['max_budget'] == 3000
    assert result['bedrooms'] == 2 and result['room_type'] == 'whole'


def test_explicit_default_beats_newer_temporary_semantic_comparison():
    memories = [
        {'id': 'temporary', 'mtype': 'semantic', 'created_at': '2026-10-19T12:00',
         'text': 'Temporary comparison: search Watford, whole studio, budget £1300 per month.'},
        {'id': 'default', 'mtype': 'semantic', 'created_at': '2026-10-19T10:00',
         'text': '长期默认租房偏好：整租2居室，整套月租上限 £2400/月，通勤到 UCL 主校区30分钟以内。'},
    ]
    restored = restore_saved_criteria({}, memories, '请按我之前保存的长期默认继续搜索。')
    assert restored['max_budget'] == 2400 and restored['bedrooms'] == 2
    assert restored['constraint_sources']['max_budget']['memory_id'] == 'default'


def test_semantic_defaults_do_not_use_relevance_ranking_or_other_identities():
    seen = []
    records = {'ids': ['own', 'foreign', 'episode', 'bad'],
        'documents': ['own durable default', 'foreign secret', 'own episode', 'malformed'],
        'metadatas': [dict(user_id='alice', mtype='semantic', created_at='2026-10-19'),
                      dict(user_id='bob', mtype='semantic'),
                      dict(user_id='alice', mtype='episodic'), None]}
    memory = AgentMemory.__new__(AgentMemory)
    memory.col = SimpleNamespace(get=lambda **kwargs: seen.append(kwargs) or records)
    assert memory.semantic_records(user_id='alice') == [
        dict(id='own', text='own durable default', mtype='semantic', created_at='2026-10-19')]
    assert seen == [{'where': {'$and': [{'user_id': 'alice'}, {'mtype': 'semantic'}]}}]
    for missing in (None, '', 'default'):
        assert memory.semantic_records(user_id=missing) == []
    assert len(seen) == 1


@pytest.mark.parametrize('message', [
    'Search Camden only, a whole home. Our cat needs permission.',
    'Step-free access remains essential, as do permission for our cat and the UCL commute limit.',
    'Permission for our cat is essential. Find whole homes in Camden.',
    '必须允许养猫，整租两居。',
])
def test_explicit_cat_permission_is_a_structured_hard_requirement(message):
    result = _apply_explicit_criteria_updates({}, message)
    assert 'cats allowed' in result['property_features']


@pytest.mark.parametrize('message', [
    'We have a cat. Find whole flats in Camden.',
    'Find whole flats in Camden. Can you tell me whether cats are allowed?',
    'Find whole flats in Camden. Do not add cat permission as a filter.',
    'Find whole flats in Camden. What if my cat needs permission?',
])
def test_cat_question_or_hypothetical_cannot_add_a_filter(message):
    result = _apply_explicit_criteria_updates({}, message)
    assert 'cats allowed' not in (result.get('property_features') or [])


def test_explicit_removal_of_cat_permission_preserves_other_features():
    previous = {'property_features': ['cats allowed', 'step-free']}
    result = _apply_explicit_criteria_updates(previous, 'Remove the cat permission requirement.')
    assert result['property_features'] == ['step-free']
