from pathlib import Path
import json,sqlite3
B=Path('/tmp/rentcompass-fix-live-20261019')
profiles={'student':['bd64dd5d9b12410586b93a31b812a6f0','7fc8c690d7bf4e54ab7cf39d8967764a'],'professional':['817bd0754098483997d66252c68b338e'],'family':['76e9351df6d1450582bf1576b6e2116f']}
rows=[json.loads(x)for x in (B/'evidence/tool-dispatches.jsonl').read_text().splitlines()]
with sqlite3.connect('file:'+str(B/'runtime/conversations.sqlite3')+'?mode=ro',uri=True) as db:
 for persona,cids in profiles.items():
  turns=[dict(zip(('turn_id','conversation_id','request_id','status'),r)) for r in db.execute('select id,conversation_id,request_id,status from turns where conversation_id in ('+','.join('?'*len(cids))+') order by started_at',cids)]
  ids={r['request_id']for r in turns};calls=[r for r in rows if r.get('request_id')in ids]
  (B/persona/'own-tool-ledger.json').write_text(json.dumps({'boundary':'Only actual Tool.execute records for this persona authenticated conversation(s). Terminal ask_user is graph-managed and is not recorded here; no absence implies no clarification.','turns':turns,'actual_dispatches':calls},ensure_ascii=False,indent=2))
  print(persona,len(turns),len(calls))
