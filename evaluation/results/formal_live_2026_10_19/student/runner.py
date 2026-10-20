import sys,json,time,pathlib,traceback
sys.path.insert(0,'/tmp/rentcompass-live-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-live-20261019/student')
b=AuditBrowser(folder,profile='student_ucl')
def response_log(response):
    if '/api/' in response.url:
        try:
            data={'time':time.time(),'url':response.url,'status':response.status,'request_method':response.request.method,'request_body':response.request.post_data,'response_body':response.text()}
            with (folder/'student-network.jsonl').open('a') as f:f.write(json.dumps(data,ensure_ascii=False)+'\n')
        except Exception as e:
            with (folder/'student-network-errors.txt').open('a') as f:f.write(str(e)+'\n')
b.page.on('response',response_log)
def inspect():
    return {'url':b.page.url,'title':b.page.title(),'body':b.page.locator('body').inner_text(),'inputs':b.page.locator('input').evaluate_all('(els)=>els.map(e=>({id:e.id,type:e.type,placeholder:e.placeholder,value:e.value}))'),'buttons':b.page.locator('button').evaluate_all('(els)=>els.map(e=>({id:e.id,text:e.innerText,disabled:e.disabled}))'),'local_storage':b.page.evaluate('({...localStorage})'),'session_storage':b.page.evaluate('({...sessionStorage})')}
b.page.get_by_role('button',name='中文',exact=True).click()
b.screenshot('00-initial-zh')
(folder/'00-initial-zh.json').write_text(json.dumps(inspect(),ensure_ascii=False,indent=2))
print(json.dumps({'event':'initial','inspection':inspect()},ensure_ascii=False),flush=True)
for line in sys.stdin:
    try:
        c=json.loads(line);op=c['op']
        if op=='send':
            result=b.send(c['message']);b.screenshot(c['name']);print(json.dumps({'event':'send','record':result},ensure_ascii=False),flush=True)
        elif op=='reload':
            b.page.reload(wait_until='domcontentloaded');b.page.locator('#chatInput').wait_for(state='visible');b.page.wait_for_timeout(1500);b.install_observer();b.screenshot(c['name']);result=inspect();(folder/(c['name']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'event':'reload','inspection':result},ensure_ascii=False),flush=True)
        elif op=='inspect':
            result=inspect();(folder/(c.get('name','inspection')+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'event':'inspect','inspection':result},ensure_ascii=False),flush=True)
        elif op=='api':
            result=b.api(c['path'],c.get('method','GET'),c.get('data'));(folder/(c['name']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'event':'api','result':result},ensure_ascii=False),flush=True)
        elif op=='close':
            b.close();print(json.dumps({'event':'closed'}),flush=True);break
    except Exception as e:print(json.dumps({'error':str(e),'traceback':traceback.format_exc()},ensure_ascii=False),flush=True)
