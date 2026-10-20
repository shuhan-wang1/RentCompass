import sys,os,json,pathlib,argparse
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import sync_playwright
from urllib.parse import urlparse
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');args=argparse.ArgumentParser();args.add_argument('--before',action='store_true');args=args.parse_args()
html=pathlib.Path('/home/shuhan/uk_rent_recommendation/app/unified-ui.html').read_text();origin='http://family-revision-offline.test';events=[];mode={'fail_new':False}
criteria={'area':'Camden','areas':['Camden'],'max_budget':3000,'bedrooms':2,'bedrooms_minimum':True,'room_type':'whole'}
recs=[{'address':'Offline family home '+str(i),'url':'https://fixtures.invalid/'+str(i),'price':'£2000/month','rank':i+1,'candidate_status':'unknown','possibly_outdated':True} for i in range(10)]
def route(r):
 req=r.request;p=urlparse(req.url)
 if p.netloc!=urlparse(origin).netloc:r.abort();return
 path=p.path
 if path=='/':r.fulfill(content_type='text/html',body=html);return
 if path=='/api/auth/me':data={'authenticated':False}
 elif path=='/api/favorites':data={'favorites':[]}
 elif path=='/api/conversations':
  if req.method=='POST':
   if mode['fail_new']:r.abort('failed');return
   data={'conversation':{'id':'fresh','title':'Fresh'}}
  else:data={'conversations':[{'id':'old','title':'Old'},{'id':'fresh','title':'Fresh'}]}
 elif path.endswith('/messages'):data={'messages':[],'view_state':{'operation':'replace','revision':7 if '/old/' in path else 1,'criteria':criteria,'results':[]}}
 elif path.endswith('/version_map'):data={'version_map':{}}
 elif path=='/api/alex':
  body=req.post_data_json;events.append(body);data={'conversation_id':'fresh','message':'Ten matching cached candidates.','response_type':'search','recommendations':recs,'view_state':{'operation':'replace','revision':1,'criteria':criteria,'results':recs}}
 else:data={}
 r.fulfill(body=json.dumps(data),content_type='application/json')
results=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/tmp/carbonminer-qa-browsers/chromium-1248/chrome-linux64/chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 c=browser.new_context(viewport={'width':390,'height':844});c.route('**/*',route);c.add_init_script("if(window.top===window){localStorage.setItem('ukra_user_id','revision-test');localStorage.setItem('ukra_active_conversation','old');}")
 page=c.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(origin);page.wait_for_function('mainPane && mainPane.conversationId==="old" && !mainPane.isLoading')
 for phase in ['explicit-new','offline-blank-implicit-adoption']:
  if phase!='explicit-new':
   page.locator('#menuToggle').click();page.locator('#convList').get_by_text('Old',exact=True).click();page.wait_for_function('!mainPane.isLoading && mainPane.conversationId==="old"');mode['fail_new']=True
  oldrev=page.evaluate('mainPane.stateRevision');page.locator('#menuToggle').click();page.locator('#newChatBtn').click();page.wait_for_timeout(200)
  before=page.evaluate('({cid:mainPane.conversationId,revision:mainPane.stateRevision,loadToken:mainPane.loadToken})');page.locator('#chatInput').fill('Find a whole family home in Camden');page.locator('#sendBtn').click();page.wait_for_function('!mainPane.isProcessing');page.wait_for_timeout(100)
  result={'phase':phase,'old_revision':oldrev,'before_reply':before,'after_reply':page.evaluate('({cid:mainPane.conversationId,revision:mainPane.stateRevision,loadToken:mainPane.loadToken})'),'reply_recommendations':10,'reply_revision':1,'rendered_cards':page.locator('#resultsContent .property-card').count(),'body':page.locator('#resultsContent').inner_text(),'request_has_conversation_id':'conversation_id' in events[-1]};results.append(result)
  page.screenshot(path=str(f/('revision-'+('before' if args.before else 'after')+'-'+phase+'.png')),full_page=True)
 c.close();browser.close()
output={'phase':'before' if args.before else 'after','results':results,'mock_chat_requests':len(events),'real_model_requests':0,'page_errors':errors,'all_requests_intercepted':True};(f/('ui-revision-'+output['phase']+'.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2));print(json.dumps(output,ensure_ascii=False));assert not errors;assert all(r['rendered_cards']==(0 if args.before else 10) for r in results)
