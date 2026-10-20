import importlib.util,json,pathlib,sys,time,traceback

ROOT=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/student_retest')
spec=importlib.util.spec_from_file_location('public_driver','/tmp/rentcompass-e2e-repair-20261019/public/test_driver.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
b=mod.AuditBrowser(ROOT,profile='student')
responses=[]
b.page.on('response',lambda response: responses.append(response) if '/api/' in response.url else None)
seq=max([int(p.name.split('-')[0]) for p in ROOT.glob('[0-9][0-9]-*-state.json')]+[0])

def install_raw():
    b.page.evaluate('''() => {
      if(window.__studentRawInstalled)return;
      window.__studentRawInstalled=true;window.__studentRaw=[];
      const f=window.fetch;
      window.fetch=async(...args)=>{
        const response=await f(...args);
        if(String(args[0]).includes('/api/alex')) {
          response.clone().text().then(body=>window.__studentRaw.push({url:String(args[0]),request_body:args[1]?.body,status:response.status,response_body:body})).catch(e=>window.__studentRaw.push({error:String(e)}));
        }
        return response;
      };
    }''')
install_raw()

def capture(label):
    global seq
    seq+=1
    snap={'time':time.time(),'label':label,'url':b.page.url,'body':b.page.locator('body').inner_text(),
          'local_storage':b.page.evaluate('Object.fromEntries(Object.entries(localStorage))'),
          'form_fields':b.page.locator('input,select,textarea').evaluate_all('(els)=>els.map(e=>({id:e.id,name:e.name,type:e.type,value:e.value,checked:e.checked,placeholder:e.placeholder}))'),
          'buttons':b.page.locator('button').evaluate_all('(els)=>els.map(e=>({text:e.innerText,id:e.id,title:e.title,aria:e.getAttribute("aria-label"),disabled:e.disabled}))')}
    (ROOT/f'{seq:02d}-{label}-state.json').write_text(json.dumps(snap,ensure_ascii=False,indent=2))
    b.screenshot(f'{seq:02d}-{label}')
    b.context.storage_state(path=str(b.state))
    net=[]
    for response in responses:
        try:
            req=response.request
            net.append({'url':response.url,'status':response.status,'method':req.method,'request_headers':req.headers,'request_body':req.post_data,'response_headers':response.headers,'response_body':response.text()})
        except Exception as e:net.append({'url':response.url,'error':str(e)})
    responses.clear()
    net.extend(b.page.evaluate('window.__studentRaw||[]'))
    b.page.evaluate('if(window.__studentRaw)window.__studentRaw=[]')
    (ROOT/f'{seq:02d}-{label}-network.json').write_text(json.dumps(net,ensure_ascii=False,indent=2))
    return {'label':label,'body':snap['body'],'buttons':snap['buttons'],'form_fields':snap['form_fields'],'storage_keys':list(snap['local_storage']),'network':[{'url':n.get('url'),'status':n.get('status')} for n in net],'screenshot':str(ROOT/f'{seq:02d}-{label}.png')}

def compact_turn(result,summary):
    payload=(result.get('result') or {}).get('payload') or {}
    return {'message':result['message'],'seconds':result['seconds'],'error':result['error'],
            'result':{k:v for k,v in payload.items() if k not in ('recommendations','view_state')},
            'recommendations':[{k:x.get(k) for k in ('address','bedrooms','room_type','price','candidate_status','candidate_reasons','candidate_unknown_reasons','possibly_outdated')} for x in payload.get('recommendations',[])],
            'view_state_operation':payload.get('view_state',{}).get('operation'),
            'request':result['request'],'snapshot':{'screenshot':summary['screenshot'],'form_fields':summary['form_fields'],'buttons':summary['buttons'],'network':summary['network']}}

print(json.dumps({'ready':True,'initial':capture('initial')},ensure_ascii=False),flush=True)
for line in sys.stdin:
    try:
        cmd=json.loads(line);action=cmd['action']
        if action=='send':
            result=b.send(cmd['message']);summary=capture(cmd['label'])
            print(json.dumps(compact_turn(result,summary),ensure_ascii=False),flush=True)
        elif action=='reload':
            b.page.reload(wait_until='domcontentloaded');b.page.locator('#chatInput').wait_for(state='visible');b.page.wait_for_timeout(1000);b.install_observer();install_raw()
            print(json.dumps(capture(cmd['label']),ensure_ascii=False),flush=True)
        elif action=='click_turn':
            before=b.page.evaluate('window.__auditResults.length');started=time.time()
            b.page.locator(cmd['selector']).click()
            b.page.wait_for_function(f'window.__auditResults.length>{before}',timeout=180000);b.page.wait_for_timeout(300)
            result={'time':time.time(),'message':'[CLICK] '+cmd['selector'],'seconds':round(time.time()-started,3),'error':None,'result':b.page.evaluate(f'window.__auditResults[{before}]'),'request':b.page.evaluate('window.__auditRequests.at(-1)'), 'dom':b.page.locator('#chatMessages').inner_text()}
            with b.log.open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
            print(json.dumps(compact_turn(result,capture(cmd['label'])),ensure_ascii=False),flush=True)
        elif action=='click':
            b.page.locator(cmd['selector']).click();b.page.wait_for_timeout(750)
            print(json.dumps(capture(cmd['label']),ensure_ascii=False),flush=True)
        elif action=='inspect':print(json.dumps(capture(cmd['label']),ensure_ascii=False),flush=True)
        elif action=='eval':print(json.dumps({'value':b.page.evaluate(cmd['expression'])},ensure_ascii=False),flush=True)
        elif action=='api':
            result=b.api(cmd['path'],cmd.get('method','GET'),cmd.get('data'))
            (ROOT/(cmd['label']+'-api.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False),flush=True)
        elif action=='close':
            capture(cmd.get('label','closed'));b.close();print(json.dumps({'closed':True}),flush=True);break
    except Exception as e:print(json.dumps({'error':str(e),'traceback':traceback.format_exc()},ensure_ascii=False),flush=True)
