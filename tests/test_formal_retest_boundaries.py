"""Regressions from real persona use: form, details routing and evidence scope."""
import copy

import pytest

from core.candidate_validation import (
    _clean_commute_fragments, hard_constraints, validate_candidates,
    validate_search_payload,
)
from core.criteria_contract import apply_form_criteria
from core.refine_results import parse_refinement


def test_form_replaces_exposed_aliases_and_keeps_unexposed_hard_requirements():
    old = {'max_budget': 1850, 'max_budget_monthly': 1850,
           'max_commute_time': 45, 'max_travel_time': 45,
           'monthly_total_budget': 2150, 'office_days_per_week': 3,
           'max_changes': 1, 'property_features': ['cats allowed'],
           'constraint_sources': {'property_features': {'source': 'user_message'}}}
    before = copy.deepcopy(old)
    form = {'max_budget': 1900.50, 'max_travel_time': 30, 'area': 'Lewisham',
            'areas': ['Lewisham'], 'commute_destination': 'Bank'}
    criteria = apply_form_criteria(old, form)
    normalized = hard_constraints(criteria)
    assert normalized['max_budget_monthly'] == 1900.50
    assert normalized['max_commute_minutes'] == 30
    assert normalized['monthly_total_budget'] == 2150
    assert normalized['office_days_per_week'] == 3
    assert normalized['max_changes'] == 1
    assert normalized['property_features'] == ['cats allowed']
    assert criteria['constraint_sources']['max_budget']['source'] == 'form'
    result = validate_search_payload({'recommendations': [{
        'address': 'Example Road', 'price': '£1800 pcm', 'area': 'Lewisham',
        'verified_features': [], 'unverified_features': [], 'absent_features': [],
    }], 'search_criteria': criteria})
    assert not result['recommendations']
    assert result['unverified_candidates']
    assert old == before


def test_form_explicit_clear_clears_legacy_budget_and_commute_aliases():
    criteria = apply_form_criteria({'max_budget_monthly': 1800, 'max_commute_time': 45},
                                   {'max_budget': None, 'max_travel_time': None})
    normalized = hard_constraints(criteria)
    assert normalized['max_budget_monthly'] is None
    assert normalized['max_commute_minutes'] is None


@pytest.mark.parametrize('absent,expected', [([], 'unknown'), (['cats allowed'], 'excluded')])
def test_new_requirement_on_three_way_listing_requires_explicit_denial(absent, expected):
    candidate = {'address': 'Example Road', 'price': '£1800 pcm',
                 'verified_features': [], 'unverified_features': [], 'absent_features': absent}
    result = validate_candidates([candidate], {'property_features': ['cats allowed']})
    assert result['statuses'][0]['status'] == expected


def test_legacy_binary_feature_contract_still_excludes_missing_feature():
    result = validate_candidates([{'verified_features': []}], {'property_features': ['lift']})
    assert result['statuses'][0]['status'] == 'excluded'


def test_family_comparison_does_not_become_cached_area_filter():
    message = ("Let's look at Tennyson Road, Stratford first, and compare it with Borthwick Road. "
        "For our family the step-free route in, enough space for my 68 cm wheelchair through "
        "the doors and bathroom, and permission for our cat are requirements, not preferences. "
        "What do the actual details for these two homes confirm, and what is still unknown? "
        "Does either have a written cat permission statement or measurements for wheelchair access? "
        "A lift on its own would not answer the space question. Please keep our other search limits.")
    rows = [{'address': 'Tennyson Road', 'area': 'Stratford'},
            {'address': 'Borthwick Road', 'area': 'Leyton'}]
    assert parse_refinement(message, rows) is None
    pure_filter = parse_refinement('Only keep the Stratford ones.', rows)
    assert pure_filter and pure_filter['filters'] == [{'kind': 'area', 'value': 'Stratford'}]


@pytest.mark.parametrize('fragment', [
    'Total journey time: which is within your 45-minute limit.',
    '通勤时间：由 TfL Journey Planner 提供。',
])
def test_removed_duration_does_not_leave_an_empty_positive_label(fragment):
    cleaned, changed = _clean_commute_fragments(fragment + '\nActual availability remains unverified.')
    assert changed and fragment not in cleaned
    assert 'Actual availability remains unverified.' in cleaned


def test_duration_unavailable_notice_is_preserved():
    text = 'Commute time: unavailable because the route provider timed out.'
    assert _clean_commute_fragments(text) == (text, False)


def test_proven_lift_repair_removes_only_its_exact_display_labels():
    from core.criteria_recovery import preferences_after_criteria_repair
    preferences = {'required_amenities':['lift','step-free'],
                   'hard_preferences':['Must have lift','Must have step-free'],
                   'soft_preferences':['lift would be useful']}
    assert preferences_after_criteria_repair(preferences, {}) is preferences
    repaired = preferences_after_criteria_repair(preferences, {'property_features':['step-free'],
        'constraint_sources':{'property_features':{
            'reason':'lift_insufficiency_statement_is_not_requirement'}}})
    assert repaired['required_amenities'] == ['step-free']
    assert repaired['hard_preferences'] == ['Must have step-free']
    assert repaired['soft_preferences'] == preferences['soft_preferences']
    assert preferences['required_amenities'] == ['lift','step-free']


def test_separate_is_not_the_annual_budget_abbreviation_pa():
    from core.context_assembler import detect_history_conflicts
    first = ("I'm moving to London for a job near Bank station. I'll be in the office three days a week. "
        "Please help me find a self-contained one-bedroom flat in Lewisham or Stratford, "
        "with rent up to £1,850 per month and rent plus commuting no more than £2,150 per month. "
        "I'd like a public-transport commute of about 45 minutes or less. What suitable places can you show me?")
    second = ("Please keep the £1,850 rent limit separate from the £2,150 rent-and-commute limit, "
              "and tell me what you could actually verify.")
    assert detect_history_conflicts([{'user': first}, {'user': second}], first) == []
    assert detect_history_conflicts([{'user': 'My rent budget is £1850 pa.'}],
                                   'My rent budget is £1850 per month.')


def test_partial_weather_failure_cannot_deny_a_successful_city_forecast():
    from core.candidate_validation import render_structured_evidence_fallback
    state = {'tool_artifacts':[
        {'tool':'get_weather','success':False,'raw_data':{'success':False,'error':'unresolved area'}},
        {'tool':'get_weather','success':True,'raw_data':{'success':True,'location':'London',
         'weather_type':'forecast','temperature':8.4,'condition':'Overcast',
         'valid_at':'2026-10-24T11:00:00+01:00'}}]}
    answer = render_structured_evidence_fallback(state)
    assert 'Some requested weather forecast lookups failed' in answer
    assert 'Weather forecast for London' in answer and '8.4°C' in answer
    assert 'Weather forecast could not be verified this turn' not in answer


def test_family_mixed_neighbourhood_request_must_reach_all_requested_tools():
    message = ("We are considering viewing Tennyson Road, Stratford — "
        "https://www.onthemarket.com/details/20489843/ — tomorrow afternoon. "
        "Keeping the housing search unchanged, please find primary schools and GP surgeries within 1 km "
        "for our seven-year-old and explain recorded crime for this home's actual location, "
        "including reference point, radius and data months. Nearby does not guarantee admission or GP registration. "
        "Also check the weather for the viewing and the public-transport trip from this home to King's Cross. "
        "Please be clear if that route's step-free access is not actually verified for my wheelchair.")
    rows = [{'address':'Tennyson Road','area':'Stratford'}, {'address':'Borthwick Road','area':'Leyton'}]
    assert parse_refinement(message, rows) is None
    assert parse_refinement("Don't check weather. Only keep the Stratford ones.", rows)


def test_precise_requirement_labels_do_not_change_machine_evidence():
    from core.candidate_validation import candidate_reason_text, render_candidate_status
    reason = 'required features are not verified: wheelchair_space_68cm, cat_written_consent'
    en = candidate_reason_text(reason)
    zh = candidate_reason_text(reason, language='zh')
    assert '68 cm wheelchair' in en and 'written permission for the cat' in en
    assert '68cm轮椅' in zh and '书面' in zh
    ledger = validate_candidates([{'verified_features':[], 'absent_features':[], 'unverified_features':[]}],
                                 {'property_features':['wheelchair_space_68cm','cat_written_consent']})
    assert ledger['unknown'][0]['unknown_reasons'] == [reason]
    assert 'wheelchair_space_68cm' not in render_candidate_status(ledger)


def test_commute_redaction_removes_empty_table_row_and_unsupported_inside_claim():
    from core.candidate_validation import validate_commute_response
    message = ('| Price | £2000 | £2100 |\n| Commute |\n'
        "Your other search limits (budget, two bedrooms, King's Cross commute) are unchanged "
        'and both properties still fall inside them.')
    out = validate_commute_response(message, {'tool_artifacts':[],
        'accumulated_search_criteria':{'max_travel_time':45,'destination':"King's Cross"}})
    assert '| Commute |' not in out
    assert 'both properties still fall inside' not in out
    assert '| Price | £2000 | £2100 |' in out


def test_current_weather_caveat_does_not_deny_a_separate_dated_forecast():
    from core.candidate_validation import render_structured_evidence_fallback
    state = {'tool_artifacts':[
        {'tool':'get_weather','success':True,'raw_data':{'success':True,'location':'Stratford',
         'current_temp':14,'condition':'Cloudy','weather_type':'current'}},
        {'tool':'get_weather','success':True,'raw_data':{'success':True,'location':'Stratford London',
         'weather_type':'forecast','temperature':13.1,'condition':'Rain',
         'valid_at':'2026-10-20T14:00:00+01:00'}}]}
    out = render_structured_evidence_fallback(state)
    assert 'Current weather returned for Stratford' in out
    assert 'Weather forecast for Stratford London' in out
    assert 'no multi-day forecast is verified' not in out


def test_withdrawn_route_proof_withdraws_old_verified_card_text_on_refresh():
    from core.candidate_validation import revalidate_view_results
    criteria = {'destination':"King's Cross",'max_travel_time':45}
    row = {'address':'Angel Lane','travel_time':"37 min to King's Cross",
           'explanation':"2 bedroom flat (TfL transit: 37 min to King's Cross)",
           'qualification':{'status':'unknown','constraints':hard_constraints(criteria),
                            'reasons':[],'unknown_reasons':['commute evidence is missing']}}
    refreshed = revalidate_view_results([row], criteria)[0]
    assert 'not verified' in refreshed['travel_time']
    assert 'TfL transit:' not in refreshed['explanation']
    assert refreshed['qualification']['status'] == 'unknown'
    assert row['travel_time'] == "37 min to King's Cross"


def test_failed_poi_notice_explains_cooldown_and_smaller_radius():
    from core.candidate_validation import render_nearby_failure_notice
    state = {'tool_artifacts':[{'tool':'search_nearby_pois','success':False,'raw_data':{
        'success':False,'service_status':'unavailable','failed_types':['school','gp'],
        'radius_m':800,'user_requested_radius_m':1000,
        'category_status':{'school':{'service_status':'cooldown'}}}}]}
    out = render_nearby_failure_notice(state)
    assert 'school, gp' in out and 'cooling down' in out
    assert '1000 m' in out and '800 m' in out and 'not covered' in out
    assert '不能据此断言' in render_nearby_failure_notice(state, language='zh')


def test_literal_this_home_journey_origin_resolves_to_same_listing_only(monkeypatch):
    from core import property_scope
    monkeypatch.setattr(property_scope, '_cached_records', lambda: [])
    url = 'https://www.onthemarket.com/details/20489843/'
    context = {'last_results':[{'url':url,'address':'Tennyson Road, Stratford',
                               'geo_location':'51.54103, 0.006745'}]}
    args = {'query_type':'journey','from_location':'this home','to_location':"King's Cross",
            'date':'2026-10-20','time':'14:00'}
    result = property_scope.grounded_property_read_args('get_transport_info', args,
        f'Check the trip from this home {url} to Kings Cross tomorrow.', context)
    assert result == dict(args, from_location='Tennyson Road, Stratford')
    station = dict(args, from_location='Stratford station, London')
    assert property_scope.grounded_property_read_args('get_transport_info', station,
        f'Check the separate station journey near {url}.', context) == station
    assert property_scope.grounded_property_read_args('get_transport_info', args,
        'Check the trip from this home https://fixtures.invalid/unknown.', context) == args
