"""Public black-box browser helper; no implementation internals or expected answers."""
import pathlib,json,time,os,sys,uuid
os.environ['LD_LIBRARY_PATH']='/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu'+(':'+os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:15009'
class AuditBrowser:
    def __init__(self,folder,profile='primary',mobile=False):
        self.folder=pathlib.Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.profile=profile
        self.state=self.folder/(profile+'-storage.json');self.log=self.folder/(profile+'-turns.jsonl')
        self.p=sync_playwright().start()
        self.browser=self.p.chromium.launch(executable_path='/tmp/rentcompass-live-20261019/browsers/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        opts={'viewport':{'width':390,'height':844} if mobile else {'width':1440,'height':960}}
        if self.state.exists():opts['storage_state']=str(self.state)
        self.context=self.browser.new_context(**opts);self.external=[];self.errors=[]
        def route(r):
            if r.request.url.startswith(URL+'/') or r.request.url.startswith('data:'):r.continue_()
            else:
                self.external.append(r.request.url)
                if r.request.resource_type=='stylesheet':r.fulfill(status=200,body='',content_type='text/css')
                else:r.abort('blockedbyclient')
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(URL,wait_until='domcontentloaded');self.page.locator('#chatInput').wait_for(state='visible',timeout=30000)
        self.install_observer()
    def install_observer(self):
        self.page.evaluate('''() => {
            if(window.__auditInstalled)return;
            window.__auditInstalled=true;window.__auditResults=[];window.__auditStages=[];window.__auditRequests=[];
            const oldFetch=window.fetch;
            window.fetch=async(...args)=>{
                if(String(args[0]).includes('/api/alex'))window.__auditRequests.push({url:String(args[0]),body:args[1]?.body,headers:args[1]?.headers});
                return oldFetch(...args);
            };
            const original=readChatResponse;
            readChatResponse=async(...args)=>{const r=await original(...args);window.__auditResults.push(r);return r;};
            new MutationObserver(()=>{const l=document.querySelector('.progress-label');if(l)window.__auditStages.push({t:Date.now(),text:l.textContent});}).observe(document.querySelector('#chatMessages'),{subtree:true,childList:true,characterData:true});
        }''')
    def send(self,message,timeout=180000):
        self.install_observer();before=self.page.evaluate('window.__auditResults.length');t=time.time();error=None
        self.page.locator('#chatInput').fill(message);self.page.locator('#sendBtn').click()
        try:self.page.wait_for_function(f'window.__auditResults.length>{before}',timeout=timeout)
        except Exception as e:error=str(e)
        self.page.wait_for_timeout(250)
        r={'time':time.time(),'message':message,'seconds':round(time.time()-t,3),'error':error,
           'result':self.page.evaluate(f'window.__auditResults[{before}]||null'),
           'request':self.page.evaluate('window.__auditRequests.at(-1)||null'),
           'dom':self.page.locator('#chatMessages').inner_text(),
           'criteria':self.page.locator('body').inner_text()[:12000],
           'stages':self.page.evaluate('window.__auditStages')}
        with self.log.open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
        return r
    def api(self,path,method='GET',data=None):
        assert path.startswith('/api/')
        response=self.context.request.fetch(URL+path,method=method,data=data,timeout=180000)
        try:body=response.json()
        except Exception:body=response.text()
        return {'status':response.status,'body':body}
    def screenshot(self,name):self.page.screenshot(path=str(self.folder/(name+'.png')),full_page=True)
    def close(self):
        self.context.storage_state(path=str(self.state))
        with (self.folder/(self.profile+'-browser-metadata.jsonl')).open('a') as f:f.write(json.dumps({'time':time.time(),'page_errors':self.errors,'external_requests_blocked':self.external},ensure_ascii=False)+'\n')
        self.context.close();self.browser.close();self.p.stop()
