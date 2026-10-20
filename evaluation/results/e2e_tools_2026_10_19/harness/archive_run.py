import collections,datetime,hashlib,json,pathlib,shutil,statistics
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019');DEST=pathlib.Path('/home/shuhan/uk_rent_recommendation/evaluation/results/e2e_tools_2026_10_19');DEST.mkdir(parents=True,exist_ok=True)
def read(p):return json.loads(p.read_text())
def lines(p):return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
private_keys={'password','cookie','set-cookie','authorization','auth_sid','csrf_token'}
def redact(v):
 if isinstance(v,dict):return {k:('[REDACTED]' if k.lower() in private_keys else redact(x)) for k,x in v.items()}
 if isinstance(v,list):return [redact(x) for x in v]
 return v
for folder in ['evidence','student','professional','family','root','harness','public']:
 for p in (B/folder).rglob('*'):
  if not p.is_file() or '__pycache__' in p.parts or 'storage' in p.name.lower() or p.suffix=='.pyc':continue
  if p.suffix not in ['.json','.jsonl','.png','.py','.txt']:continue
  target=DEST/p.relative_to(B);target.parent.mkdir(parents=True,exist_ok=True)
  if p.suffix=='.json':write(target,redact(read(p)))
  elif p.suffix=='.jsonl':target.write_text(''.join(json.dumps(redact(x),ensure_ascii=False)+'\n' for x in lines(p)))
  else:shutil.copy2(p,target)
shutil.copy2(B/'setup.py',DEST/'harness/setup.py')
models=lines(B/'evidence/model-calls.jsonl');dispatches=lines(B/'evidence/tool-dispatches.jsonl');schema=read(B/'evidence/tool-schemas.json');coverage=[]
personas={};all_ledgers=[]
for persona in ['student','professional','family']:
 ts=lines(B/persona/(persona+'-turns.jsonl'));ex=read(B/persona/'dispatch-excerpt.json');ledger=[l for t in ex for l in t['completed_turn_ledger']];all_ledgers+=ledger
 statuses=collections.Counter(str((t.get('result') or {}).get('status')) for t in ts)
 personas[persona]={'api_alex_requests':len(ts),'status_counts':dict(statuses),'completed_chat_ledgers':len(ledger),'chat_turns_with_model':sum((l.get('llm_calls') or 0)>0 for l in ledger),'form_search_direct_requests':1 if persona=='professional' else 0,'user_actions':len(ts)+(1 if persona=='professional' else 0),'chat_latency_median_seconds':round(statistics.median(t['seconds'] for t in ts),3),'actual_tool_dispatches':sum(len(t['tool_dispatches']) for t in ex),'terminal_ask_user_calls':sum(l.get('tool_latency',{}).get('ask_user',{}).get('count',0) for l in ledger)}
for tool in schema:
 calls=[r for r in dispatches if r['tool']==tool];terminal=sum(l.get('tool_latency',{}).get('ask_user',{}).get('count',0) for l in all_ledgers) if tool=='ask_user' else 0
 refs={}
 for persona in ['student','professional','family','root']:
  p=B/persona/'dispatch-excerpt.json'
  if p.exists():
   matches=[]
   for t in read(p):
    if any(d['tool']==tool for d in t['tool_dispatches']) or (tool=='ask_user' and any(l.get('tool_latency',{}).get('ask_user') for l in t['completed_turn_ledger'])):matches.append(t['turn'])
   if matches:refs[persona]=matches
 coverage.append({'tool':tool,'e2e_execute_attempts':len(calls),'e2e_logical_successes':sum(r.get('result',{}).get('success') is True for r in calls),'e2e_logical_failures':sum(r.get('result',{}).get('success') is False for r in calls),'executor_terminal_calls':terminal,'covered':bool(calls or terminal),'persona_or_root_turns':refs,'transport_query_types':sorted({r['args'].get('query_type','auto') for r in calls}) if tool=='get_transport_info' else None})
write(DEST/'TOOL_COVERAGE.json',coverage)
api=read(B/'evidence/api-total-summary.json');contract=read(B/'evidence/contract-summary.json');fault=read(B/'fault/evidence/fault-summary.json');protocol=read(B/'evidence/protocol-summary.json');resilience=read(B/'evidence/resilience-summary.json');auth=read(B/'evidence/auth-ui-checks.json');business=read(B/'evidence/business-invariants.json');outage=read(B/'evidence/model-outage-checks.json');scope=read(B/'evidence/runtime-memory-scope-checks.json')
for p in (B/'fault/evidence').glob('*.json*'):
 q=DEST/'evidence/fault'/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
assert len(coverage)==14 and all(x['covered'] for x in coverage)
assert not api.get('failed') and api['checks']==api['passed']==253
assert contract['checks']==contract['passed']==50 and fault['checks']==fault['passed']==19
assert protocol['checks']==protocol['passed']==34 and resilience['checks']==resilience['passed']==8
assert len(auth)==9 and all(x['passed'] for x in auth)
assert scope and all(x['passed'] for x in scope)
ns={r['n'] for r in models};gaps=sorted(set(range(1,max(ns)+1))-ns)
network={'container_network_mode':'none','container_readonly_root':True,'public_port_bindings':{},'browser_origin':'http://127.0.0.1:15006','model_upstream_allowlist':['api.deepseek.com'],'otm_transport_attempts':len(lines(B/'evidence/forbidden-attempts.jsonl')),'socket_external_attempts_blocked':len(lines(B/'private/blocked_network.jsonl')),'housing_producer_cache_only':True,'model_call_budget':320,'model_token_budget':1600000,'browser_external_requests_are_aborted':True}
assert network['otm_transport_attempts']==0
write(DEST/'evidence/network-isolation.json',network)
verification={'audit_date':'2026-10-19','audit_execution_complete':True,'product_acceptance':'FAIL','product_source_unchanged':read(B/'evidence/product-source-verification.json')['product_source_unchanged'],'personas':personas,'user_actions_total':sum(p['user_actions'] for p in personas.values()),'api_alex_requests_total':sum(p['api_alex_requests'] for p in personas.values()),'completed_persona_chat_turns':sum(p['completed_chat_ledgers'] for p in personas.values()),'persona_chat_turns_with_real_model':sum(p['chat_turns_with_model'] for p in personas.values()),'tools_registered':len(schema),'tools_e2e_covered':sum(x['covered'] for x in coverage),'api_authorization':api,'tool_integration':contract,'upstream_fault_integration':fault,'http_sse_protocol':protocol,'concurrency_disconnect':resilience,'auth_ui':{'checks':len(auth),'passed':sum(x['passed'] for x in auth)},'runtime_memory_scope':{'checks':len(scope),'passed':sum(x['passed'] for x in scope)},'business_invariants':{'checks':len(business),'passed':sum(x['passed'] for x in business),'failed':[x['case'] for x in business if not x['passed']]},'model_outage':{'checks':len(outage),'passed':sum(x['passed'] for x in outage),'failed':[x['case'] for x in outage if not x['passed']]},'model_requests':{'observed':len(models),'real_upstream_requests':sum(not r.get('synthetic_failure') and not r.get('synthetic_response') for r in models),'synthetic_503':sum(bool(r.get('synthetic_failure')) for r in models),'synthetic_tool_call_responses':sum(bool(r.get('synthetic_response')) for r in models),'total_reported_tokens':sum(r.get('usage',{}).get('total_tokens',0) for r in models),'sequence_gaps':gaps},'actual_container_restarts':len(lines(B/'evidence/restart-events.jsonl')),'network':network,'scope_limits':['Frozen historical listings; no live housing availability or current price verified','Synthetic TfL/Police/OSM/weather/web transport responses; live upstream conformance not established','Minutes-long repeated sessions and actual restarts; not a multiweek wall-clock soak','Current fc_loop configuration only; optional manager/MCP architectures not exercised','Map HTML and escaping tested; external map tiles, listing images and View Deal navigation blocked']}
write(DEST/'VERIFICATION.json',verification)
print(json.dumps(verification,ensure_ascii=False,indent=2))
