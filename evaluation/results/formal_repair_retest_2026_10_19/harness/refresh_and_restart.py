from pathlib import Path
import sys,json,os,signal,shutil,hashlib,time,subprocess,urllib.request
B=Path('/tmp/rentcompass-fix-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');sys.path.insert(0,str(R))
from evaluation.results_package import workspace_source_paths
manifest={}
for name in workspace_source_paths(R):
 p=R/name
 if not p.is_file() or p.name=='.env' or '.runtime' in p.parts or (name.startswith('evaluation/results/') and not name.startswith('evaluation/results/_harness/')):continue
 target=B/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target);manifest[name]=hashlib.sha256(target.read_bytes()).hexdigest()
old=B/'evidence/source-manifest.json'
if old.exists():shutil.copy2(old,B/'evidence/source-manifest-before-student-fixes.json')
old.write_text(json.dumps(manifest,indent=2))
pids=json.loads((B/'private/pids.json').read_text());os.killpg(pids['app'],signal.SIGTERM)
time.sleep(1)
namespace={};exec((B/'harness/start.py').read_text().split('pids={}')[0],namespace)
f=(B/'private/app.log').open('a');p=subprocess.Popen([namespace['P'],str(B/'harness/boot.py')],cwd=B/'source',env=namespace['e'],stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
pids['app']=p.pid;(B/'private/pids.json').write_text(json.dumps(pids))
for n in range(90):
 try:
  with urllib.request.urlopen('http://127.0.0.1:15019/ready',timeout=1) as response:
   if response.status==200:
    print(json.dumps({'ready':True,'source_files':len(manifest),'signed_identity_secret_preserved':True}));break
 except Exception:time.sleep(.4)
else:raise RuntimeError('Test restart not ready')
