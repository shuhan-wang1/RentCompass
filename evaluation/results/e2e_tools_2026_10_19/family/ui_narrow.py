import sys,json,pathlib
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family')
b=AuditBrowser(folder,profile='family',mobile=True);p=b.page
r={'chat_attrs':p.locator('#chatMessages').evaluate('(e)=>Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]))'),'input_attrs':p.locator('#chatInput').evaluate('(e)=>Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]))')}
p.set_viewport_size({'width':320,'height':640});r['narrow']=p.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})');b.screenshot('05-narrow-320')
p.locator('#menuToggle').focus();p.keyboard.press('Enter');p.wait_for_timeout(200);r['menu_tab']=[]
for i in range(13):
 p.keyboard.press('Tab');r['menu_tab'].append(p.evaluate('(()=>{const e=document.activeElement,r=e.getBoundingClientRect();return {tag:e.tagName,id:e.id,text:e.innerText,box:{x:r.x,y:r.y,width:r.width,height:r.height}}})()'))
r['requests']=p.evaluate('window.__auditRequests')
(folder/'ui-narrow.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False));b.close()
