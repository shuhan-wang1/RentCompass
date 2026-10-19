from pathlib import Path
import os,signal,json,time,socket,subprocess
B=Path('/tmp/rentcompass-fix-live-20261019');before=json.loads((B/'evidence/existing-services-before.json').read_text());fmt='{"name":{{json .Name}},"id":{{json .Id}},"started":{{json .State.StartedAt}}}'
r=subprocess.run(['docker','inspect','--format',fmt,*[r['name']for r in before]],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True);after=[json.loads(x)for x in r.stdout.splitlines()]
for row in after:row['name']=row['name'].lstrip('/')
(B/'evidence/existing-services-after.json').write_text(json.dumps(after,indent=2));checks={old['name']:next(x for x in after if x['name']==old['name'])==old for old in before};assert all(checks.values())
terminated=[]
for label,pid in json.loads((B/'private/pids.json').read_text()).items():
 try:
  assert str(B).encode()in Path(f'/proc/{pid}/cmdline').read_bytes()
  assert os.getpgid(pid)==pid
  os.killpg(pid,signal.SIGTERM);terminated.append(label)
 except ProcessLookupError:pass
time.sleep(1);closed={}
for port in (15019,19094):
 with socket.socket()as sock:sock.settimeout(.5);closed[str(port)]=sock.connect_ex(('127.0.0.1',port))!=0
out={'existing_container_ids_and_start_times_unchanged':checks,'only_test_process_groups_terminated':terminated,'isolated_ports_closed':closed,'public_site_not_reopened':True,'at':time.time()};(B/'evidence/final-service-state.json').write_text(json.dumps(out,indent=2));print(json.dumps(out));assert all(closed.values())
