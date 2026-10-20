import sys,json,pathlib,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family');b=AuditBrowser(f,profile='family',mobile=True);p=b.page
try:
 p.get_by_text("I've reopened this conversation.",exact=False).first.wait_for(state='attached',timeout=30000)
 p.wait_for_timeout(500)
 r={'body':p.locator('body').inner_text(),'forms':p.locator('input,select').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,type:e.type,value:e.value,checked:e.checked,placeholder:e.placeholder,labels:e.labels?[...e.labels].map(x=>x.innerText):[]}))'),'chat_requests':p.evaluate('window.__auditRequests'),'chat_count':p.locator('#chatMessages').inner_text().count('Edit message'),'dimensions':p.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})')}
 b.screenshot('15-restart-restored-settled');(f/'ui-after-restart.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in r.items() if k!='body'},ensure_ascii=False))
finally:b.close()
