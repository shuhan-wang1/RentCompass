"""Run only after parent announces local server restart. Never sends chat."""
import sys,json,pathlib,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
sys.dont_write_bytecode=True
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/student')
a=AuditBrowser(f,profile='student');blocked=[]
a.context.route('**/api/alex**',lambda route:(blocked.append(route.request.url),route.abort('blockedbyclient')))
a.page.wait_for_timeout(800)
def snapshot():
 return a.page.evaluate('''()=>({body:document.body.innerText,chat:document.querySelector('#chatMessages').innerText,conversations:Array.from(document.querySelectorAll('.conv-item-main')).map(e=>e.innerText),favoriteLabel:document.querySelector('#btn-favorites').innerText,fields:Array.from(document.querySelectorAll('.criteria-input,.criteria-select')).map(e=>({placeholder:e.placeholder,value:e.value})),cards:Array.from(document.querySelectorAll('.property-card')).map(e=>e.innerText)})''')
before=snapshot();a.page.locator('#btn-favorites').click();a.page.wait_for_timeout(300);favorites=snapshot();a.screenshot('15-after-server-restart-favorites')
a.page.reload(wait_until='domcontentloaded');a.page.wait_for_timeout(800);after_reload=snapshot();a.page.locator('#btn-favorites').click();a.page.wait_for_timeout(300);favorites_after_reload=snapshot();a.screenshot('15-after-server-restart-refreshed')
checks={
 'two_conversations_restored':len(before['conversations'])==2,
 'latest_conversation_transcript_restored':'现在另外帮一位新同学找房' in before['chat'],
 'prior_recall_answer_restored':'月预算：最高 £1800' in before['chat'],
 'one_favorite_restored':favorites['favoriteLabel'].endswith('1') and len(favorites['cards'])==1,
 'favorite_identity_and_price_restored':len(favorites['cards'])==1 and 'Flanders Court' in favorites['cards'][0] and '£1350/month' in favorites['cards'][0],
 'favorite_budget_snapshot_restored':len(favorites['cards'])==1 and '收藏时月租预算：£1800' in favorites['cards'][0],
 'favorite_survives_refresh':len(favorites_after_reload['cards'])==1 and 'Flanders Court' in favorites_after_reload['cards'][0],
 'no_chat_attempted':len(blocked)==0,
}
report={'time':time.time(),'checks':checks,'before':before,'favorites':favorites,'after_reload':after_reload,'favorites_after_reload':favorites_after_reload,'blocked_chat_requests':blocked,'page_errors':a.errors}
(f/'recovery-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));a.close();print(json.dumps({'checks':checks,'all_pass':all(checks.values()),'page_errors':report['page_errors']},ensure_ascii=False))
