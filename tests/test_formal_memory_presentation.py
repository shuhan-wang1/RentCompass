"""A saved preference is user text, not a measured provider claim."""
from core.memory_contract import (memory_contract_from_artifact, compose_memory_contract_response,
    saved_content_quote, strip_saved_content_quote)
from core.candidate_validation import validate_commute_response


def artifact(text, **flags):
    return dict(tool='remember', success=True, raw_data={'success':True,'stored':text}, **flags)


def test_successful_memory_only_reply_quotes_exact_stored_text():
    text='请记住并保存默认：Camden，正好2间卧室，最高£2400，UCL30分钟以内。只保存，不要搜索。'
    contract=memory_contract_from_artifact(artifact(text))
    response=compose_memory_contract_response('预算冲突，请再选择。',contract,language='zh')
    assert text in response and '预算冲突' not in response
    assert '已保存的原文' in response


def test_user_quote_survives_commute_validation_without_licensing_travel_claims():
    text='Save my preference: a whole two-bedroom flat, £2400, at most 30 minutes to UCL.'
    contract=memory_contract_from_artifact(artifact(text))
    combined=compose_memory_contract_response('A particular flat takes 12 minutes.',contract,
        language='en',preserve_content=True)
    unquoted=strip_saved_content_quote(combined,contract,'en')
    assert text not in unquoted
    state={'user_query':'Save these preferences.', 'extracted_context':{'reply_language':'en'},
           'tool_artifacts':[artifact(text)],'accumulated_search_criteria':{}}
    verified=validate_commute_response(unquoted,state)
    final=compose_memory_contract_response(verified,contract,language='en',preserve_content=True)
    assert text in final
    assert 'takes 12 minutes' not in final
    assert final.count(saved_content_quote(contract,'en'))==1


def test_failed_or_timed_out_write_cannot_echo_text_as_saved():
    for row in [dict(tool='remember',success=False,raw_data={'stored':'private text'}),
                artifact('private text',timed_out=True)]:
        contract=memory_contract_from_artifact(row)
        assert 'stored_content' not in contract
        assert 'private text' not in compose_memory_contract_response('Saved private text.',contract)


def test_acknowledged_memory_only_request_stops_before_another_model_call():
    from core.agent_loop import build_fc_nodes
    from test_fc_loop import FakeChat, FakeProvider, FakeSpec, _base_state, _run
    text='请记住我的长期默认：整套正好2间卧室，预算£2400。只保存，不要搜索。'
    nodes=build_fc_nodes(FakeProvider([FakeSpec('remember',side_effect='write')]),agent_llm=FakeChat([]))
    state=_base_state(user_query=text,extracted_context={'current_message':text,'reply_language':'zh'},
                      tool_artifacts=[artifact(text)])
    command=_run(nodes['agent'](state))
    assert command.goto=='format_output_fc'
    state.update(command.update or {})
    result=nodes['format_output_fc'](state)
    assert text in result['final_response']
    assert '预算冲突' not in result['final_response']
