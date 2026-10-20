"""Test transport log. OTM is paused after the one real 403 preflight."""
import pathlib,json,time,threading,urllib.parse,socket,requests
B=pathlib.Path('/tmp/rentcompass-live-20261019');lock=threading.Lock();counts={};last={};paused={};original=requests.Session.send;dns=socket.getaddrinfo

def emit(name,value):
 with lock:
  with (B/'evidence'/name).open('a') as f:f.write(json.dumps(value,ensure_ascii=False,default=str)+'\n')
def safe_url(url):
 p=urllib.parse.urlsplit(url);query=urllib.parse.parse_qsl(p.query,keep_blank_values=True)
 return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,urllib.parse.urlencode([(k,'[REDACTED]' if any(s in k.lower() for s in ['key','token','secret','password']) else v) for k,v in query]),''))
def send(self,request,**kwargs):
 u=urllib.parse.urlsplit(request.url);host=(u.hostname or '').lower();t=time.time();record={'started':t,'host':host,'method':request.method,'url':safe_url(request.url)}
 if host=='onthemarket.com' or host.endswith('.onthemarket.com'):
  # Reuse the actual failure body to test failure handling without hitting the
  # blocked site again. This is explicitly a REPLAY, not a live listing response.
  r=requests.Response();r.status_code=403;r._content=(B/'evidence/otm-preflight-response.html').read_bytes();r.url=request.url;r.request=request;r.headers['Content-Type']='text/html'
  record.update(status=403,transport='captured_preflight_403_replay',actual_external_request=False);emit('http-requests.jsonl',record);return r
 if host in ['127.0.0.1','localhost','::1']:
  return original(self,request,**kwargs)
 with lock:
  count=counts.get(host,0);reason=paused.get(host)
  if reason or count>=12:blocked=True
  else:
   blocked=False;wait=max(0,1.2-(time.monotonic()-last.get(host,0)));time.sleep(wait);last[host]=time.monotonic();counts[host]=count+1
 if blocked:
  record.update(transport='local_host_budget_stop',actual_external_request=False,reason=reason or '12-request provider budget');emit('http-requests.jsonl',record);raise requests.ConnectionError('Formal test provider request budget/circuit is closed')
 try:
  r=original(self,request,**kwargs);record.update(status=r.status_code,seconds=round(time.time()-t,3),transport='live',actual_external_request=True)
  if r.status_code in [403,429]:
   with lock:paused[host]='HTTP '+str(r.status_code)
  return r
 except Exception as e:record.update(error_type=type(e).__name__,transport='live',actual_external_request=True);raise
 finally:emit('http-requests.jsonl',record)
def guarded_dns(host,*args,**kwargs):
 if host and str(host).lower().rstrip('.').endswith('onthemarket.com'):raise socket.gaierror('OTM live requests paused after CloudFront 403')
 return dns(host,*args,**kwargs)
def install():requests.Session.send=send;socket.getaddrinfo=guarded_dns
