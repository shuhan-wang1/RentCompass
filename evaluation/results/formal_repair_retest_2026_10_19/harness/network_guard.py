"""Audit-only shared request budgets; every allowed response comes from the real provider."""
from pathlib import Path
import json,time,threading,urllib.parse,sqlite3,requests
B=Path('/tmp/rentcompass-fix-live-20261019');lock=threading.Lock();original=requests.Session.send
DB=B/'runtime/http-budget.sqlite3'
def emit(name,value):
 with lock:
  with (B/'evidence'/name).open('a') as f:f.write(json.dumps(value,ensure_ascii=False,default=str)+'\n')
def safe_url(url):
 p=urllib.parse.urlsplit(url)
 pairs=[(k,'[REDACTED]' if any(s in k.lower() for s in ('key','token','secret','password')) else v) for k,v in urllib.parse.parse_qsl(p.query,keep_blank_values=True)]
 return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,urllib.parse.urlencode(pairs),''))
def claim(host):
 with sqlite3.connect(DB,timeout=10) as db:
  db.execute('CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY,n INT,next REAL,paused TEXT)')
  db.execute('INSERT OR IGNORE INTO hosts VALUES(?,0,0,?)',(host,''));db.commit();db.execute('BEGIN IMMEDIATE')
  n,next_at,paused=db.execute('SELECT n,next,paused FROM hosts WHERE host=?',(host,)).fetchone()
  cap=12 if host=='www.onthemarket.com' else (120 if host in ('nominatim.openstreetmap.org','api.tfl.gov.uk') else 60)
  if paused or n>=cap:raise requests.ConnectionError('Local audit provider budget/circuit stopped: '+(paused or 'request cap'))
  start=max(time.time(),next_at);db.execute('UPDATE hosts SET n=n+1,next=? WHERE host=?',(start+1.2,host))
 return max(0,start-time.time())
def send(self,request,**kwargs):
 u=urllib.parse.urlsplit(request.url);host=(u.hostname or '').lower()
 if host in ('127.0.0.1','localhost','::1'):return original(self,request,**kwargs)
 record={'started':time.time(),'host':host,'method':request.method,'url':safe_url(request.url)}
 try: delay=claim(host)
 except requests.ConnectionError:
  record.update(transport='local_audit_budget_stop',actual_external_request=False);emit('http-requests.jsonl',record);raise
 if delay:time.sleep(delay)
 try:
  response=original(self,request,**kwargs);record.update(status=response.status_code,transport='live',actual_external_request=True)
  if host=='www.onthemarket.com':record['program_user_agent']=request.headers.get('User-Agent')
  if response.status_code in (403,429):
   with sqlite3.connect(DB) as db:db.execute('UPDATE hosts SET paused=? WHERE host=?',('HTTP '+str(response.status_code),host))
  return response
 except Exception as exc:
  record.update(error_type=type(exc).__name__,transport='live',actual_external_request=True);raise
 finally:
  record['seconds']=round(time.time()-record['started'],3);emit('http-requests.jsonl',record)
def install():requests.Session.send=send
