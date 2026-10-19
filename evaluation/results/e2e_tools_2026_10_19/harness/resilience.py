import concurrent.futures,http.client,http.cookiejar,json,pathlib,socket,threading,time,urllib.request,uuid
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019');URL='http://127.0.0.1:15006';checks=[];records=[]
state=json.loads((B/'private/clients.json').read_text());jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def normal(path,data=None,method='POST',headers=None):
 h={'Content-Type':'application/json'};h.update(headers or {});req=urllib.request.Request(URL+path,json.dumps(data).encode() if data is not None else None,headers=h,method=method)
 try:resp=opener.open(req,timeout=180)
 except urllib.error.HTTPError as exc:resp=exc
 raw=resp.read().decode()
 try:body=json.loads(raw)
 except Exception:body=raw
 records.append({'path':path,'method':method,'status':resp.status,'body':body});return resp.status,body
normal('/api/auth/login',state['credentials']['A']);_,created=normal('/api/conversations',{'title':'Synthetic concurrency and disconnect audit'});cid=created['conversation']['id'];cookie='; '.join(c.name+'='+c.value for c in jar)
ready=threading.Event();first_id='resilience-'+uuid.uuid4().hex
message='Compare Bloomsbury and Camden in London for value and a commute to UCL under 45 minutes. Use cached rent evidence; keep missing evidence explicit.'
def stream_worker(request_id,text,disconnect=False):
 con=http.client.HTTPConnection('127.0.0.1',15006,timeout=180);headers={'Content-Type':'application/json','Accept':'text/event-stream','Cookie':cookie,'X-Request-Id':request_id};con.request('POST','/api/alex',json.dumps({'message':text,'conversation_id':cid}),headers=headers);resp=con.getresponse();lines=[]
 while True:
  line=resp.readline()
  if not line:break
  lines.append(line.decode())
  if b'event: step' in line:
   ready.set()
   if disconnect:resp.close();con.close();return {'status':resp.status,'disconnected_after_step':True,'raw':''.join(lines)}
 con.close();return {'status':resp.status,'raw':''.join(lines)}
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
 f=pool.submit(stream_worker,first_id,message);assert ready.wait(30),'no actual SSE progress received'
 started=time.time();status,health=normal('/health',method='GET');checks.append({'case':'health-responsive-during-tool-turn','passed':status==200 and time.time()-started<2,'seconds':round(time.time()-started,3)})
 status,other=normal('/api/alex',{'message':'Hello'},headers={'X-Request-Id':'independent-'+uuid.uuid4().hex});checks.append({'case':'independent-conversation-remains-usable','passed':status==200 and other.get('conversation_id')!=cid,'status':status})
 status,body=normal('/api/alex',{'message':'Change the same plan while it is running','conversation_id':cid},headers={'X-Request-Id':'concurrent-'+uuid.uuid4().hex});checks.append({'case':'same-conversation-single-flight','passed':status==409,'status':status,'body':body})
 status,body=normal('/api/alex',{'message':message,'conversation_id':cid},headers={'X-Request-Id':first_id});checks.append({'case':'inflight-request-retry-no-second-execution','passed':status in (409,202),'status':status,'body':body})
 first=f.result(timeout=180);records.append({'case':'initial-stream',**first});checks.append({'case':'one-sse-final','passed':first['status']==200 and first['raw'].count('event: final')==1})
 status,replay=normal('/api/alex',{'message':message,'conversation_id':cid},headers={'X-Request-Id':first_id});checks.append({'case':'completed-request-replay','passed':status==200 and replay.get('replayed') is True,'status':status})
 status,turns=normal(f'/api/conversations/{cid}/turns',method='GET');checks.append({'case':'duplicate-requests-do-not-append-turns','passed':status==200 and len(turns.get('turns',[]))==1,'actual_turns':len(turns.get('turns',[]))})
ready.clear();cancel_id='disconnect-'+uuid.uuid4().hex;cancelled=stream_worker(cancel_id,'Please check Bloomsbury weather and neighbourhood safety now, and summarize the evidence.',True);records.append({'case':'client-disconnect',**cancelled});time.sleep(1)
status,recovery=normal('/api/alex',{'message':'Hello','conversation_id':cid},headers={'X-Request-Id':'after-cancel-'+uuid.uuid4().hex});checks.append({'case':'disconnect-releases-conversation-lease','passed':status==200 and recovery.get('turn_id') is not None,'status':status,'body':recovery})
status,turns=normal(f'/api/conversations/{cid}/turns',method='GET');records.append({'case':'turns-after-disconnect','body':turns})
(B/'evidence/resilience-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));(B/'evidence/resilience-requests.json').write_text(json.dumps(records,ensure_ascii=False,indent=2));summary={'checks':len(checks),'passed':sum(c['passed'] for c in checks),'failed':[c for c in checks if not c['passed']]};(B/'evidence/resilience-summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
