import pathlib,subprocess,shutil,hashlib,json,time,urllib.request,sys
B=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019');R=pathlib.Path('/home/shuhan/uk_rent_recommendation');sys.path.insert(0,str(R))
from evaluation.results_package import workspace_source_paths
N=B/'web-source-next';N.mkdir(exist_ok=True);manifest={}
for f in workspace_source_paths(R):
 p=R/f
 if not p.is_file() or p.name=='.env' or '.runtime' in p.parts:continue
 t=N/f;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t);manifest[f]=hashlib.sha256(t.read_bytes()).hexdigest()
(B/'evidence/web-source-manifest-v4.json').write_text(json.dumps(manifest,indent=2))
subprocess.run(['docker','stop','rentcompass-e2e-repair-test'],check=True,capture_output=True)
(B/'web-source').rename(B/'web-source-v3');N.rename(B/'web-source');(B/'runtime/web.sock').unlink(missing_ok=True)
f=(B/'private/serve-process.log').open('a');p=subprocess.Popen(['python3',str(B/'harness/serve.py')],stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
pids=json.loads((B/'private/pids.json').read_text());pids['serve']=p.pid;(B/'private/pids.json').write_text(json.dumps(pids))
for i in range(80):
 try:
  with urllib.request.urlopen('http://127.0.0.1:15008/ready',timeout=2) as resp:
   body=json.load(resp)
   if resp.status==200:break
 except Exception:time.sleep(.5)
else:raise RuntimeError('candidate4 not ready')
record={'candidate':4,'frozen_at':time.time(),'files':len(manifest),'readiness':body}
(B/'evidence/freeze4-marker.json').write_text(json.dumps(record,indent=2));print({'candidate':4,'files':len(manifest),'ready':body['status']})
