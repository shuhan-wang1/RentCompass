"""Read-only same-visitor recovery probe; safe before/after service restart."""
import argparse,hashlib,json,pathlib,sys,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
ROOT=pathlib.Path(__file__).parent
p=argparse.ArgumentParser();p.add_argument('--label',default='after-restart');a=p.parse_args()
if not all(c.isalnum() or c in '-_' for c in a.label):p.error('Simple artifact label required')
b=AuditBrowser(ROOT,profile='professional')
try:
    b.page.get_by_role('button',name='EN',exact=True).click();b.page.wait_for_timeout(500)
    chat=b.page.locator('#chatMessages').inner_text()
    e={'time':time.time(),'label':a.label,'url':b.page.url,'chat':chat,'chat_sha256':hashlib.sha256(chat.encode()).hexdigest(),
      'conversations':b.page.locator('.conv-item-main').all_inner_texts(),
      'selected_conversation':b.page.locator('.conv-item.active').all_inner_texts(),
      'filters':b.page.evaluate('''() => [...document.querySelectorAll('.criteria-input,.criteria-select,input[type=checkbox]')].map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,value:e.value,checked:e.checked,disabled:e.disabled}))'''),
      'saved_count':b.page.locator('#btn-favorites').inner_text(),
      'body':b.page.locator('body').inner_text()}
    b.screenshot('recovery-'+a.label+'-desktop')
    b.page.locator('#btn-favorites').click();b.page.wait_for_timeout(300)
    e['saved_cards']=b.page.locator('.property-card').all_inner_texts()
    b.screenshot('recovery-'+a.label+'-saved')
    b.page.locator('#btn-results').click()
    b.page.set_viewport_size({'width':390,'height':844});b.page.wait_for_timeout(250)
    b.screenshot('recovery-'+a.label+'-mobile')
    e['mobile_dimensions']=b.page.evaluate('''() => ({innerWidth,scrollWidth:document.documentElement.scrollWidth,innerHeight,scrollHeight:document.documentElement.scrollHeight})''')
    e['chat_requests']=b.page.evaluate('window.__auditRequests');assert not e['chat_requests']
    ROOT.joinpath('recovery-'+a.label+'.json').write_text(json.dumps(e,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in e.items() if k not in ['chat','body','filters']},ensure_ascii=False))
finally:b.close()
