"""Failures observed in formal use: upstream refusal and independent mixed tasks."""
import pytest
from langchain_core.messages import AIMessage
from core.source_status import source_failure_summary
from core.agent_loop import build_fc_nodes, _explicit_transport_reads
from core.candidate_validation import render_structured_evidence_fallback, validate_candidates
from core.context_assembler import build_turn_snapshot, snapshot_to_session_patch
from uk_rent_agent.web.view_state import view_state
from test_fc_loop import FakeChat, FakeProvider, FakeResult, FakeSpec, _tc, _run as drive_run, _drive, _base_state
from test_search_optional_criteria import stub_env, _install_listings, _run


def test_403_is_source_failure_through_search_and_final_formatter(stub_env, monkeypatch):
    _install_listings(monkeypatch, [], {'source_status': 'blocked', 'retrieval_complete': False,
        'source_error': {'status_code': 403, 'reason': 'http_access_denied', 'retry_after_seconds': 3600}})
    result = _run(area='Camden', max_budget=1900, no_commute=True, confirmed=True)
    assert result['source_status'] == 'blocked'
    assert result['area_status']['Camden'] == 'blocked'
    assert result['partial'] is True and result['retrieval_complete'] is False
    assert '403' in result['message']
    assert 'adjust the budget' not in result['message']
    provider = FakeProvider([FakeSpec('search_properties')], {'search_properties': FakeResult(True, result)})
    chat = FakeChat([AIMessage(content='', tool_calls=[_tc('search_properties', {'area':'Camden'}, 's')]),
                     AIMessage(content='No homes at that budget. Try increasing it.')])
    final = drive_run(_drive(build_fc_nodes(provider, agent_llm=chat), _base_state()))
    assert final['tool_data']['source_status'] == 'blocked'
    assert '403' in final['final_response']
    assert 'Try increasing' not in final['final_response']


def test_successful_empty_sample_is_distinct_from_refusal(stub_env, monkeypatch):
    _install_listings(monkeypatch, [], {'source_status':'ok', 'retrieval_complete':True})
    result = _run(area='Camden', no_commute=True, confirmed=True)
    assert result.get('source_status') not in {'blocked','unavailable'}
    assert result['area_status']['Camden'] == 'empty'
    assert result['partial'] is False


def test_safe_failure_metadata_omits_unrelated_upstream_fields():
    result = source_failure_summary({'Camden': {'meta': {'source_status':'rate_limited',
        'source_error': {'status_code':429, 'reason':'http_rate_limited', 'body':'private-body',
                         'Authorization':'private-token', 'retry_after_seconds':45}}}})
    assert result['source_errors']['Camden'] == {'status':'rate_limited','status_code':429,
        'reason':'http_rate_limited','retry_after_seconds':45}


def test_source_panel_state_survives_snapshot_and_session_restore():
    retrieval = {'source_status':'blocked','source_message':'Source access restricted.',
                 'retrieval_complete':False, 'partial':True}
    snapshot = build_turn_snapshot(turn_id='formal-source', persistent_state={
        'accumulated_search_criteria':{'area':'Camden','max_budget':1900},
        'extracted_context':{'view_results':[], 'search_retrieval':retrieval}}, context_revision=2)
    patch = snapshot_to_session_patch(snapshot)
    assert patch['search_retrieval'] == retrieval
    state = view_state(snapshot)
    assert state['retrieval'] == retrieval
    state['retrieval']['source_status'] = 'ok'
    assert view_state(snapshot)['retrieval']['source_status'] == 'blocked'


def test_budget_clarification_does_not_block_explicit_transport_and_forecast():
    message = ('Keep Lewisham and Bank. My rent allowance is unclear. Check the actual TfL route '
               'from Lewisham station to Bank arriving by 8:45 am on Tuesday 20 October 2026 '
               'and the current peak single fare. Check Lewisham weather for 2026-10-20.')
    provider = FakeProvider([FakeSpec('ask_user', terminal=True), FakeSpec('search_properties'),
                            FakeSpec('get_transport_info'), FakeSpec('get_weather')], {
        'get_transport_info': FakeResult(True, {'success':True, 'query_type':'fare', 'fare_available':True,
                                               'fare_gbp':3.2, 'from':{'input':'Lewisham'}, 'to':{'input':'Bank'}}),
        'get_weather': FakeResult(True, {'success':True, 'weather_type':'forecast', 'temperature':12.0,
            'valid_at':'2026-10-20T09:00', 'location':'Lewisham', 'condition':'Cloudy','assumed_time':True})})
    chat = FakeChat([AIMessage(content='', tool_calls=[_tc('ask_user', {'question':'Which rent ceiling should I use?',
         'clarification_kind':'budget_conflict', 'missing_fields':['max_budget']}, 'a')])])
    final = drive_run(_drive(build_fc_nodes(provider, agent_llm=chat), _base_state(
        user_query=message, extracted_context={'current_message':message,'reply_language':'en'},
        accumulated_search_criteria={'area':'Lewisham','destination':'Bank'})))
    assert sorted(name for name, _ in provider.calls) == ['get_transport_info','get_weather']
    transport = next(args for name,args in provider.calls if name=='get_transport_info')
    assert transport['to_location'] == 'Bank'
    assert {key:transport[key] for key in ('date','time','time_is')} == {
        'date':'2026-10-20','time':'08:45','time_is':'arrival'}
    weather = next(args for name,args in provider.calls if name=='get_weather')
    assert weather['target_date'] == '2026-10-20'
    assert final['response_type'] == 'clarification'
    assert 'Which rent ceiling' in final['final_response']
    assert '£3.20' in final['final_response'] and 'forecast' in final['final_response']
    assert 'Current weather' not in final['final_response']


def test_timed_route_without_fare_request_gets_one_journey_read():
    calls = _explicit_transport_reads({}, [], 'Check the actual route from Lewisham station to Bank arriving by 08:45 on 2026-10-20.')
    assert len(calls) == 1
    assert calls[0][1]['query_type'] == 'journey' and calls[0][1]['to_location'] == 'Bank'


@pytest.mark.parametrize('count, expected', [(None,'unknown'),(1,'eligible'),(2,'excluded')])
def test_transfer_constraint_requires_candidate_bound_measured_evidence(count, expected):
    candidate = {'address':'1 Example Road','price':'£1800 pcm','url':'https://fixtures.invalid/1'}
    raw = {'success':True,'from_address':'1 Example Road','to_address':'Bank',
           'duration_minutes':25,'source':'TfL Journey Planner','changes_count':count}
    evidence = [{'candidate_key':'url:https://fixtures.invalid/1','success':True,'raw_data':raw}]
    result = validate_candidates([candidate], {'max_changes':1,'destination':'Bank'}, evidence)
    assert result['statuses'][0]['status'] == expected


def test_transfer_evidence_survives_unrelated_rent_change():
    from core.candidate_validation import revalidate_view_results
    candidate = {'address':'1 Example Road','price':'£1800 pcm','url':'https://fixtures.invalid/1'}
    evidence = [{'candidate_key':'url:https://fixtures.invalid/1', 'success':True,
        'from_address':'1 Example Road','to_address':'Bank','raw_data':{
        'success':True,'from_address':'1 Example Road','to_address':'Bank',
        'duration_minutes':25,'source':'TfL Journey Planner','changes_count':1}}]
    criteria={'max_budget':1900,'destination':'Bank','max_commute_time':35,'max_changes':1}
    ledger=validate_candidates([candidate],criteria,evidence)
    assert ledger['statuses'][0]['status']=='eligible'
    row=ledger['statuses'][0]['candidate']
    assert row['verified_commute_evidence']['raw_data']['changes_count']==1
    refreshed=revalidate_view_results([row],dict(criteria,max_budget=2000))
    assert refreshed[0]['candidate_status']=='eligible'


def test_empty_state_defaults_cannot_remove_constraints_the_search_enforced():
    from core.criteria_contract import reconcile_search_criteria
    acc={'area':'Camden','destination':None,'max_travel_time':45,'max_budget':2166.67}
    searched={'area':'Camden','commute_destination':'UCL','max_travel_time':45,
              'max_budget':2166.67,'room_type':'whole','bedrooms':1}
    canonical=reconcile_search_criteria(acc,searched)
    assert canonical['destination']==canonical['commute_destination']=='UCL'
    assert canonical['room_type']=='whole' and canonical['bedrooms']==1
    candidate={'address':'1 Camden Rd','area':'Camden','price':'£2100 pcm',
               'room_type':'whole','bedrooms':1}
    assert validate_candidates([candidate],canonical)['statuses'][0]['status']=='unknown'


def test_explicit_clear_and_old_area_destination_alias_are_not_reintroduced():
    from core.criteria_contract import reconcile_search_criteria
    cleared={'area':'Camden','commute_destination':None,'destination':None,
             'constraint_sources':{'commute_destination':{'source':'user_message','value':None}}}
    assert reconcile_search_criteria(cleared,{'destination':'Camden','commute_destination':'UCL'})['destination'] is None
    assert reconcile_search_criteria({}, {'area':'Camden','destination':'Camden'})['destination'] is None


def test_saved_false_green_with_missing_destination_is_withdrawn():
    from core.candidate_validation import revalidate_saved_favorite, hard_constraints
    row={'address':'Apt108','price':'£2100 pcm','travel_time':'~38 min (estimated, unverified)',
         'qualification':{'status':'eligible','constraints':hard_constraints({
             'max_budget':2166.67,'max_travel_time':45,'destination':None}), 'unknown_reasons':[]}}
    repaired=revalidate_saved_favorite(row)
    assert repaired['qualification']['status']=='unknown'
    assert 'commute destination is not verified' in repaired['qualification']['unknown_reasons']
    assert repaired['price']==row['price']
