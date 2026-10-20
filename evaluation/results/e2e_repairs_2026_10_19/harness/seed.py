"""Seed only new synthetic conversations. Never reads production paths or credentials."""
import sys,json,sqlite3
from uk_rent_agent.web.conversation_store import ConversationStore
path='/audit/runtime/conversations.sqlite3'
spec=json.load(sys.stdin)
store=ConversationStore(path);out={}
for label,item in spec.items():
    with sqlite3.connect(path) as c:row=c.execute('SELECT user_id,title FROM conversations WHERE id=?',(item['cid'],)).fetchone()
    assert row and row[1].startswith('SYNTHETIC_AUTHZ_'), 'Only newly created synthetic conversations may be seeded'
    uid=row[0];turns=[]
    for number in (1,2):
        key=f'authz-shared-replay-{number}'
        turn=store.start_request_turn(uid,item['cid'],key,item['marker']+f' USER {number}',lease_seconds=30)
        store.finalize_request_turn(uid,turn['id'],status='completed',assistant_content=item['marker']+f' ASSISTANT {number}',response_type='chat',recommendations=[],snapshot={'schema_version':1,'turn_id':turn['id']})
        turns.append(turn['id'])
    out[label]={'uid':uid,'turns':turns}
print(json.dumps(out))
store.close()
