import hashlib,json,os,pathlib,secrets,shutil,sqlite3,subprocess,sys
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019');R=pathlib.Path('/home/shuhan/uk_rent_recommendation');OLD=pathlib.Path('/tmp/rentcompass-personas-20261019')
sys.path.insert(0,str(R))
from evaluation.results_package import workspace_source_paths
manifest={}
for name in workspace_source_paths(R):
 p=R/name
 if not name or not p.is_file() or p.name=='.env' or '.runtime' in p.parts or name.startswith('evaluation/results/'):continue
 target=B/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target);manifest[name]=hashlib.sha256(p.read_bytes()).hexdigest()
(B/'evidence/source-manifest.json').write_text(json.dumps(manifest,indent=2))
for name in ['listing_cache.sqlite3','otm_desc_cache.sqlite3','area_reco_cache.sqlite3']:
 src=sqlite3.connect(OLD/'runtime'/name);dst=sqlite3.connect(B/'runtime'/name);src.backup(dst);src.close();dst.close()
src=sqlite3.connect(OLD/'runtime/runtime_cache.sqlite3');dst=sqlite3.connect(B/'runtime/runtime_cache.sqlite3');dst.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY,value TEXT NOT NULL,accessed REAL NOT NULL)');count=0
for row in src.execute('SELECT key,value,accessed FROM cache'):
 if 'geocode-v3' in row[1]:dst.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',row);count+=1
dst.commit();dst.close();src.close()
for name in ['serve.py','boot.py','relay.py','proxy.py','sitecustomize.py']:
 text=(OLD/'harness'/name).read_text().replace('rentcompass-personas-20261019','rentcompass-e2e-tools-20261019').replace('rentcompass-persona-test','rentcompass-e2e-tools-test').replace('15004','15006')
 if name=='serve.py':text=text.replace("'SEARXNG_URL':''","'SEARXNG_URL':'http://127.0.0.1:19092'").replace("'APP_CANDIDATE_SHA':'persona-audit-working-tree'","'APP_CANDIDATE_SHA':'e2e-tools-frozen-working-tree'").replace("'FLASK_SECRET_KEY':'isolated-persona-audit-20261019-secret',",'')
 if name=='boot.py':text=text.replace('import uvicorn', 'import transport_fixtures, instrumentation\ntransport_fixtures.install()\ninstrumentation.install()\nimport uvicorn')
 if name=='relay.py':
  text=text.replace("calls+=1;n=calls", "calls+=1;n=calls\n            used=sum(r.get('usage',{}).get('total_tokens',0) for r in records)")
  text=text.replace('if n>500:', 'if n>320 or used>1600000:')
  text=text.replace("with (BASE/'private/model_calls.jsonl').open('a')", "records.append(record)\n            with (BASE/'evidence/model-calls.jsonl').open('a')")
  text=text.replace('lock=threading.Lock();calls=0','lock=threading.Lock();calls=0;records=[]')
 for directory in ['harness']:(B/directory/name).write_text(text)
for name in ['transport_fixtures.py','instrumentation.py']:shutil.copy2(R/'evaluation/e2e_tool_audit'/name,B/'harness'/name)
text=(OLD/'public/test_driver.py').read_text().replace('15004','15006');(B/'public/test_driver.py').write_text(text)
values={}
for line in (R/'app/.env').read_text().splitlines():
 if '=' not in line or line.lstrip().startswith('#'):continue
 k,v=line.split('=',1)
 if k.strip() in {'DEEPSEEK_API_KEY','DEEPSEEK_MODEL'}:values[k.strip()]=v.strip().strip('\"\'')
assert values.get('DEEPSEEK_API_KEY'),'configured model key missing'
values['FLASK_SECRET_KEY']=secrets.token_hex(32)
p=B/'private/model.env';p.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));p.chmod(0o600)
(B/'runtime/fixture-mode.json').write_text('{}')
(B/'public/BRIEF.txt').write_text('Local audit http://127.0.0.1:15006. Real configured LLM, frozen historical housing. Weather/TfL/Police/OSM/web are synthetic HTTP-boundary fixtures, not live data. Actual tool schemas, dispatch, policies, filters, arithmetic and stores are unchanged. External requests blocked, Docker network none. Do not read old reports or other persona evidence. Each agent max14 chat requests, own browser profile only.\n')
print(json.dumps({'frozen_files':len(manifest),'geocode_entries':count,'private_key_loaded':bool(values.get('DEEPSEEK_API_KEY')),'port':15006}))
