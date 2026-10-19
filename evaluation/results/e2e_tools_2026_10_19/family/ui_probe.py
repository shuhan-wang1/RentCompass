import sys,json,pathlib
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family')
b=AuditBrowser(folder,profile='family',mobile=True)
p=b.page
r={'body':p.locator('body').inner_text(),'url':p.url,'dimensions':p.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,height:innerHeight})'),'controls':p.locator('input,button,select,a,textarea').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,id:e.id,type:e.type,role:e.getAttribute("role"),text:e.innerText,placeholder:e.getAttribute("placeholder"),aria:e.getAttribute("aria-label"),label:e.labels?[...e.labels].map(x=>x.innerText):[],box:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}}))')}
r['keyboard']=[]
for i in range(28):
 p.keyboard.press('Tab')
 r['keyboard'].append(p.evaluate('(()=>{const e=document.activeElement,r=e.getBoundingClientRect();return {tag:e.tagName,id:e.id,text:e.innerText,aria:e.getAttribute("aria-label"),focusVisible:e.matches(":focus-visible"),outline:getComputedStyle(e).outline,box:{x:r.x,y:r.y,width:r.width,height:r.height},scrollY:scrollY}})()'))
b.screenshot('01-mobile-initial')
(folder/'ui-initial.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
print(json.dumps(r,ensure_ascii=False))
b.close()
