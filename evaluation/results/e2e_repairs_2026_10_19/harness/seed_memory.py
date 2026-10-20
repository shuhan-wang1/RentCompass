import json,secrets,pathlib
B=pathlib.Path('/audit');s=json.loads((B/'private/clients.json').read_text());value=secrets.token_hex(20);content='BLUE_LANTERN_PET_POLICY synthetic B private value: '+value
from rag.agent_memory import get_agent_memory
mid=get_agent_memory().add(content,'semantic',session_id='scope-probe',user_id=s['actors']['B']['uid'])
assert mid
(B/'private/memory-scope.json').write_text(json.dumps({'owner':s['actors']['B']['uid'],'caller':s['actors']['A']['uid'],'private_value':value,'memory_id':mid}))
print('Synthetic B memory fixture seeded')
