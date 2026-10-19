"""Archive sanitized evidence; credentials and runtime databases are excluded."""
from pathlib import Path
import collections,datetime,hashlib,json,re,shutil,subprocess,xml.etree.ElementTree as ET
B=Path('/tmp/rentcompass-e2e-repair-20261019');R=Path('/home/shuhan/uk_rent_recommendation');DEST=R/'evaluation/results/e2e_repairs_2026_10_19';DEST.mkdir(parents=True,exist_ok=True)
def read(p):return json.loads(p.read_text())
def lines(p):return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2,ensure_ascii=False)+'\n')
secrets={'cookie','set-cookie','authorization','password','auth_sid','csrf_token','access_token','refresh_token','api_key','deepseek_api_key','flask_secret_key'}
def redact(v):
 if isinstance(v,dict):
  if str(v.get('name','')).lower() in secrets:return {**v,'value':'[REDACTED]'}
  return {k:'[REDACTED]' if k.lower() in secrets else redact(x) for k,x in v.items()}
 if isinstance(v,list):return [redact(x) for x in v]
 if isinstance(v,str) and v.lstrip().startswith(('{','[')):
  try:return json.dumps(redact(json.loads(v)),ensure_ascii=False)
  except (ValueError,TypeError):pass
 return v
for folder in ['evidence','student_retest','professional_retest','family_retest','root','harness','public','ui-offline','fault/evidence']:
 for p in (B/folder).rglob('*'):
  if not p.is_file() or '__pycache__' in p.parts or 'storage' in p.name.lower() or p.suffix not in {'.json','.jsonl','.png','.py','.txt','.md'}:continue
  q=DEST/p.relative_to(B);q.parent.mkdir(parents=True,exist_ok=True)
  if p.suffix=='.json':write(q,redact(read(p)))
  elif p.suffix=='.jsonl':q.write_text(''.join(json.dumps(redact(x),ensure_ascii=False)+'\n' for x in lines(p)))
  else:shutil.copy2(p,q)
for name in ['candidate4full','candidate3full','finalfull','candidate3-target','candidate4-refine-target']:
 for suffix in ['.log','.xml']:
  p=B/'artifacts'/(name+suffix)
  if p.exists():q=DEST/'tests'/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
for stem in ['sentence-final-v2','recap-proof-final-v3']:
 for suffix in ['.log','.xml']:
  p=Path('/tmp/rentcompass-student-freeze2-i79mx8_g/runtime')/(stem+suffix)
  if p.exists():q=DEST/'tests'/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
ledger=[]
for line in (B/'private/server.log').read_text().splitlines():
 try:
  outer=json.loads(line);r=json.loads(outer.get('message',''))
  if r.get('event')=='canary.turn':ledger.append(r)
 except (ValueError,TypeError):pass
write(DEST/'evidence/completed-turn-ledger.json',ledger)
dispatches=lines(B/'evidence/tool-dispatches.jsonl');models=lines(B/'evidence/model-calls.jsonl')
tools=['search_properties','calculate_commute','calculate_commute_cost','check_safety','get_weather','web_search','search_nearby_pois','get_property_details','check_transport_cost','get_transport_info','remember','recall_memory','ask_user','compare_or_rank_areas'];coverage=[]
for name in tools:
 ds=[d for d in dispatches if d['tool']==name];terminal=sum((r.get('tool_latency')or{}).get('ask_user',{}).get('count',0) for r in ledger) if name=='ask_user' else 0
 coverage.append({'tool':name,'real_conversation_execute_attempts':len(ds),'logical_successes':sum(d.get('result',{}).get('success') is True for d in ds),'logical_failures':sum(d.get('result',{}).get('success') is False for d in ds),'executor_terminal_calls':terminal,'covered_in_browser_conversation':bool(ds or terminal),'request_ids':sorted({d['request_id'] for d in ds}) if not terminal else [r['request_id'] for r in ledger if (r.get('tool_latency')or{}).get('ask_user')]})
write(DEST/'TOOL_COVERAGE.json',coverage)
root=ET.parse(B/'artifacts/candidate4full.xml').getroot();suite=root.find('testsuite');total=int(suite.get('tests'));skipped=int(suite.get('skipped'));fail=int(suite.get('failures'));errors=int(suite.get('errors'))
unit={'passed':total-skipped-fail-errors,'skipped':skipped,'failed':fail,'errors':errors,'seconds':float(suite.get('time')),'skip_reasons':[{'test':t.get('classname')+'.'+t.get('name'),'reason':t.find('skipped').get('message')} for t in root.iter('testcase') if t.find('skipped') is not None]}
api_phases={p:read(B/f'evidence/{p}-summary.json') for p in ['guest','strict','reproduce','after-restart']};api={'checks':sum(p['checks'] for p in api_phases.values()),'passed':sum(p['passed'] for p in api_phases.values()),'phases':api_phases}
checks={name:read(B/f'evidence/{name}-checks.json') for name in ['candidate4-student','candidate4-family','model-outage','runtime-memory-scope']}
checks['candidate4-family'].append({'case':'weather-dispatch-is-weather-only','passed':[d['tool'] for d in read(B/'evidence/candidate4-family-weather-dispatch.json')]==['get_weather']})
last_area=next(r for r in reversed(lines(B/'root/root-turns.jsonl')) if 'Area comparison:' in ((r.get('result')or{}).get('payload')or{}).get('message',''))
checks['area-scope']=[{'case':'mixed-cache-scope-not-claimed-as-whole-flat-data','passed':'without room-type or bedroom filtering' in last_area['result']['payload']['message']}]
verification={'date':'2026-10-19','final_candidate':4,'verdict':'PASS_WITH_SCOPE_LIMITS','unit_regression':unit,'source_frozen_consistency':read(B/'evidence/final-source-verification.json'),'persona_agent_rounds':{'student':12,'professional':10,'family':12},'parent_final_replays':{'student':1,'family':2,'area_ranking':1,'zone_cost_reference':1},'persona_independence':{'student':'fresh context; black-box API/browser; no product implementation context','professional':'fresh context; black-box API/browser; no product implementation context','family':'reused accessibility repair agent; repair-aware; not a blind test','candidate4_parent_checks':'implementation-aware acceptance replay; not blind'},'tools_registered':len(tools),'tools_covered_in_browser_conversation':sum(c['covered_in_browser_conversation'] for c in coverage),'api_authorization':api,'tool_integration':read(B/'evidence/contract-summary.json'),'upstream_fault_integration':read(B/'fault/evidence/fault-summary.json'),'http_sse_protocol':read(B/'evidence/protocol-summary.json'),'concurrency_disconnect':read(B/'evidence/resilience-summary.json'),'auth_ui':{'checks':len(read(B/'evidence/auth-ui-checks.json')),'passed':sum(c['passed'] for c in read(B/'evidence/auth-ui-checks.json'))},'offline_ui':{'checks':read(B/'ui-offline/verification.json')['passed'],'mock_chat_requests':read(B/'ui-offline/verification.json')['mock_chat_requests']},'final_acceptance':checks,'actual_container_restarts':len(lines(B/'evidence/restart-events.jsonl')),'model_requests':{'observed':len(models),'real_upstream_requests':sum(not r.get('synthetic_failure') and not r.get('synthetic_response') for r in models),'synthetic_503':sum(bool(r.get('synthetic_failure')) for r in models),'synthetic_tool_call_responses':sum(bool(r.get('synthetic_response')) for r in models),'reported_tokens':sum(r.get('usage',{}).get('total_tokens',0) for r in models),'request_budget':320,'token_budget':1600000},'network':{'container_network':'none','read_only_root':True,'container_public_bindings':{},'browser_origin':'http://127.0.0.1:15008','browser_external_requests_aborted':True,'model_upstream_allowlist':['api.deepseek.com'],'housing_cache_only':True,'on_the_market_transport_attempts':len(lines(B/'evidence/forbidden-attempts.jsonl')),'socket_external_attempts_blocked':len(lines(B/'private/blocked_network.jsonl')),'model_uses_existing_configuration':True},'limits':['Historical frozen listings; current prices and availability not verified','Weather, TfL, Police, OSM and web HTTP transport responses are synthetic; real tool implementations run','Repeated sessions, refreshes and six actual restarts; not a weeks-long soak','Current fc_loop configuration; optional manager and MCP architectures not E2E exercised','External map tiles, images and listing navigation blocked','One existing Starlette WSGI deprecation warning; no deployment performed']}
assert not fail and not errors and api['checks']==api['passed'] and all(c['covered_in_browser_conversation'] for c in coverage)
assert all(c['passed'] for cs in checks.values() for c in cs)
for n in ['tool_integration','upstream_fault_integration','http_sse_protocol','concurrency_disconnect','auth_ui']:assert verification[n]['checks']==verification[n]['passed']
write(DEST/'VERIFICATION.json',verification)
# Check exact test credentials in text artifacts without printing any values.
needles=[]
for p in (B/'private').glob('*'):
 if p.name=='model.env':needles += [line.split('=',1)[1].strip() for line in p.read_text().splitlines() if '=' in line and line.split('=',1)[0] in {'DEEPSEEK_API_KEY','FLASK_SECRET_KEY'}]
 elif p.name=='clients.json':
  s=read(p);needles += [d['password'] for d in s.get('credentials',{}).values()];needles+=list(s.get('cookies',{}).values())
for folder in ['student_retest','professional_retest','family_retest','root']:
 for p in (B/folder).glob('*storage.json'):
  s=read(p);needles += [c['value'] for c in s.get('cookies',[]) if c.get('value')]
leaks=[]
for p in DEST.rglob('*'):
 if p.is_file() and p.suffix in {'.json','.jsonl','.py','.txt','.md','.log','.xml'}:
  text=p.read_text(errors='replace')
  if any(n and len(n)>12 and n in text for n in needles):leaks.append(str(p.relative_to(DEST)))
assert not leaks, 'Credential redaction verification failed in: '+repr(leaks)
write(DEST/'evidence/privacy-verification.json',{'passed':True,'storage_and_credential_files_excluded':True,'private_credential_substrings_checked':len(needles),'artifacts_with_exact_credential_match':[]})
print(json.dumps({'destination':str(DEST),'unit':unit['passed'],'api_checks':api['checks'],'covered_tools':len(coverage),'models':verification['model_requests'],'files':sum(p.is_file() for p in DEST.rglob('*'))},ensure_ascii=False))
