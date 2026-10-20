import json,os,pathlib,subprocess,sys,time,urllib.request
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019');mode=sys.argv[1] if len(sys.argv)>1 else '0'
subprocess.run(['docker','stop','-t','2','rentcompass-e2e-tools-test'],check=True,stdout=subprocess.DEVNULL)
event={'at':time.time(),'action':'actual_container_restart','require_auth':mode}
with (B/'evidence/restart-events.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
(B/'runtime/web.sock').unlink(missing_ok=True)
with (B/'private/serve-process.log').open('a') as log:
 p=subprocess.Popen(['python3',str(B/'harness/serve.py')],env=dict(os.environ,AUDIT_REQUIRE_AUTH=mode),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
pids=json.loads((B/'private/pids.json').read_text());pids['serve']=p.pid;(B/'private/pids.json').write_text(json.dumps(pids))
for n in range(80):
 try:
  with urllib.request.urlopen('http://127.0.0.1:15006/health',timeout=1) as response:
   if response.status==200:print(json.dumps({'ready':True,'require_auth':mode}));break
 except Exception:time.sleep(.25)
else:raise RuntimeError('restarted service not ready')
