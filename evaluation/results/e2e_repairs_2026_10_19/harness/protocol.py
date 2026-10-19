import collections,concurrent.futures,http.cookiejar,json,pathlib,time,urllib.request,urllib.error,uuid
B=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019');URL='http://127.0.0.1:15008';jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar));records=[];checks=[]
def call(name,path='/api/alex',data=None,raw=None,headers=None,expected=(200,),method='POST',predicate=None):
 h={'Content-Type':'application/json','X-Request-Id':'protocol-'+uuid.uuid4().hex};h.update(headers or {});body=raw if raw is not None else json.dumps(data).encode() if data is not None else None;t=time.time()
 try:r=opener.open(urllib.request.Request(URL+path,body,headers=h,method=method),timeout=180)
 except urllib.error.HTTPError as e:r=e
 rawbody=r.read().decode();status=r.status
 try:parsed=json.loads(rawbody)
 except Exception:parsed=rawbody
 record={'case':name,'path':path,'method':method,'status':status,'content_type':r.headers.get('Content-Type'),'retry_after':r.headers.get('Retry-After'),'seconds':round(time.time()-t,3),'request_id':h['X-Request-Id'],'body':parsed};records.append(record)
 passed=status in expected and (predicate(parsed) if predicate else True);checks.append({'case':name,'passed':bool(passed),'status':status,'expected':expected});return record
state=json.loads((B/'private/clients.json').read_text())
call('protocol-account-A-login',path='/api/auth/login',data=state['credentials']['A'])
for name,data in [('empty-object',{}),('null-json',None),('array-json',[]),('scalar-json','invalid'),('nonstring-message',{'message':5}),('empty-message',{'message':''}),('wrong-context',{'message':'Hello','context':[]})]:
 call(name,data=data,raw=b'null' if name=='null-json' else None,expected=(200,400) if name=='wrong-context' else (400,))
call('malformed-json',raw=b'{',expected=(400,))
call('oversized-body',raw=json.dumps({'message':'x'*300000}).encode(),expected=(413,))
call('wrong-content-type',raw=b'{"message":"Hello"}',headers={'Content-Type':'text/plain'},expected=(400,415))
call('map-no-address',path='/api/generate_map',data={},expected=(400,))
call('map-bad-coordinates',path='/api/generate_map',data={'address':'Bloomsbury London','geo_location':'invalid'},expected=(400,))
call('map-synthetic-positive',path='/api/generate_map',data={'address':'Bloomsbury London','geo_location':'51.523,-0.127','price':'£1800/month'},predicate=lambda x:isinstance(x,str) and 'leaflet' in x.lower())
malicious='<img src=x onerror="window.__auditXSS=1">'
call('map-address-escape',path='/api/generate_map',data={'address':malicious,'geo_location':'51.523,-0.127','price':malicious},predicate=lambda x:isinstance(x,str) and malicious not in x and ('&lt;img' in x or '&amp;lt;img' in x or '\\u0026lt;img' in x))
# Authenticated identities have independent rate-limit buckets.
jar.clear()
call('protocol-account-B-login',path='/api/auth/login',data=state['credentials']['B'])
reqid='protocol-replay-'+uuid.uuid4().hex
first=call('json-hello',data={'message':'Hello'},headers={'X-Request-Id':reqid})
cid=first['body'].get('conversation_id') if isinstance(first['body'],dict) else None
call('json-idempotent-replay',data={'message':'Hello','conversation_id':cid},headers={'X-Request-Id':reqid},predicate=lambda x: {k:v for k,v in x.items() if k!='replayed'}==first['body'])
sse=call('sse-idempotent-replay',data={'message':'Hello','conversation_id':cid},headers={'X-Request-Id':reqid,'Accept':'text/event-stream'},predicate=lambda x:isinstance(x,dict) and {k:v for k,v in x.items() if k!='replayed'}==first['body'] or isinstance(x,str) and x.count('event: final')==1)
call('sse-fresh-hello',data={'message':'Hi','conversation_id':cid},headers={'Accept':'text/event-stream'},predicate=lambda x:isinstance(x,str) and x.count('event: final')==1 and 'payload' in x)
call('messages-durable-once',path=f'/api/conversations/{cid}/messages',method='GET',predicate=lambda x:isinstance(x,dict) and len(x.get('messages',[]))>=2)
for n in range(3):call('refresh-load-'+str(n),path=f'/api/conversations/{cid}/messages',method='GET')
# Exercise the actual configured authenticated-user limiter, bounded to twelve cheap greetings.
for n in range(12):
 r=call('bounded-rate-limit-'+str(n),data={'message':'Hello'},expected=(200,429))
 if r['status']==429:
  checks.append({'case':'rate-limit-retry-after','passed':bool(r['retry_after']),'status':429});break
(B/'private/protocol-cookie.txt').write_text('; '.join(c.name+'='+c.value for c in jar));(B/'private/protocol-cookie.txt').chmod(0o600)
(B/'evidence/protocol-requests.json').write_text(json.dumps(records,ensure_ascii=False,indent=2));(B/'evidence/protocol-checks.json').write_text(json.dumps(checks,indent=2))
summary={'checks':len(checks),'passed':sum(c['passed'] for c in checks),'failed':[c for c in checks if not c['passed']]};(B/'evidence/protocol-summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
