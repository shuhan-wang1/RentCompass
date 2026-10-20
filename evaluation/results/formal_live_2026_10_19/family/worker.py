import sys,json,pathlib,time
sys.path.insert(0,'/tmp/rentcompass-live-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-live-20261019/family')
b=AuditBrowser(folder,profile='family',mobile=True)
def net(req):
    if '/api/' not in req.url:return
    try:
        res=req.response()
        x={'url':req.url,'method':req.method,'request_body':req.post_data,'status':res.status,'response_body':res.text(),'time':time.time()}
        with (folder/'network.jsonl').open('a') as f:f.write(json.dumps(x,ensure_ascii=False)+'\n')
    except Exception as e:
        with (folder/'network-errors.txt').open('a') as f:f.write(str(e)+'\n')
b.context.on('requestfinished',net)
def save(label,value):
    (folder/(label+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2))
    return value
def snapshot(label):
    b.screenshot(label)
    return save(label,{'url':b.page.url,'body':b.page.locator('body').inner_text(),'controls':b.page.locator('input,button,select,textarea,a,[role=button]').evaluate_all('(es)=>es.map(e=>({tag:e.tagName,id:e.id,text:e.innerText,value:e.value,type:e.type,aria:e.getAttribute("aria-label"),expanded:e.getAttribute("aria-expanded"),rect:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}}))'),'layout':b.page.evaluate('({innerWidth,scrollWidth:document.documentElement.scrollWidth,active:{tag:document.activeElement.tagName,id:document.activeElement.id}})')})
print(json.dumps({'ready':True,'initial':snapshot('initial')},ensure_ascii=False),flush=True)
try:
    for line in sys.stdin:
        try:
            c=json.loads(line);op=c['op']
            if op=='snapshot':r=snapshot(c['label'])
            elif op=='send':r=b.send(c['message']);save(c['label'],r);b.screenshot(c['label'])
            elif op=='api':r=b.api(c['path'],c.get('method','GET'),c.get('data'));save(c['label'],r)
            elif op=='eval':r=b.page.evaluate(c['code']);save(c['label'],r)
            elif op=='click':b.page.locator(c['selector']).click();r=snapshot(c['label'])
            elif op=='key':b.page.keyboard.press(c['key']);r=save(c['label'],b.page.evaluate('({tag:document.activeElement.tagName,id:document.activeElement.id,text:document.activeElement.innerText,outline:getComputedStyle(document.activeElement).outline,boxShadow:getComputedStyle(document.activeElement).boxShadow,rect:document.activeElement.getBoundingClientRect().toJSON()})'));b.screenshot(c['label'])
            elif op=='reload':b.page.reload(wait_until='domcontentloaded');b.page.locator('#chatInput').wait_for(state='visible');b.install_observer();r=snapshot(c['label'])
            elif op=='close':b.close();print(json.dumps({'closed':True}),flush=True);break
            else:raise Exception('unknown op')
            print(json.dumps({'ok':True,'result':r},ensure_ascii=False),flush=True)
        except Exception as e:print(json.dumps({'ok':False,'error':str(e)}),flush=True)
finally:
    if not b.browser.is_connected():pass
    else:b.close()
