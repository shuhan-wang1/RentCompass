import sys, os, json, time, pathlib, traceback
sys.path.insert(0, '/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import AuditBrowser, sync_playwright, URL

ROOT=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/professional_retest')

class StrictAuditBrowser(AuditBrowser):
    def __init__(self, folder, profile='primary'):
        self.folder=pathlib.Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.profile=profile
        self.state=self.folder/(profile+'-storage.json');self.log=self.folder/(profile+'-turns.jsonl')
        self.p=sync_playwright().start()
        self.browser=self.p.chromium.launch(executable_path='/tmp/carbonminer-qa-browsers/chromium-1248/chrome-linux64/chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        opts={'viewport':{'width':1440,'height':960}}
        if self.state.exists():opts['storage_state']=str(self.state)
        self.context=self.browser.new_context(**opts);self.external=[];self.errors=[]
        def route(r):
            if r.request.url.startswith(URL+'/') or r.request.url.startswith('data:'):r.continue_()
            else:self.external.append(r.request.url);r.abort('blockedbyclient')
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        def record_response(response):
            if response.url.startswith(URL+'/api/'):
                try:
                    item={'time':time.time(),'url':response.url,'status':response.status,'request_method':response.request.method,'request_body':response.request.post_data,'response_body':response.text()}
                    with (self.folder/'network.jsonl').open('a') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
                except Exception as e:
                    with (self.folder/'network-errors.jsonl').open('a') as f:f.write(json.dumps({'url':response.url,'error':str(e)})+'\n')
        self.page.on('response',record_response)
        self.page.goto(URL,wait_until='domcontentloaded');self.page.locator('#chatInput').wait_for(state='visible',timeout=30000)
        self.install_observer()

browser=StrictAuditBrowser(ROOT)
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
    try:
        c=json.loads(line);a=c['action']
        if a=='send':
            r=browser.send(c['message']);browser.screenshot(c['name']);payload=(r.get('result') or {}).get('payload') or {};print(json.dumps({'time':r['time'],'seconds':r['seconds'],'error':r['error'],'message':payload.get('message'),'search_criteria':payload.get('search_criteria',payload.get('view_state',{}).get('criteria'))},ensure_ascii=False),flush=True)
        elif a=='state':
            r={'time':time.time(),'url':browser.page.url,'title':browser.page.title(),'body':browser.page.locator('body').inner_text(),'localStorage':browser.page.evaluate('Object.fromEntries(Object.entries(localStorage))'),'errors':browser.errors,'external_requests_blocked':browser.external}
            if c.get('name'):
                (ROOT/(c['name']+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2));browser.screenshot(c['name'])
            print(json.dumps(r,ensure_ascii=False),flush=True)
        elif a=='eval':print(json.dumps(browser.page.evaluate(c['script']),ensure_ascii=False),flush=True)
        elif a=='api':
            r=browser.api(c['path'],c.get('method','GET'),c.get('data'))
            if c.get('name'):(ROOT/(c['name']+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2))
            print(json.dumps(r,ensure_ascii=False),flush=True)
        elif a=='reload':
            browser.page.reload(wait_until='domcontentloaded');browser.page.locator('#chatInput').wait_for(state='visible');browser.install_observer();browser.page.wait_for_timeout(700);browser.screenshot(c['name']);print(json.dumps({'reloaded':True}),flush=True)
        elif a=='screenshot':browser.screenshot(c['name']);print(json.dumps({'saved':c['name']}),flush=True)
        elif a=='close':browser.close();print(json.dumps({'closed':True}),flush=True);break
        else:raise ValueError(a)
    except Exception as e:print(json.dumps({'error':str(e),'traceback':traceback.format_exc()}),flush=True)
