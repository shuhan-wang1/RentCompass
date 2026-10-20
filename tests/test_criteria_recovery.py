"""Synthetic, same-conversation regressions for legacy criteria repair."""
from copy import deepcopy

import pytest

from core.criteria_recovery import repair_criteria_sources


def source(value, text, kind='user_message'):
    return {'source': kind, 'value': value, 'text': text}


def user(content, ident=1):
    return {'id': ident, 'role': 'user', 'content': content}


class NoReplay(list):
    def __iter__(self):
        raise AssertionError('Valid/repaired sources must not replay normal history')


ORIGINAL = ('Find a whole one-bedroom flat in Watford. My rent budget is £1800 per month. '
            'My office is at Bank station.')
DETAIL = 'Please check the details of this studio and whether bills are included.'


def corrupted_snapshot():
    return {
        'area': 'Watford', 'areas': ['Watford'], 'max_budget': 1800,
        'bedrooms': 0, 'bedrooms_minimum': False, 'room_type': 'studio',
        'commute_destination': 'My office', 'destination': 'My office',
        'monthly_total_budget': 2100, 'office_days_per_week': 2,
        'property_features': ['lift', 'step-free', 'cats allowed'],
        'favorites': [{'url': 'https://example.invalid/saved-1', 'price': 1100}],
        'soft_preferences': ['quiet evenings'],
        'constraint_sources': {
            'room_type': source('studio', DETAIL),
            'bedrooms': source(0, DETAIL),
            'bedrooms_minimum': source(False, ORIGINAL),
            'max_budget': source(1800, 'Form rent budget', 'form'),
            'property_features': source(['lift'], 'I need a lift.'),
        },
    }


def test_read_only_studio_pollution_recovers_whole_bank_and_preserves_other_state():
    old = corrupted_snapshot()
    before = deepcopy(old)
    messages = [user(ORIGINAL), {'role': 'assistant', 'content': 'Here are your one-bedroom flats.'},
                user(DETAIL, 3)]
    out = repair_criteria_sources(old, messages)
    assert out['room_type'] == 'whole' and out['bedrooms'] == 1
    assert 'bank' in out['commute_destination'].lower()
    assert out['destination'] == out['commute_destination']
    for key in ('max_budget', 'monthly_total_budget', 'office_days_per_week', 'favorites',
                'property_features', 'soft_preferences', 'bedrooms_minimum'):
        assert out[key] == before[key]
    assert out['constraint_sources']['room_type']['source'] == 'criteria_recovery'
    assert out['constraint_sources']['room_type']['original_source'] == before['constraint_sources']['room_type']
    assert old == before, 'Recovery must not mutate the loaded snapshot in place'
    assert repair_criteria_sources(out, NoReplay(messages)) is out


def test_excluded_other_studio_and_area_reference_is_not_a_new_choice():
    text = 'Please keep the separate Camden studio search out of this decision.'
    old = {'area': 'Camden', 'areas': ['Camden'], 'room_type': 'studio', 'bedrooms': 0,
           'constraint_sources': {key: source(value, text) for key, value in
                                  [('area', 'Camden'), ('room_type', 'studio'), ('bedrooms', 0)]}}
    out = repair_criteria_sources(old, [user(ORIGINAL), user(text, 2)])
    assert out['area'] == 'Watford' and out['areas'] == ['Watford']
    assert out['room_type'] == 'whole' and out['bedrooms'] == 1


def test_weather_source_restores_residence_without_modifying_valid_form_fields():
    weather = "What's the weather in Camden today? Leave housing search criteria unchanged."
    old = {'area': 'Camden', 'areas': ['Camden'], 'max_budget': 2550, 'room_type': 'shared',
           'constraint_sources': {'area': source('Camden', weather),
                                  'max_budget': source(2550, 'form', 'form'),
                                  'room_type': source('shared', 'manual', 'manual')}}
    out = repair_criteria_sources(old, [user(ORIGINAL), user(weather, 2)])
    assert out['area'] == 'Watford' and out['areas'] == ['Watford']
    assert out['max_budget'] == 2550 and out['room_type'] == 'shared'


def test_new_parser_corrects_whole_rent_instead_of_per_person_share():
    text = '整套总租金预算£2400，每人£1200，两个人合租。只按整套预算找房。'
    old = {'max_budget': 1200, 'favorites': ['saved'],
           'constraint_sources': {'max_budget': source(1200, text)}}
    out = repair_criteria_sources(old, [user(text)])
    assert out['max_budget'] == 2400 and out['favorites'] == ['saved']


def test_ambiguous_budget_does_not_override_later_explicit_whole_rent():
    ambiguous = 'Budget £1200 or £2400 per person; I have not chosen yet.'
    explicit = 'Set the whole-property rent ceiling to £2400 per month.'
    old = {'max_budget': 1200, 'constraint_sources': {'max_budget': source(1200, ambiguous)}}
    out = repair_criteria_sources(old, [user(ambiguous), user(explicit, 2)])
    assert out['max_budget'] == 2400


def test_unselected_amount_is_not_replayed_as_reliable_without_a_prior_budget():
    text = 'Budget £1200 or £2400 per person; I have not chosen yet.'
    old = {'max_budget': 1200, 'constraint_sources': {'max_budget': source(1200, text)}}
    out = repair_criteria_sources(old, [user(text)])
    assert out['max_budget'] is None


def test_total_only_budget_without_prior_rent_evidence_becomes_unknown():
    text = 'My rent plus commuting budget is £2100 a month.'
    old = {'max_budget': 2100, 'monthly_total_budget': 2100,
           'constraint_sources': {'max_budget': source(2100, text)}}
    out = repair_criteria_sources(old, [user(text)])
    assert out['max_budget'] is None
    assert out['monthly_total_budget'] == 2100
    assert out['constraint_sources']['max_budget']['source'] == 'criteria_recovery'


def test_all_legal_form_values_survive_incomplete_history_without_replay():
    old = {'area': 'Islington', 'areas': ['Islington'], 'max_budget': 2600,
           'bedrooms': 2, 'bedrooms_minimum': True, 'room_type': 'whole',
           'constraint_sources': {field: source(value, 'Weather in Camden studio', 'form')
                                  for field, value in [('area', 'Islington'), ('max_budget', 2600),
                                                       ('bedrooms', 2), ('bedrooms_minimum', True),
                                                       ('room_type', 'whole')]}}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_unaffected_valid_user_sources_take_the_fast_path():
    old = {'room_type': 'whole', 'max_budget': 1800,
           'constraint_sources': {'room_type': source('whole', ORIGINAL),
                                  'max_budget': source(1800, ORIGINAL)}}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_unparseable_wording_or_opaque_choice_is_not_proof_of_corruption():
    old = {'room_type': 'whole', 'constraint_sources': {'room_type': source('whole', 'reply_7f')}}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_stale_source_does_not_own_a_later_unrecorded_manual_value():
    old = {'max_budget': 2500, 'constraint_sources': {
        'max_budget': source(1200, 'Budget £1200 or £2400 per person; not chosen.')}}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_explicit_clears_are_valid_including_none_budget():
    old = {'max_budget': None, 'room_type': None, 'bedrooms': None, 'bedrooms_minimum': False,
           'constraint_sources': {
               'max_budget': source(None, 'Remove the budget limit.'),
               'room_type': source(None, 'Clear the room type filter.'),
               'bedrooms': source(None, 'Clear the bedroom filter.'),
               'bedrooms_minimum': source(False, 'Clear the bedroom filter.'),
           }}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_replay_preserves_prior_explicit_clear_instead_of_resurrecting_old_budget():
    question = 'Is this £1495 listing within my £1800 budget?'
    old = {'max_budget': 1495, 'constraint_sources': {'max_budget': source(1495, question)}}
    out = repair_criteria_sources(old, [user(ORIGINAL), user('Remove the budget limit.', 2), user(question, 3)])
    assert out['max_budget'] is None


def test_aggregate_features_are_not_reconciled_from_a_partial_local_source():
    old = {'property_features': ['lift', 'step-free', 'cats allowed'],
           'constraint_sources': {'property_features': source(['lift'], 'I need a lift.')}}
    assert repair_criteria_sources(old, NoReplay()) is old


@pytest.mark.parametrize('nested_metadata', [False, True])
def test_opaque_clarification_replay_uses_only_its_preceding_stored_question(nested_metadata):
    choices = [{'value': 'reply_7f', 'label': 'Whole one-bedroom property'},
               {'value': 'reply_8f', 'label': 'Studio'}]
    question = {'role': 'assistant', 'content': 'Which property type?', 'response_type': 'clarification'}
    question['metadata' if nested_metadata else 'options'] = {'options': choices} if nested_metadata else choices
    old = {'room_type': 'studio', 'bedrooms': 0, 'constraint_sources': {
        'room_type': source('studio', DETAIL), 'bedrooms': source(0, DETAIL)}}
    out = repair_criteria_sources(old, [user('Help me choose a flat in Watford.'), question,
                                      user('reply_7f', 3), user(DETAIL, 4)])
    assert out['room_type'] == 'whole' and out['bedrooms'] == 1


def test_assistant_narrative_never_becomes_recovery_evidence():
    old = {'room_type': 'studio', 'constraint_sources': {'room_type': source('studio', DETAIL)}}
    out = repair_criteria_sources(old, [user(ORIGINAL),
        {'role': 'assistant', 'content': 'Your selected property type is now studio, not whole.'}, user(DETAIL, 3)])
    assert out['room_type'] == 'whole'


def test_incomplete_history_can_use_a_corrected_current_source_itself():
    text = '整套总租金预算£2400，每人£1200，两个人合租。只按整套预算找房。'
    old = {'max_budget': 1200, 'constraint_sources': {'max_budget': source(1200, text)}}
    out = repair_criteria_sources(old, [user('My rent budget is £1800 per month.')])
    assert out['max_budget'] == 2400


def test_no_reliable_history_clears_confirmed_bad_scalar_without_inventing_a_default():
    old = {'room_type': 'studio', 'constraint_sources': {'room_type': source('studio', DETAIL)}}
    out = repair_criteria_sources(old, [user(DETAIL)])
    assert out['room_type'] is None
    assert out['constraint_sources']['room_type']['source'] == 'criteria_recovery'
    assert repair_criteria_sources(out, NoReplay()) is out


def test_generic_office_without_history_becomes_unknown_not_a_guessed_address():
    out = repair_criteria_sources({'commute_destination': 'the office', 'destination': 'the office'}, [])
    assert out['commute_destination'] is None and out['destination'] is None
    assert repair_criteria_sources(out, NoReplay()) is out


def test_form_destination_protects_its_identical_legacy_mirror():
    old = {'commute_destination': 'My office', 'destination': 'My office',
           'constraint_sources': {'commute_destination': source('My office', 'form', 'form')}}
    assert repair_criteria_sources(old, NoReplay()) is old


def test_valid_destination_and_independent_multiarea_form_are_not_rewritten():
    weather = 'Weather in Camden today?'
    old = {'area': 'Camden', 'areas': ['Watford', 'High Barnet'], 'commute_destination': 'My private workplace',
           'constraint_sources': {'area': source('Camden', weather),
                                  'areas': source(['Watford', 'High Barnet'], 'form', 'form')}}
    out = repair_criteria_sources(old, [user(ORIGINAL), user(weather, 2)])
    assert out['areas'] == ['Watford', 'High Barnet']
    assert out['commute_destination'] == 'My private workplace'


def test_latest_actual_bedroom_parser_corrects_count_and_minimum_together():
    text = 'Change to at least two bedrooms, not four bedrooms.'
    old = {'bedrooms': 4, 'bedrooms_minimum': False,
           'constraint_sources': {'bedrooms': source(4, text), 'bedrooms_minimum': source(False, text)}}
    out = repair_criteria_sources(old, [user(text)])
    assert out['bedrooms'] == 2 and out['bedrooms_minimum'] is True
