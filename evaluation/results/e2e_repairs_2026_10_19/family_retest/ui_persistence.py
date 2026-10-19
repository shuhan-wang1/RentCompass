import sys,json,pathlib
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');b=AuditBrowser(f,profile='family_retest',mobile=True);p=b.page;r={}
try:
 p.wait_for_timeout(600);p.wait_for_function('!document.querySelector("#chatInput").disabled')
 listing_url=p.get_by_role('link',name='View Deal',exact=True).first.get_attribute('href');r['selected_url']=listing_url
 initial=p.locator('#favCount').inner_text();p.locator('.favorite-btn').first.click();p.wait_for_function('(old)=>document.querySelector("#favCount").innerText!==old',arg=initial);r['saved_count']=p.locator('#favCount').inner_text()
 p.get_by_role('button',name='Ask AI',exact=True).first.click();r['after_ask_focus']=p.locator('#propertyFocusIndicator').inner_text();r['chat_requests_after_ask']=p.evaluate('window.__auditRequests');b.screenshot('ui-03-camden-selected-saved')
 p.reload(wait_until='domcontentloaded');p.locator('#chatInput').wait_for(state='visible');p.wait_for_timeout(700);p.wait_for_function('!document.querySelector("#chatInput").disabled');b.install_observer();r['refresh_focus']=p.locator('#propertyFocusIndicator').inner_text();r['refresh_saved_count']=p.locator('#favCount').inner_text()
 listing=b.api('/api/conversations');r['aux_list_status']=listing['status'];convs=listing['body']['conversations'];old=next(x for x in convs if x['id']=='83b3c2a05bac40949776659286bdc6ee');r['original_conversation_id']=old['id']
 p.locator('#menuToggle').click()
 with p.expect_response(lambda response: response.request.method=='POST' and response.url.split('?')[0].endswith('/api/conversations')) as new_response:
  p.locator('#newChatBtn').click()
 payload=new_response.value.json();new=(payload.get('conversation') or payload);r['new_conversation_id']=new.get('id');p.wait_for_timeout(300)
 r['new_chat_focus']=p.locator('#propertyFocusIndicator').inner_text();r['new_chat_forms']=p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({type:e.type,value:e.value,checked:e.checked}))');b.screenshot('ui-04-new-chat-isolated')
 p.locator('#menuToggle').click();p.locator('#convList').get_by_text(old['title'],exact=True).click();p.wait_for_function('!document.querySelector("#chatInput").disabled');p.wait_for_timeout(300);r['returned_focus']=p.locator('#propertyFocusIndicator').inner_text();r['returned_forms']=p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({type:e.type,value:e.value,checked:e.checked}))')
 p.locator('#btn-favorites').click();p.wait_for_timeout(300);r['saved_tab_body']=p.locator('#resultsContent').inner_text();r['saved_tab_urls']=p.get_by_role('link',name='View Deal',exact=True).evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))');b.screenshot('ui-05-saved-after-switch')
 p.locator('#btn-results').click();p.wait_for_timeout(200);r['final_chat_requests']=p.evaluate('window.__auditRequests');r['final_focus']=p.locator('#propertyFocusIndicator').inner_text();b.screenshot('ui-06-ready-for-restart')
 (f/'ui-persistence.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False))
finally:b.close()
