"""Exercise actual authenticated GET and cold-session hydration, without an LLM."""
import copy

import app as runtime
from core.context_assembler import build_turn_snapshot
from core.candidate_validation import hard_constraints
from test_native_chat_transport import chat


def test_get_and_cold_session_recover_source_and_card_text_without_rewriting_history(chat, monkeypatch):
    _, _, store = chat
    owner = 'snapshot-owner'
    cid = store.create_conversation(owner)['id']
    message = ('Keep my current £2,000/month rent cap separate from the £2,150/month '
               'rent-plus-commute ceiling, based on two office days a week')
    message_id = store.add_message(owner, cid, 'user', message)['id']
    turn_id = store.begin_turn(owner, cid, request_id='read-recovery-test', user_message_id=message_id)['id']
    criteria = {'max_budget':2150,'max_budget_monthly':2000,'budget_period':'month',
                'monthly_total_budget':2150,'office_days_per_week':2,
                'destination':"King's Cross",'max_travel_time':45,
                'constraint_sources':{'max_budget':{'source':'user_message','value':2150,'text':message}}}
    row = {'address':'Angel Lane','travel_time':"37 min to King's Cross",
           'explanation':"2 bedroom flat (TfL transit: 37 min to King's Cross)",
           'qualification':{'status':'unknown','constraints':hard_constraints(criteria),
                            'unknown_reasons':['commute evidence is missing'],'reasons':[]}}
    snapshot = build_turn_snapshot(turn_id=turn_id, persistent_state={
        'accumulated_search_criteria':criteria,'extracted_context':{'view_results':[row]}},context_revision=9)
    original = copy.deepcopy(snapshot)
    store.save_turn_snapshot(owner,cid,turn_id,snapshot)
    assistant_id = store.add_message(owner,cid,'assistant','Earlier recorded reply')['id']
    store.complete_turn(owner,turn_id,assistant_message_id=assistant_id)
    monkeypatch.setattr(runtime,'_identity_from_request',lambda data=None:(owner,'test'))
    client = runtime.app.test_client()
    response = client.get(f'/api/conversations/{cid}/messages')
    assert response.status_code == 200
    view = response.json['view_state']
    assert view['criteria']['max_budget'] == view['criteria']['max_budget_monthly'] == 2000
    assert view['criteria']['monthly_total_budget'] == 2150
    assert 'not verified' in view['results'][0]['travel_time']
    assert 'TfL transit:' not in view['results'][0]['explanation']
    assert response.json['messages'][0]['content'] == message
    assert store.latest_snapshot(owner,cid) == original
    session = runtime._get_session(owner,cid)
    assert session.persistent_state['accumulated_search_criteria']['max_budget'] == 2000
    assert 'not verified' in session.last_results[0]['travel_time']
    monkeypatch.setattr(runtime,'_identity_from_request',lambda data=None:('another-owner','test'))
    assert client.get(f'/api/conversations/{cid}/messages').status_code == 404
