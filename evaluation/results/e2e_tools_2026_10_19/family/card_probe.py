import sys,json,pathlib
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family');b=AuditBrowser(folder,profile='family',mobile=True);p=b.page
r={'restored':p.locator('body').inner_text()[-6000:],'buttons':p.locator('button,a').evaluate_all('(els)=>els.filter(e=>/Ask AI|Map|View Deal/.test(e.innerText)).slice(0,6).map(e=>({tag:e.tagName,text:e.innerText,html:e.outerHTML}))')}
print(json.dumps(r,ensure_ascii=False));(folder/'ui-cards.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));b.close()
