"""No model calls: saved cards, refresh, blank conversation and return, mobile focus."""
from browser import Browser,FOLDER,URL
import json,time
b=Browser();p=b.page
try:
 before=b.ui()
 conversations=b.context.request.get(URL+'/api/conversations').json()['conversations']
 cid='76e9351df6d1450582bf1576b6e2116f'
 title=next(c['title'] for c in conversations if c['id']==cid)
 p.locator('#btn-favorites').click();p.wait_for_timeout(300)
 saved={'body':p.locator('body').inner_text(),'cards':p.locator('.property-card:visible').evaluate_all('(els)=>els.map(e=>({text:e.innerText,parent:e.parentElement.id}))')}
 b.screenshot('ui-final-saved')
 p.locator('#btn-results').click()
 p.reload(wait_until='domcontentloaded');p.wait_for_function('!document.querySelector("#chatInput").disabled');p.wait_for_timeout(700)
 refreshed=b.ui();b.screenshot('ui-final-refresh')
 p.locator('#menuToggle').tap();p.locator('#newChatBtn').click();p.wait_for_timeout(350)
 blank=b.ui();b.screenshot('ui-final-blank-conversation')
 if p.locator('#menuToggle').get_attribute('aria-expanded')!='true':p.locator('#menuToggle').tap()
 p.locator('#convSidebar').get_by_text(title,exact=True).first.click();p.wait_for_timeout(700)
 if p.locator('#menuToggle').get_attribute('aria-expanded')=='true':p.keyboard.press('Escape')
 restored=b.ui()
 mobile={}
 for width in (390,320):
  p.set_viewport_size({'width':width,'height':844})
  p.locator('#menuToggle').tap();tabs=[]
  for i in range(12):
   p.keyboard.press('Tab');tabs.append(p.evaluate('({id:document.activeElement.id,inSidebar:!!document.activeElement.closest("#convSidebar")})'))
  p.keyboard.press('Escape')
  mobile[str(width)]={'tabs':tabs,'expanded':p.locator('#menuToggle').get_attribute('aria-expanded'),'focus':p.evaluate('document.activeElement.id'),'viewport':b.ui()['viewport']}
 p.set_viewport_size({'width':390,'height':844});final=b.ui();b.screenshot('ui-final-restored')
 assertions={
  'no_model_requests':len(b.requests)==0,
  'favorites_retained':before['favorite_count']=='2' and final['favorite_count']=='2',
  'refresh_form_restored':before['forms']==refreshed['forms'],
  'refresh_focus_restored':before['focus']==refreshed['focus'],
  'refresh_cards_restored':before['cards']==refreshed['cards'],
  'blank_has_no_focus':not blank['focus'],
  'blank_has_no_results':blank['card_count']==0,
  'return_form_restored':before['forms']==restored['forms'],
  'return_focus_restored':before['focus']==restored['focus'],
  'return_cards_restored':before['cards']==restored['cards'],
  'menu_focus_contained':all(x['inSidebar'] or x['id']=='menuToggle' for m in mobile.values() for x in m['tabs']),
  'escape_closes_returns_focus':all(m['expanded']=='false' and m['focus']=='menuToggle' for m in mobile.values()),
  'mobile_no_overflow':all(m['viewport']['width']==m['viewport']['scrollWidth'] for m in mobile.values()),
 }
 result={'time':time.time(),'before':before,'saved':saved,'refreshed':refreshed,'blank':blank,'restored':restored,'final':final,'mobile':mobile,'assertions':assertions,'model_requests':b.requests}
 (FOLDER/'ui-final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print(json.dumps({'assertions':assertions,'saved_cards':[x['text'] for x in saved['cards']]},ensure_ascii=False))
finally:b.close()
