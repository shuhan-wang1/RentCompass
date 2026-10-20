"""Recovery keeps singleton area values and their proven shared sources coherent."""
from copy import deepcopy
import pytest
from core.criteria_recovery import repair_criteria_sources
from core.langgraph_agent import update_search_criteria

TEXT = '我下学期要来伦敦读研，想在Camden找个住处。能帮我看看吗？'


def source(value, kind='user_message', text=TEXT):
    return {'source': kind, 'value': value, 'text': text}


class NoReplay(list):
    def __iter__(self):
        raise AssertionError('Alias-only recovery must not replay history')


def snapshot(repaired=False, areas=None):
    area = 'Camden' if repaired else 'London'
    area_source = ({'source': 'criteria_recovery', 'value': 'Camden',
                    'original_source': source('London'), 'message_id': 5,
                    'reason': 'parser_value_corrected'} if repaired else source('London'))
    return {'area': area, 'areas': areas if areas is not None else [area],
            'constraint_sources': {'area': area_source, 'areas': source(['London'])}}


def assert_fixed(out):
    assert out['area'] == 'Camden' and out['areas'] == ['Camden']
    record = out['constraint_sources']['areas']
    assert record['source'] == 'criteria_recovery' and record['value'] == ['Camden']
    assert record['original_source']['value'] == ['London']
    assert record['original_source']['text'] == TEXT
    assert update_search_criteria(out, {'area': 'London', 'areas': ['London']})['areas'] == ['Camden']
    assert repair_criteria_sources(out, NoReplay()) is out


def test_fresh_scalar_recovery_updates_the_same_source_singleton_alias():
    old = snapshot(); before = deepcopy(old)
    out = repair_criteria_sources(old, [{'role': 'user', 'content': TEXT, 'id': 5}])
    assert_fixed(out)
    assert old == before


@pytest.mark.parametrize('areas', [['Camden'], ['London']])
def test_existing_recovered_snapshot_repairs_its_stale_source_without_history(areas):
    old = snapshot(repaired=True, areas=areas); before = deepcopy(old)
    out = repair_criteria_sources(old, NoReplay())
    assert_fixed(out)
    assert old == before


@pytest.mark.parametrize('kind', ['form', 'manual', 'saved_preference'])
def test_independent_protected_singleton_source_is_preserved(kind):
    old = snapshot(repaired=True)
    old['constraint_sources']['areas'] = source(['London'], kind)
    assert repair_criteria_sources(old, NoReplay()) is old


def test_other_user_instruction_is_not_the_scalar_sources_alias():
    old = snapshot(repaired=True)
    old['constraint_sources']['areas'] = source(['London'], text='Search London only.')
    assert repair_criteria_sources(old, NoReplay()) is old


@pytest.mark.parametrize('areas', [['Camden', 'Leyton'], ['Greenwich']])
def test_multi_area_choice_or_unrecorded_different_value_is_preserved(areas):
    old = snapshot(repaired=True, areas=areas)
    assert repair_criteria_sources(old, NoReplay()) is old


def test_stale_scalar_recovery_record_cannot_own_a_later_manual_area():
    old = snapshot(repaired=True)
    old['area'] = 'Greenwich'
    assert repair_criteria_sources(old, NoReplay()) is old


LIFT_NOTE = ("For our family step-free access and wheelchair space are requirements. "
             "A lift on its own would not answer the space question.")


def test_erroneous_lift_from_insufficiency_note_is_recovered_without_touching_other_features():
    old = {'property_features': ['lift', 'step-free', 'cats allowed'], 'constraint_sources': {
        'property_features': source(['lift', 'step-free', 'cats allowed'], text=LIFT_NOTE)}}
    before = deepcopy(old)
    out = repair_criteria_sources(old, [
        {'role': 'user', 'content': 'Step-free access is essential. Our cat must be allowed.', 'id': 1},
        {'role': 'user', 'content': LIFT_NOTE, 'id': 2}])
    assert out['property_features'] == ['step-free', 'cats allowed']
    assert out['constraint_sources']['property_features']['source'] == 'criteria_recovery'
    assert out['constraint_sources']['property_features']['original_source'] == before['constraint_sources']['property_features']
    assert old == before and repair_criteria_sources(out, NoReplay()) is out


def test_lift_insufficiency_note_does_not_revoke_an_actual_lift_requirement():
    old = {'property_features': ['lift', 'step-free'], 'constraint_sources': {
        'property_features': source(['lift', 'step-free'], text=LIFT_NOTE)}}
    out = repair_criteria_sources(old, [
        {'role': 'user', 'content': 'I need a lift in the building.', 'id': 1},
        {'role': 'user', 'content': LIFT_NOTE, 'id': 2}])
    assert out is old


@pytest.mark.parametrize('kind', ['form', 'manual', 'saved_preference'])
def test_lift_insufficiency_note_cannot_override_protected_features(kind):
    old = {'property_features': ['lift'], 'constraint_sources': {'property_features': source(['lift'], kind, LIFT_NOTE)}}
    assert repair_criteria_sources(old, NoReplay()) is old
