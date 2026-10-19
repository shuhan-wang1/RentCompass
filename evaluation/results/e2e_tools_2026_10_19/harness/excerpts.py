import datetime,json,pathlib
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019')
ds=[json.loads(s) for s in (B/'evidence/tool-dispatches.jsonl').read_text().splitlines()];ledger=[]
for line in (B/'private/server.log').read_text().splitlines():
 try:
  outer=json.loads(line);r=json.loads(outer.get('message',''))
  if r.get('event')=='canary.turn':
   r['_epoch']=datetime.datetime.fromisoformat(r['ts'].replace('Z','+00:00')).timestamp();ledger.append(r)
 except Exception:pass
for persona in ['student','professional','family','root']:
 p=B/persona/(persona+'-turns.jsonl')
 if not p.exists():continue
 turns=[json.loads(s) for s in p.read_text().splitlines()];rows=[]
 journal_path=B/persona/'request-budget.jsonl';journal=[json.loads(s) for s in journal_path.read_text().splitlines()] if journal_path.exists() else []
 for i,t in enumerate(turns):
  end=t['time'];start=end-t['seconds']-1;result=t.get('result') or {};payload=result.get('payload') or result
  cid=payload.get('conversation_id');matched=[x for x in ledger if start<=x['_epoch']<=end+1 and cid and x.get('conversation_id')==cid]
  request_ids={x['request_id'] for x in matched};candidates=[j for j in journal if j.get('start') and not j.get('no_request') and abs(j['start']-(end-t['seconds']))<3]
  turn=min(candidates,key=lambda j:abs(j['start']-(end-t['seconds'])))['turn'] if candidates else i+1
  rows.append({'turn':turn,'message':t['message'],'tool_dispatches':[x for x in ds if start<=x['started']<=end+1 and (not x.get('request_id') or x['request_id'] in request_ids)],'completed_turn_ledger':matched})
 (B/persona/'dispatch-excerpt.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
 print(persona,len(turns),'turns',sum(bool(x['completed_turn_ledger']) for x in rows),'ledgers')
