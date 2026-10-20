import sys,json,pathlib
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import sync_playwright
from urllib.parse import urlparse
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');html=pathlib.Path('/home/shuhan/uk_rent_recommendation/app/unified-ui.html').read_text();origin='http://family-reasons-offline.test';rec={'address':'Offline family flat','url':'https://fixtures.invalid/reason','price':'£2000/month','possibly_outdated':True,'qualification':{'status':'unknown','reasons':['machine_reason_total_cost_unknown'],'unknown_reasons':['required features are not verified: step-free'],'reason_messages':{'en':['Total monthly cost still needs verification.','Step-free access is unverified.'],'zh':['每月总费用仍需核实。','无台阶通行尚未核实。']}}};criteria={'area':'Camden','areas':['Camden'],'max_budget':3000}
def route(r):
 p=urlparse(r.request.url)
 if p.netloc!=urlparse(origin).netloc:r.abort();return
 if p.path=='/':r.fulfill(body=html,content_type='text/html');return
 if p.path=='/api/auth/me':d={'authenticated':False}
 elif p.path=='/api/conversations':d={'conversations':[{'id':'reason','title':'Reason verification'}]}
 elif p.path.endswith('/messages'):d={'messages':[],'view_state':{'operation':'replace','revision':1,'criteria':criteria,'results':[rec]}}
 elif p.path=='/api/favorites':d={'favorites':[]}
 else:d={}
 r.fulfill(body=json.dumps(d),content_type='application/json')
checks=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/tmp/carbonminer-qa-browsers/chromium-1248/chrome-linux64/chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage']);c=browser.new_context(viewport={'width':390,'height':844});c.route('**/*',route);page=c.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(origin);page.wait_for_function('mainPane && !mainPane.isLoading && mainPane.conversationId==="reason"')
 text=page.locator('.property-evidence').inner_text();assert 'Total monthly cost still needs verification.' in text and 'machine_reason' not in text;checks.append('English backend messages override machine reasons')
 page.locator('[data-lang=zh]').click();text=page.locator('.property-evidence').inner_text();assert '每月总费用仍需核实。' in text and '无台阶通行尚未核实。' in text and 'machine_reason' not in text and 'Total monthly' not in text;checks.append('Chinese backend messages replace every machine reason on language switch')
 page.locator('[data-lang=en]').click();assert 'Total monthly cost still needs verification.' in page.locator('.property-evidence').inner_text();checks.append('Switching back rerenders English messages')
 rec['qualification'].pop('reason_messages');rec['qualification']['reasons']=[];page.reload();page.wait_for_function('mainPane && !mainPane.isLoading && mainPane.conversationId==="reason"');page.locator('[data-lang=zh]').click();assert '以下必要条件尚未核实：无台阶通行' in page.locator('.property-evidence').inner_text();checks.append('Legacy raw reasons retain existing localization fallback')
 assert not errors;c.close();browser.close()
out={'checks':checks,'passed':len(checks),'page_errors':errors,'real_model_requests':0,'all_requests_intercepted':True};(f/'ui-reason-messages.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False))
