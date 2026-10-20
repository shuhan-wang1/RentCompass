"""Task-level regressions from the independent student/work/family audits."""
from dataclasses import replace

import pytest

from core.agent_loop import build_fc_nodes
from core.candidate_validation import validate_candidates, validate_commute_response
from core.listing_identity import bind_listing_links
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore
import app as runtime


class NoTools:
    def list_specs(self):
        return []

    def get(self, name):
        return None


def test_search_with_zero_results_still_replaces_panel():
    raw = {'success': True, 'status': 'no_results', 'recommendations': [],
           'message': 'No matching homes in the cached snapshot.',
           'search_criteria': {'area': 'Manchester', 'max_budget': 1500}}
    result = build_fc_nodes(NoTools())['format_output_fc']({
        'tool_artifacts': [{'tool': 'search_properties', 'raw_data': raw}],
        'user_preferences': {}, 'accumulated_search_criteria': raw['search_criteria'],
        'final_response': 'The market has no homes.', 'extracted_context': {'reply_language': 'en'},
    })
    assert result['response_type'] == 'search'
    assert result['tool_data']['recommendations'] == []
    assert result['tool_data']['search_criteria']['max_budget'] == 1500
    assert result['final_response'] == raw['message']


@pytest.mark.parametrize('success', [True, False])
def test_memory_plus_text_summary_does_not_require_a_second_tool(success):
    result = build_fc_nodes(NoTools())['format_output_fc']({
        'tool_artifacts': [{'tool': 'remember', 'success': success, 'raw_data': {'success': success}}],
        'memory_write_contract': {'requested': True, 'success': success},
        'extracted_context': {'current_message': '记住我的条件，并逐项复述和说明如何恢复。', 'reply_language': 'zh'},
        'final_response': '条件：整租，预算£1800。\n恢复方法：登录账号，打开此会话。',
        'user_preferences': {}, 'accumulated_search_criteria': {},
    })
    text = result['final_response']
    assert '预算£1800' in text and '恢复方法' in text
    assert ('已经记住' in text) == success
    assert ('保存失败' in text) != success


def test_arithmetic_survives_a_commute_topic_in_the_question():
    state = {'extracted_context': {'current_message': '通勤还没核实，先计算每月£1495比£1350贵多少。', 'reply_language': 'zh'}}
    answer = '£1495 − £1350 = £145/月，全年多£1740。这超过预算。'
    assert validate_commute_response(answer, state) == answer


def test_whole_property_and_minimum_bedrooms_use_actual_facts():
    rows = [
        {'address': 'Share', 'price': '£695/month', 'room_type': 'House share', 'bedrooms': 4},
        {'address': 'Small flat', 'price': '£1400/month', 'property_type': 'Flat', 'bedrooms': 2},
        {'address': 'Large flat', 'price': '£1500/month', 'property_type': 'Flat', 'bedrooms': 4},
    ]
    ledger = validate_candidates(rows, {'room_type': 'whole', 'bedrooms': 3, 'bedrooms_minimum': True, 'max_budget': 1500})
    assert [s['candidate']['address'] for s in ledger['eligible']] == ['Large flat']
    accessible = validate_candidates([rows[2]], {'room_type': 'whole', 'property_features': ['step-free', 'pet-friendly']})
    assert accessible['unknown'] and not accessible['eligible']


def test_budget_change_without_search_rechecks_cached_green_labels():
    from core.candidate_validation import hard_constraints, revalidate_view_results
    old = {'address': 'Cached flat', 'price': '£1820/month', 'bedrooms': 1, 'property_type': 'Flat',
           'qualification': {'status': 'eligible', 'constraints': hard_constraints({'max_budget': 2000})}, 'budget_basis': 2000}
    changed = revalidate_view_results([old], {'max_budget': 1500})[0]
    assert changed['qualification']['status'] == 'excluded'
    assert changed['budget_status_code'] == 'over' and changed['budget_basis'] == 1500
    assert old['qualification']['status'] == 'eligible' and old['budget_basis'] == 2000
    changed = revalidate_view_results([old], {'bedrooms': 3, 'bedrooms_minimum': True})[0]
    assert changed['qualification']['status'] == 'excluded'


def test_listing_link_uses_the_address_entity_not_a_generated_id():
    entities = [{'address': 'Beverley House, Watford', 'url': 'https://www.onthemarket.com/details/19909957/'}]
    text = '[Beverley House](https://www.onthemarket.com/details/19643971/) costs £1100.'
    repaired = bind_listing_links(text, entities)
    assert '19909957' in repaired and '19643971' not in repaired
    assert '£1100' in repaired
    assert 'onthemarket' not in bind_listing_links('[Unknown](https://www.onthemarket.com/details/12345678/)', entities)


def test_listing_rent_and_id_use_current_entity_without_rewriting_arithmetic():
    entities = [{'address': 'Beverley House, Watford', 'url': 'https://www.onthemarket.com/details/19909957/', 'price': '£1100/month'},
                {'address': 'Beverley House, Watford', 'url': 'https://www.onthemarket.com/details/19909957/', 'price': '£1200/month'}]
    text = 'Beverley House: £950/month; ID 19643971; budget £1800/month.\nDifference: £1495 − £1350 = £145.'
    answer = bind_listing_links(text, entities)
    assert '£1100/month' in answer and 'ID 19909957' in answer
    assert 'budget £1800/month' in answer and '£1495 − £1350 = £145' in answer


def test_listing_identity_preserves_bills_and_corrects_a_known_wrong_bare_url():
    entities = [{'address': 'Parrs Wood Road', 'url': 'https://www.onthemarket.com/details/19643970/', 'price': '£650/month'},
                {'address': 'Beverly Road', 'url': 'https://www.onthemarket.com/details/19909957/', 'price': '£1100/month'}]
    text = 'Parrs Wood Road: 月租£650/月，账单£120/月，总开支£770/月。'
    assert bind_listing_links(text, entities) == text
    for text in ('Parrs Wood Road：£120/月水电费；房租£650/月；合计£770/月。',
                 'Parrs Wood Road: £120/month bills; rent £650/month; total £770/month.'):
        assert bind_listing_links(text, entities) == text
    assert '19909957' in bind_listing_links('Beverly Road: https://www.onthemarket.com/details/19643970/', entities)


def test_named_property_question_does_not_refine_cached_results():
    from core.refine_results import plan_refinement
    rows = [{'address': 'Whole flat', 'property_type': 'Flat', 'room_type': 'whole', 'bedrooms': 1},
            {'address': 'Otter Close', 'property_type': 'House share', 'room_type': 'shared', 'bedrooms': 1}]
    query = "Is Otter Close a whole one-bedroom flat or a room in a shared flat? Please don't change my search criteria."
    assert plan_refinement(query, rows) is None


def test_empty_results_and_criteria_survive_replay_and_restart(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations.sqlite3')
    monkeypatch.setattr(runtime, 'conversation_store', store)
    monkeypatch.setattr(runtime, '_session_store', SessionStore())
    monkeypatch.setattr(runtime, '_runtime_config', replace(runtime._runtime_config, require_auth=False, allow_legacy_client_user_id=False))
    monkeypatch.setattr(runtime._api_rate_limiter, 'allow', lambda *a, **k: (True, 0))
    monkeypatch.setattr(runtime, '_ensure_outbox_worker', lambda: None)

    async def agent(message, context, continuation, user_id, conversation_id, request_id, ui_language='en', turn=None):
        results = [{'address': 'Old flat', 'price': '£1820/month', 'url': 'https://rental-fixture.invalid/old'}] if message == 'first' else ([] if message == 'empty' else None)
        criteria = {'area': 'Manchester', 'max_budget': 2000 if message == 'first' else 1500}
        runtime._write_back_turn(user_id, conversation_id, message, 'answer', results,
                                 accumulated_search_criteria=criteria, turn_id=turn['id'])
        payload = {'message': 'answer', 'response_type': 'search' if results is not None else 'chat'}
        if results is not None:
            payload['recommendations'] = results
        return payload

    monkeypatch.setattr(runtime, 'handle_with_react_agent', agent)
    client = runtime.app.test_client()
    first = client.post('/api/alex', json={'message': 'first'}, headers={'X-Request-Id': 'first'}).json
    cid = first['conversation_id']
    assert len(first['view_state']['results']) == 1
    empty = client.post('/api/alex', json={'message': 'empty', 'conversation_id': cid}, headers={'X-Request-Id': 'empty'}).json
    assert empty['view_state']['results'] == []
    assert empty['view_state']['revision'] == 2 and empty['view_state']['criteria']['max_budget'] == 1500
    replay = client.post('/api/alex', json={'message': 'empty', 'conversation_id': cid}, headers={'X-Request-Id': 'empty'}).json
    assert replay['view_state'] == empty['view_state']
    monkeypatch.setattr(runtime, '_session_store', SessionStore())
    consulted = client.post('/api/alex', json={'message': 'consult', 'conversation_id': cid}).json
    assert consulted['view_state']['results'] == [] and consulted['view_state']['revision'] == 3
    loaded = client.get('/api/conversations/' + cid + '/messages').json
    assert loaded['view_state'] == consulted['view_state']
    store.close()
