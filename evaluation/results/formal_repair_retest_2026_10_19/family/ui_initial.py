from browser import Browser,FOLDER
import json
b=Browser();p=b.page
try:
 r={'initial':b.ui(),'closed_tabs':[]}
 for i in range(20):
  p.keyboard.press('Tab');r['closed_tabs'].append(p.evaluate('({id:document.activeElement.id,inSidebar:!!document.activeElement.closest("#convSidebar")})'))
 p.locator('#menuToggle').tap();r['touch_open']={'expanded':p.locator('#menuToggle').get_attribute('aria-expanded'),'focus':p.evaluate('document.activeElement.id')};r['open_tabs']=[]
 for i in range(12):
  p.keyboard.press('Tab');r['open_tabs'].append(p.evaluate('({id:document.activeElement.id,inSidebar:!!document.activeElement.closest("#convSidebar")})'))
 b.screenshot('ui-01-touch-menu');p.keyboard.press('Escape');r['escape']={'expanded':p.locator('#menuToggle').get_attribute('aria-expanded'),'focus':p.evaluate('document.activeElement.id')};r['requests']=b.requests
 (FOLDER/'ui-initial.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps({'touch_open':r['touch_open'],'escape':r['escape'],'closed_sidebar_tab_stops':[x for x in r['closed_tabs'] if x['inSidebar']],'open_tabs':r['open_tabs'],'chat_requests':len(b.requests),'viewport':r['initial']['viewport']},ensure_ascii=False))
finally:b.close()
