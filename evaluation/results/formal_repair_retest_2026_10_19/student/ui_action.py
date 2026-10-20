import asyncio,json,sys,time,re
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
from playwright.async_api import async_playwright
ROOT=Path('/tmp/rentcompass-fix-live-20261019/student');ORIGIN='http://127.0.0.1:15019'
STORAGE=ROOT/'student-storage.json';LEDGER=ROOT/'chat-requests.jsonl'
def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def ledger():return [json.loads(s) for s in LEDGER.read_text().splitlines()] if LEDGER.exists() else []
async def main():
 mode,label=sys.argv[1:3]
 network=[];blocked=[]
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
  ctx=await browser.new_context(storage_state=str(STORAGE) if STORAGE.exists() else None,viewport={'width':1440,'height':1100},accept_downloads=True)
  async def route(r):
   u=urlsplit(r.request.url)
   if f'{u.scheme}://{u.netloc}'!=ORIGIN:
    blocked.append(r.request.url);await r.abort();return
   if u.path=='/api/alex':
    if mode!='chat':
     blocked.append('MODEL_REQUEST_BLOCKED:'+r.request.url);await r.abort();return
    if len(ledger())>=7:await r.abort();raise RuntimeError('seven-chat budget exhausted')
    req={'time':time.time(),'label':label,'method':r.request.method,'url':r.request.url,'body':r.request.post_data_json}
    with LEDGER.open('a',encoding='utf-8') as f:f.write(json.dumps(req,ensure_ascii=False)+'\n')
    dump(ROOT/f'{label}-request.json',req)
   await r.continue_()
  await ctx.route('**/*',route)
  await ctx.add_init_script("""(() => { const original=window.fetch.bind(window); window.__rentAudit={}; window.fetch=async (...args) => { const response=await original(...args); const url=typeof args[0]==='string'?args[0]:(args[0]?.url||''); if(url.includes('/api/alex')) {window.__rentAudit={status:response.status,started:Date.now()}; response.clone().text().then(raw=>{window.__rentAudit.raw=raw;window.__rentAudit.done=true}).catch(error=>{window.__rentAudit.error=String(error);window.__rentAudit.done=true});} return response; }; })();""")
  page=await ctx.new_page()
  async def capture(resp):
   if resp.request.resource_type in ['fetch','xhr'] and urlsplit(resp.url).path!='/api/alex':
    rec={'path':urlsplit(resp.url).path,'method':resp.request.method,'status':resp.status}
    if resp.request.post_data and 'auth' not in rec['path'] and 'login' not in rec['path'] and 'register' not in rec['path']:rec['request_body']=resp.request.post_data
    try:
     if 'json' in resp.headers.get('content-type',''):rec['response']=await resp.json()
    except Exception as e:rec['read_error']=str(e)
    network.append(rec)
  page.on('response',capture)
  await page.goto(ORIGIN,wait_until='domcontentloaded');await page.wait_for_timeout(1300)
  if mode=='chat':
   message=Path(sys.argv[3]).read_text()
   if label=='07':
    await page.locator('#newChatBtn').click();await page.wait_for_timeout(400)
    (ROOT/'07-before-message-dom.txt').write_text(await page.locator('body').inner_text(),encoding='utf-8')
    await page.screenshot(path=str(ROOT/'07-before-message.png'),full_page=True)
   await page.get_by_role('button',name='中文',exact=True).click()
   await page.locator('#chatInput').fill(message)
   async with page.expect_response(lambda r:urlsplit(r.url).path=='/api/alex',timeout=240000) as pending:
    await page.locator('#sendBtn').click()
   resp=await pending.value
   await page.wait_for_function('window.__rentAudit && window.__rentAudit.done',timeout=240000)
   audit=await page.evaluate('window.__rentAudit')
   if 'raw' not in audit:raise RuntimeError(audit)
   raw=audit['raw']
   (ROOT/f'{label}-response.sse').write_text(raw,encoding='utf-8')
   events=[]
   for part in raw.replace('\r\n','\n').split('\n\n'):
    name='message';data=[]
    for line in part.splitlines():
     if line.startswith('event:'):name=line[6:].strip()
     elif line.startswith('data:'):data.append(line[5:].strip())
    if data:
     try:value=json.loads('\n'.join(data))
     except Exception:value='\n'.join(data)
     events.append({'event':name,'data':value})
   dump(ROOT/f'{label}-events.json',events)
   final=next((x['data'] for x in reversed(events) if x['event']=='final'),None)
   dump(ROOT/f'{label}-response.json',{'status':resp.status,'final':final})
   await page.wait_for_timeout(800)
   if final:
    payload=final.get('payload',final)
    print(json.dumps({'label':label,'status':resp.status,'conversation_id':payload.get('conversation_id'),'turn_id':payload.get('turn_id'),'message':payload.get('message'),'criteria':{k:v for k,v in (payload.get('view_state',{}).get('criteria',payload.get('search_criteria')) or {}).items() if k not in ['constraint_sources','plans','criteria_repair_provenance']},'recommendations':[{k:x.get(k) for k in ['address','price','room_type','bedrooms','verified_commute_minutes','candidate_status','candidate_reasons','candidate_unknown_reasons']} for x in payload.get('recommendations',[])],'view_results':[{k:x.get(k) for k in ['address','price','room_type','bedrooms','verified_commute_minutes','candidate_status','candidate_reasons','candidate_unknown_reasons']} for x in payload.get('view_state',{}).get('results',[])],'stages':[x['data'] for x in events if x['event']!='final']},ensure_ascii=False,indent=2))
   else:print('NO_FINAL',raw[:2500])
  elif mode=='new':
   await page.locator('#newChatBtn').click();await page.wait_for_timeout(800)
  elif mode=='favorite':
   print('BUTTONS',await page.locator('button').evaluate_all('(es)=>es.filter(e=>e.offsetParent!==null).map(e=>({text:e.innerText,title:e.title,aria:e.getAttribute("aria-label"),id:e.id,cls:e.className}))'))
  elif mode=='saved':
   await page.locator('#btn-favorites').click();await page.wait_for_timeout(600)
  elif mode=='reload':
   await page.reload(wait_until='domcontentloaded');await page.wait_for_timeout(1000)
  elif mode=='click':
   await page.locator(sys.argv[3]).click();await page.wait_for_timeout(800)
  elif mode=='inspect':pass
  else:raise RuntimeError(mode)
  body=await page.locator('body').inner_text()
  (ROOT/f'{label}-dom.txt').write_text(body,encoding='utf-8')
  await page.screenshot(path=str(ROOT/f'{label}.png'),full_page=True)
  state={'time':time.time(),'url':page.url,'forms':await page.locator('input,textarea,select').evaluate_all('(es)=>es.filter(e=>e.type!=="password").map(e=>({tag:e.tagName,type:e.type,aria:e.getAttribute("aria-label"),id:e.id,value:e.value,checked:e.checked}))'),'chat_requests':len(ledger())}
  dump(ROOT/f'{label}-state.json',state);dump(ROOT/f'{label}-local-network.json',network);dump(ROOT/f'{label}-blocked-external.json',blocked)
  await ctx.storage_state(path=str(STORAGE))
  if mode!='chat':print(body[:14000]);print('STATE',json.dumps(state,ensure_ascii=False))
  print('CHAT_REQUESTS_TOTAL',len(ledger()))
  await browser.close()
asyncio.run(main())
