from uk_rent_agent.web.conversation_store import ConversationStore
from core.tools.ask_user import normalize_options


def test_clickable_choices_survive_reopen_and_request_replay(tmp_path):
    path = tmp_path / 'conversations.sqlite3'
    store = ConversationStore(path)
    cid = store.create_conversation('user-a', 'Choices')['id']
    turn = store.start_request_turn('user-a', cid, 'choose-request', 'Help me decide')
    options = [{'label': '整套', 'value': '我要整套住宅'}, {'label': '合租', 'value': '我要合租房间'}]
    store.finalize_request_turn('user-a', turn['id'], status='completed',
        assistant_content='你想住哪一种？', response_type='clarification',
        response_metadata={'options': options, 'clarification_kind': 'soft_criteria'},
        snapshot={'turn_id': turn['id'], 'context_revision': 1})
    store.close()
    reopened = ConversationStore(path)
    assert reopened.get_messages('user-a', cid)[-1]['options'] == options
    assert reopened.get_turn_response('user-a', turn['id'])['options'] == options
    assert reopened.get_messages('user-b', cid) == []
    assert reopened.get_turn_response('user-b', turn['id']) is None
    reopened.close()


def test_options_have_explicit_bounded_reply_values():
    assert normalize_options([{'label': 'A', 'value': 'whole'}, {'label': 'B'}, 'C', {'label': '', 'value': 'x'}]) == [
        {'label': 'A', 'value': 'whole'}]
