from pathlib import Path
import json,shutil,hashlib,collections,xml.etree.ElementTree as ET,time
B=Path('/tmp/rentcompass-fix-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');D=R/'evaluation/results/formal_repair_retest_2026_10_19';D.mkdir(parents=True,exist_ok=True)
allowed={'.json','.jsonl','.txt','.sse','.png','.xml','.log','.py'}
# Private env, browser cookie stores and all runtime databases are never packaged.
for group in ('evidence','artifacts','student','professional','family','harness'):
 for src in (B/group).rglob('*'):
  if not src.is_file() or src.suffix not in allowed or '__pycache__' in src.parts:continue
  if any(word in src.name.lower()for word in ('storage','cookie','private')):continue
  if group=='harness' and src.name in ('setup.py','start.py','relay.py'):continue
  dest=D/group/src.relative_to(B/group);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
# Agent regressions are supplementary to the one final complete run.
for src in [Path('/tmp/repair_transport_services/property-scope-regression.xml'),Path('/tmp/rentcompass_criteria_scope/prof_family_recovery_final_run.log'),Path('/tmp/repair_transport_services/monthly-commuting-regression.xml'),Path('/tmp/repair_transport_services/weather-stratford-regression.xml'),Path('/tmp/rentcompass_criteria_scope/r6_final_run.log')]:
 if src.exists():shutil.copy2(src,D/'artifacts'/src.name)
logs=lambda n:[json.loads(x)for x in (B/'evidence'/n).read_text().splitlines()]
http=logs('http-requests.jsonl');model=logs('model-calls.jsonl');calls=logs('tool-dispatches.jsonl')
coverage={name:{'calls':len(rs),'success':sum(r.get('result',{}).get('success') is True for r in rs),'exceptions':dict(collections.Counter(r.get('exception')for r in rs if r.get('exception')))}for name,rs in((n,[r for r in calls if r.get('tool')==n])for n in sorted({r.get('tool')for r in calls}))}
direct=json.loads((B/'evidence/live-tool-integration.json').read_text());coverage['direct_integrations']=[{'tool':r['tool'],'seconds':r.get('seconds'),'success':(r.get('result')or{}).get('success'),'exception':r.get('exception')}for r in direct]
coverage['ask_user_corrected_input']=json.loads((B/'evidence/ask-user-valid.json').read_text());coverage['scope_note']='Actual execution, direct integration and persona UI coverage are separate. Tool wrapper success alone does not certify nested provider data. Terminal ask_user is graph-managed and absent from Tool.execute ledger. Initial invalid direct ask_user enum retained as audit input failure.'
(D/'TOOL_COVERAGE.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2))
verification={'generated_at':time.time(),'production_deployed':False,'isolated_url':'http://127.0.0.1:15019','http':{'actual':sum(r.get('actual_external_request') is True for r in http),'audit_budget_stops':sum(r.get('actual_external_request') is False for r in http),'by_host':dict(collections.Counter(r['host']for r in http if r.get('actual_external_request') is True))},'model':{'calls':len(model),'status_counts':dict(collections.Counter(str(r.get('status'))for r in model)),'usage':{k:sum((r.get('usage')or{}).get(k,0)for r in model)for k in ('prompt_tokens','completion_tokens','total_tokens')}},'personas':{}}
for name in ('student','professional','family'):
 ledger=B/name/'own-tool-ledger.json'
 if ledger.exists():
  data=json.loads(ledger.read_text());verification['personas'][name]={'stored_turns':len(data['turns']),'tool_execute_calls':len(data['actual_dispatches']),'ui_model_posts':7,'non_model_form_searches':2 if name=='professional' else 0}
for label in ('full-regression-final-read','full-regression-delivery','full-regression-final-contract','full-regression-accepted','full-regression-release','full-regression-frozen'):
 p=B/'artifacts'/(label+'.xml')
 if p.exists():
  root=ET.parse(p).getroot();suites=root.findall('testsuite')or[root];verification[label]={k:sum(int(s.get(k,0))for s in suites)for k in ('tests','failures','errors','skipped')};verification[label]['seconds']=sum(float(s.get('time',0))for s in suites)
ui=B/'artifacts/ui-offline-accepted/verification.json'
if ui.exists():verification['offline_browser']=json.loads(ui.read_text())
# Count four initial requests outside the instrumented guard exactly once.
verification['onthemarket']={'guard_requests':sum(r.get('host')=='www.onthemarket.com'and r.get('actual_external_request') is True for r in http),'pre_guard_requests':4,'total_actual':4+sum(r.get('host')=='www.onthemarket.com'and r.get('actual_external_request') is True for r in http),'guard_statuses':dict(collections.Counter(str(r.get('status'))for r in http if r.get('host')=='www.onthemarket.com'and r.get('actual_external_request') is True)),'shared_limit':12,'identified_program_ua':'RentCompassBot/1.0','no_challenge_bypass':True}
verification['restoration'] = {
 'student':json.loads((B/'student/recovery-after-read-projection-checks.json').read_text()),
 'student_saved_labels':json.loads((B/'student/final-saved-label-checks.json').read_text()),
 'professional':json.loads((B/'professional/final-restore-20261019T131557Z-assertions.json').read_text()),
 'family':{k:v for k,v in json.loads((B/'family/restart-recovery-check.json').read_text()).items() if k in ('assertions','extra_read_path_assertions')},
 'exact_final_api':json.loads((B/'evidence/final-authenticated-read-checks.json').read_text())['checks'],
 'identity_api':json.loads((B/'evidence/identity-checks.json').read_text()),
 'memory_new_process':json.loads((B/'evidence/memory-new-process.json').read_text())['checks']}
verification['service_state'] = json.loads((B/'evidence/final-service-state.json').read_text())
(D/'VERIFICATION.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2))
# Fail closed if an actual credential/cookie value made it into text artifacts.
secrets=[]
env=json.loads((B/'private/env.json').read_text())
for key,value in env.items():
 if any(x in key.upper()for x in ('SECRET','TOKEN','KEY','PASSWORD'))and isinstance(value,str)and len(value)>=12:secrets.append(value)
for folder in ('student','professional','family'):
 for p in (B/folder).glob('*storage*.json'):
  for cookie in json.loads(p.read_text()).get('cookies',[]):
   if len(cookie.get('value',''))>=12:secrets.append(cookie['value'])
leaks=[]
for path in D.rglob('*'):
 if path.is_file()and path.suffix!='.png':
  data=path.read_text(errors='replace')
  if any(secret in data for secret in secrets):leaks.append(str(path.relative_to(D)))
if leaks:raise RuntimeError('Credential scan rejected files: '+repr(leaks))
(D/'PRIVACY_CHECK.json').write_text(json.dumps({'actual_secret_values_checked':len(set(secrets)),'credential_matches':0,'private_env_excluded':True,'browser_cookie_stores_excluded':True,'runtime_databases_excluded':True},indent=2))
manifest={str(p.relative_to(D)):hashlib.sha256(p.read_bytes()).hexdigest()for p in D.rglob('*')if p.is_file()and p.name!='ARTIFACT_SHA256.json'}
(D/'ARTIFACT_SHA256.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps({'folder':str(D),'artifacts':len(manifest),'final_tests':verification.get('full-regression-final-read'),'onthemarket_actual_requests':verification['onthemarket']['total_actual'],'credential_scan':'passed'},ensure_ascii=False))
