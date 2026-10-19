import os,sys,json,time,pathlib
sys.dont_write_bytecode=True
os.environ['PLAYWRIGHT_BROWSERS_PATH']='/tmp/rentcompass-live-20261019/browsers'
os.environ['LD_LIBRARY_PATH']='/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu'+(':'+os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse
FOLDER=pathlib.Path('/tmp/rentcompass-fix-live-20261019/family');URL='http://127.0.0.1:15019'
class Browser:
 def __init__(self):
  self.state=FOLDER/'family-live-storage.json';self.p=sync_playwright().start();self.browser=self.p.chromium.launch(headless=True,args=['--no-sandbox']);opts={'viewport':{'width':390,'height':844},'has_touch':True,'is_mobile':True}
  if self.state.exists():opts['storage_state']=str(self.state)
  self.context=self.browser.new_context(**opts);self.blocked=[];self.errors=[];self.requests=[];self.responses=[]
  def route(r):
   parsed=urlparse(r.request.url)
   if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port==15019:r.continue_()
   else:self.blocked.append({'url':r.request.url,'type':r.request.resource_type});r.abort('blockedbyclient')
  self.context.route('**/*',route);self.page=self.context.new_page();self.page.on('pageerror',lambda e:self.errors.append(str(e)))
  self.page.on('request',lambda r:self.requests.append({'url':r.url,'body':r.post_data_json}) if '/api/alex' in r.url and r.method=='POST' else None)
  self.page.on('response',lambda r:self.responses.append(r) if '/api/alex' in r.url and r.request.method=='POST' else None)
  self.page.goto(URL,wait_until='domcontentloaded');self.page.locator('#chatInput').wait_for(state='visible');self.page.wait_for_timeout(700);self.page.wait_for_function('!document.querySelector("#chatInput").disabled')
  self.page.evaluate('''() => { window.__familyAuditRaw=[]; const original=window.fetch; window.fetch=async(...args)=>{const response=await original(...args); if(String(args[0]).includes('/api/alex')) {const copy=response.clone(); copy.text().then(raw=>window.__familyAuditRaw.push({raw,status:response.status,content_type:response.headers.get('content-type'),request_id:response.headers.get('x-request-id')})).catch(error=>window.__familyAuditRaw.push({error:String(error)}));} return response;};}''')
 def ui(self):
  p=self.page
  return {'body':p.locator('body').inner_text(),'chat':p.locator('#chatMessages').inner_text(),'criteria':p.locator('#criteriaBody').inner_text(),'forms':p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({type:e.type,value:e.value,checked:e.checked,aria:e.getAttribute("aria-label")}))'),'card_count':p.locator('#resultsContent .property-card').count(),'cards':p.locator('#resultsContent').inner_text(),'urls':p.locator('#resultsContent a').evaluate_all('(els)=>els.map(e=>({text:e.innerText,href:e.getAttribute("href")}))'),'focus':p.locator('#propertyFocusIndicator').inner_text(),'favorite_count':p.locator('#favCount').inner_text(),'viewport':p.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})')}
 def screenshot(self,name):self.page.screenshot(path=str(FOLDER/(name+'.png')),full_page=True)
 def send(self,text,name):
  before=self.ui();start=time.time();count=len(self.requests)
  with self.page.expect_response(lambda r:'/api/alex' in r.url and r.request.method=='POST',timeout=240000) as pending:
   self.page.locator('#chatInput').fill(text);self.page.locator('#sendBtn').click()
  response=pending.value;self.page.wait_for_function('window.__familyAuditRaw.length>0',timeout=240000);captured=self.page.evaluate('window.__familyAuditRaw.at(-1)');raw=captured.get('raw','');content_type=captured.get('content_type') or response.headers.get('content-type','');final=None;frames=[]
  if 'text/event-stream' in content_type:
   for frame in raw.replace('\r\n','\n').split('\n\n'):
    event='message';data=[]
    for line in frame.split('\n'):
     if line.startswith('event:'):event=line[6:].strip()
     elif line.startswith('data:'):data.append(line[5:].lstrip())
    if not data:continue
    try:item=json.loads('\n'.join(data))
    except Exception:item={'unparsed':'\n'.join(data)}
    frames.append({'event':event,'data':item})
    if event=='final':final=item
  else:
   try:final={'status':response.status,'payload':json.loads(raw)}
   except Exception:final={'status':response.status,'payload':{'unparsed':raw}}
  self.page.wait_for_timeout(350);self.page.wait_for_function('!document.querySelector("#sendBtn").classList.contains("is-stop")',timeout=30000)
  result={'name':name,'time':time.time(),'seconds':round(time.time()-start,3),'message':text,'network_request_count':len(self.requests)-count,'request':self.requests[-1],'response_status':response.status,'response_ids':{k:response.headers.get(k) for k in ['x-request-id','x-correlation-id'] if response.headers.get(k)},'content_type':content_type,'frames':frames,'result':final,'before':before,'after':self.ui()}
  (FOLDER/(name+'-raw-response.txt')).write_text(raw);(FOLDER/(name+'-response.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));self.screenshot(name)
  with (FOLDER/'turns.jsonl').open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
  return result
 def close(self):
  self.context.storage_state(path=str(self.state));self.state.chmod(0o600)
  with (FOLDER/'browser-metadata.jsonl').open('a') as f:f.write(json.dumps({'time':time.time(),'page_errors':self.errors,'blocked_external':self.blocked,'chat_request_count':len(self.requests)},ensure_ascii=False)+'\n')
  self.context.close();self.browser.close();self.p.stop()
