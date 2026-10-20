from pathlib import Path
import sys,shutil,hashlib,json,secrets,subprocess
B=Path('/tmp/rentcompass-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');sys.path.insert(0,str(R));from evaluation.results_package import workspace_source_paths
manifest={}
for f in workspace_source_paths(R):
 p=R/f
 if not p.is_file() or p.name=='.env' or '.runtime' in p.parts or (f.startswith('evaluation/results/') and not f.startswith('evaluation/results/_harness/')):continue
 q=B/'source'/f;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q);manifest[f]=hashlib.sha256(q.read_bytes()).hexdigest()
(B/'evidence/source-manifest.json').write_text(json.dumps(manifest,indent=2))
values={}
for line in (R/'app/.env').read_text().splitlines():
 if '=' not in line or line.lstrip().startswith('#'):continue
 k,v=line.split('=',1);values[k.strip()]=v.strip().strip('\"\'')
values['FLASK_SECRET_KEY']=secrets.token_hex(32)
p=B/'private/env.json';p.write_text(json.dumps(values));p.chmod(0o600)
old=R/'evaluation/results/e2e_repairs_2026_10_19/harness'
s=(old/'relay.py').read_text().replace('/tmp/rentcompass-e2e-repair-20261019',str(B)).replace('n>320 or used>1600000','n>100 or used>600000');(B/'harness/relay.py').write_text(s)
s=(old/'instrumentation.py').read_text().replace('from transport_fixtures import emit','from network_guard import emit');(B/'harness/instrumentation.py').write_text(s)
rows=[]
for n in ['uk-rent-app-fc','uk-rent-app','uk-rent-searxng','uk-rent-valkey']:
 r=subprocess.run(['docker','inspect',n],capture_output=True,text=True)
 if r.returncode==0:
  d=json.loads(r.stdout)[0];rows.append({'name':n,'id':d['Id'],'started':d['State']['StartedAt']})
(B/'evidence/existing-services-before.json').write_text(json.dumps(rows,indent=2));print({'source_files':len(manifest),'model_configured':bool(values.get('DEEPSEEK_API_KEY')),'fresh_test_databases':True})
