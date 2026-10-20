"""Inspect visible local property details and gallery; do not follow View Deal."""
import json,pathlib,sys,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
ROOT=pathlib.Path(__file__).parent
b=AuditBrowser(ROOT,profile='professional')
e={'time':time.time()}
try:
    b.page.get_by_role('button',name='EN',exact=True).click()
    b.page.locator('#btn-favorites').click();b.page.wait_for_timeout(300)
    e['saved_body']=b.page.locator('body').inner_text()
    cards=b.page.locator('.property-card');e['saved_card_count']=cards.count()
    if cards.count():
        card=cards.first;e['card']=card.inner_text();e['markup']=card.evaluate('(e)=>e.outerHTML')
        e['source_links']=card.locator('a').evaluate_all('(es)=>es.map(e=>({text:e.innerText,href:e.href,target:e.target}))')
        b.screenshot('saved-property-details')
        imgs=card.locator('img')
        if imgs.count():
            e['image_visible']=imgs.first.is_visible()
            imgs.first.locator('..').click(timeout=5000);b.page.wait_for_timeout(400)
            e['after_image_click']=b.page.locator('body').inner_text()
            e['visible_modals']=b.page.locator('[role=dialog],.modal').evaluate_all('(es)=>es.filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)).map(e=>({cls:e.className,text:e.innerText,role:e.getAttribute("role"),label:e.getAttribute("aria-label")}))')
            b.screenshot('property-gallery')
            b.page.keyboard.press('Escape');b.page.wait_for_timeout(300)
    e['chat_requests']=b.page.evaluate('window.__auditRequests');assert not e['chat_requests']
    ROOT.joinpath('ui-detail.json').write_text(json.dumps(e,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in e.items() if k not in ['markup','saved_body','after_image_click']},ensure_ascii=False))
finally:b.close()
