from pathlib import Path
import collections,json,hashlib,shutil,xml.etree.ElementTree as ET
B=Path('/tmp/rentcompass-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');D=R/'evaluation/results/formal_live_2026_10_19';D.mkdir(parents=True,exist_ok=True)
def read(p):return json.loads(p.read_text())
def lines(p):return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2,ensure_ascii=False)+'\n')
private={'cookie','cookies','set-cookie','authorization','password','auth_sid','csrf_token','access_token','refresh_token','api_key','deepseek_api_key','flask_secret_key'}
def redact(v):
 if isinstance(v,dict):
  if str(v.get('name','')).lower() in private:return {**v,'value':'[REDACTED]'}
  return {k:('[REDACTED]' if k.lower() in private else redact(x)) for k,x in v.items()}
 if isinstance(v,list):return [redact(x) for x in v]
 if isinstance(v,str) and v.lstrip().startswith(('{','[')):
  try:return json.dumps(redact(json.loads(v)),ensure_ascii=False)
  except (ValueError,TypeError):pass
 return v
for folder in ['evidence','student','professional','family','harness','public','artifacts']:
 for p in (B/folder).rglob('*'):
  if not p.is_file() or '__pycache__' in p.parts or 'storage' in p.name.lower() or 'approval' in p.name.lower() or p.suffix not in {'.json','.jsonl','.png','.py','.txt','.log','.xml','.html'}:continue
  q=D/p.relative_to(B);q.parent.mkdir(parents=True,exist_ok=True)
  if p.suffix=='.json':write(q,redact(read(p)))
  elif p.suffix=='.jsonl':q.write_text(''.join(json.dumps(redact(x),ensure_ascii=False)+'\n' for x in lines(p)))
  else:shutil.copy2(p,q)
H=lines(B/'evidence/http-requests.jsonl');M=lines(B/'evidence/model-calls.jsonl');T=read(B/'evidence/live-tool-integration.json');C=lines(B/'evidence/tool-dispatches.jsonl');ledger=[]
for line in (B/'private/app.log').read_text(errors='replace').splitlines():
 try:
  o=json.loads(line);r=json.loads(o.get('message',''))
  if r.get('event')=='canary.turn':ledger.append(r)
 except (ValueError,TypeError):pass
write(D/'evidence/completed-turn-ledger.json',ledger)
root=ET.parse(B/'artifacts/full-regression.xml').getroot();s=root.find('testsuite');skips=[{'test':t.get('classname')+'.'+t.get('name'),'reason':t.find('skipped').get('message')} for t in root.iter('testcase') if t.find('skipped') is not None]
unit={'passed':int(s.get('tests'))-int(s.get('failures'))-int(s.get('errors'))-int(s.get('skipped')),'failed':int(s.get('failures')),'errors':int(s.get('errors')),'skipped':int(s.get('skipped')),'seconds':float(s.get('time')),'skip_reasons':skips}
names=['search_properties','get_property_details','calculate_commute','calculate_commute_cost','get_transport_info','check_transport_cost','check_safety','get_weather','search_nearby_pois','web_search','compare_or_rank_areas','remember','recall_memory','ask_user'];coverage=[]
for name in names:
 browser=[x for x in C if x['tool']==name];direct=[x for x in T if x['tool']==name and x.get('result')];ask_terminals=sum((x.get('tool_latency')or{}).get('ask_user',{}).get('count',0) for x in ledger) if name=='ask_user' else 0
 row={'tool':name,'browser_executor_calls':len(browser),'terminal_calls':ask_terminals,'direct_integration_attempts':len(direct),'browser_request_ids':sorted({x['request_id'] for x in browser}),'live_housing_success_certified':False if name in ['search_properties','get_property_details','compare_or_rank_areas'] else None}
 if name=='get_property_details':row['result']='BLOCKED: no successfully fetched current listing identity'
 elif name=='search_properties':row['result']='403 failure-response replay exercised; live listing success BLOCKED'
 elif name=='compare_or_rank_areas':row['result']='PASS honest no_data; positive rent ranking BLOCKED'
 elif name=='search_nearby_pois':row['result']='DEGRADED: 30s test deadline; Overpass mirror failures observed'
 elif name=='web_search':row['result']='DEGRADED: no usable result, unresponsive search engines'
 elif name=='ask_user':row.update(result='PASS valid options contract; invalid test clarification_kind rejected',valid_contract=read(B/'evidence/ask-user-valid-contract.json')['success'])
 else:row['result']='Executed real implementation; inspect raw result and evidence scope'
 coverage.append(row)
write(D/'TOOL_COVERAGE.json',coverage)
issues=[
 {'id':'F01','priority':'P1','title':'房源数据源失败被归为已搜索但为空，界面引导用户改条件','observed':'捕获403回放时area_status=empty/partial=false，source无候选；正文与面板建议调整预算或区域。附加的市场非空提示未解释来源失败。','evidence':['student/turn-2-response-payload.json','student/report.txt','family/own-dispatches.json','evidence/live-tool-integration.json'],'suggestion':'贯穿HTTP错误类型和source_status；403、429、超时、schema drift与complete_empty分离。显示来源不可用、保留当前条件，并在源站拒绝时启用共享暂停/冷却。','verification':'实际403响应回放后不得出现complete_empty/empty或调整预算建议；有效空页面仍可显示真正的零结果。'},
 {'id':'F02','priority':'P1','title':'租金上限与租金加交通总预算被误判冲突，确认后租金仍为空','observed':'英文上班族分别给出£1900租金与£2100总预算，系统重复澄清；接受第1选项后租金字段仍null。','evidence':['professional/turn2_driver_result.json','professional/turn3_driver_result.json','professional/report.txt'],'suggestion':'分别维护rent ceiling和combined budget的带来源字段；同一条输入的两个不同口径不能互相覆盖或触发冲突。','verification':'原样重放三条英文消息后max_budget=1900、monthly_total_budget=2100，刷新与执行参数一致。'},
 {'id':'F03','priority':'P1','title':'混合办公被设成无需通勤，已确认目的地未贯穿持久化视图','observed':'Bank、Tue–Thu、35分钟、至多一次换乘的输入出现no_commute=true及空目的地；后续raw commute_destination有Bank但view_state.destination空。','evidence':['professional/turn1_driver_result.json','professional/turn3_driver_result.json','professional/after_turn3_reload.json'],'suggestion':'按用户明确的通勤语义更新，不把非每日到岗视为无通勤；统一commute_destination/destination与视图映射，补齐换乘条件的表示或明确不支持。','verification':'保留Bank/35分钟/需通勤；所有预算/目的地表单字段在同响应、刷新和重启后保持一致。'},
 {'id':'F04','priority':'P1','title':'通勤日期污染入住日期，指定到达时间未发送给TfL','observed':'用户要求20 October 08:45到达Bank，move_in_date变为2026-10-20；实际JourneyResults请求没有date/time/timeIs查询参数。另一次派生fare查询把目的地截为Bank arriving后失败。','evidence':['professional/turn3_driver_result.json','professional/own-dispatches.json','evidence/http-requests.jsonl'],'suggestion':'把入住日期与旅程日期/到达时间设为独立类型化槽位；将时间字段传到实际请求；截取站点名时排除arriving等从句。','verification':'不改变未声明的move_in_date；实际TfL请求必须带所选日期和08:45到达语义；不能用默认此刻的路线认证指定时段。'},
 {'id':'F05','priority':'P1','title':'双区域意图在同轮执行和持久化视图中不稳定','observed':'家庭首轮先调用Stratford+Leyton又调用Leyton，最终只回显Leyton；第三轮工具回显双区域，但view_state仍无area/areas，刷新Area为空。','evidence':['family/own-dispatches.json','family/after-turn3-refresh.json','family/final-persisted-messages-api.json'],'suggestion':'单一规范化criteria驱动工具、资格、表单和快照；一轮多工具不得把双区域默默缩成最后一个。','verification':'原样重放中英文两条请求后，工具和持久化视图均保留两区，刷新Area不为空。'},
 {'id':'F06','priority':'P1','title':'自然表达的猫许可和轮椅内部空间未进入可审核硬约束','observed':'明确中文“允许这只猫也是必需条件”和英语written permission/internal doorway/turning space后，实际hard features仅step-free；未包含cats allowed或wheelchair-accessible，step-free还重复进入soft。','evidence':['family/turn1-family-search.json','family/turn3-family-clarification-search.json','family/own-dispatches.json'],'suggestion':'统一需求事件/硬软属性表示，保留猫许可与功能性轮椅需求的自然语言证据；不靠某一句固定短语才能识别。','verification':'同义中英文表达进入hard constraints；缺少对应证据时必须unknown，不能被lift替代。'},
 {'id':'F07','priority':'P2','title':'组合任务缺项和内部指令直接进入用户回复','observed':'预算澄清阻断可独立查询的路线/天气；最后未算用户要求的26个单程/月；家庭第三轮直接显示Do not conclude/State only等指令句。','evidence':['professional/report.txt','family/turn3-family-clarification-search.json'],'suggestion':'把独立子任务和澄清依赖分开；将partial_note的机器约束与用户文案分离；逐项显示已完成/未知/未完成。','verification':'房源失败或预算待确认不阻断独立天气/交通；用户回复不出现内部命令；未完成任务如实标明。'},
 {'id':'F08','priority':'P2','title':'真实附近设施与网页检索能力退化','observed':'学校/GP附近设施探针30秒超时，多个Overpass镜像出现502/500/连接错误；SearXNG两次真实查询无可用结果并有4个未响应引擎。','evidence':['evidence/live-tool-integration.json','evidence/http-requests.jsonl','artifacts/live-tools.log'],'suggestion':'单独验证并观测外部服务；总预算内有界镜像切换，返回已完成类别和未知类别；搜索引擎健康与空结果分开。','verification':'服务故障不等于没有学校/GP/网页证据；保留部分结果，健康指标能显示真实能力退化。'},
 {'id':'F09','priority':'P2','title':'窄屏长会话标题裁剪会话操作按钮','observed':'390px视口内，长中文标题把4个操作初始位置挤出侧栏；键盘Tab会滚至操作，基础焦点环绕/Escape正常。','evidence':['family/menu-long-title-stable.png','family/final-menu-open.png','family/report.txt'],'suggestion':'约束标题flex最小宽度、换行/省略与操作列宽度，验证鼠标/触屏和键盘可见性。','verification':'长中英文标题下四个操作均在视口内可见可点，保留焦点边界。'},
 {'id':'F10','priority':'P3','title':'硬租金上限被向上取整','observed':'学生给定整套月上限£2166.67，实际保存2167，放宽£0.33；未获得房源，未证明实际发生超限推荐。','evidence':['student/turn-2-response-payload.json','student/report.txt'],'suggestion':'金额用Decimal/便士整数；筛选硬上限不得向上放宽，展示与搜索精度分离。','verification':'所有实际搜索上界不高于用户给定值，周月转换精度可解释。'},
]
write(D/'ISSUES.json',issues)
verification={'date':'2026-10-19','formal_acceptance':'FAIL','live_housing_acceptance':'BLOCKED_AFTER_ROBOTS_PREFLIGHT_403','regression':unit,'product_source_verification':read(B/'evidence/source-verification.json'),'personas':{'student':{'chat_turns':2,'isolated_parent_context':True,'additional_chat':'automatic review denied as exceeding turn limit; stored records show only two completed turns; no retry'},'professional':{'chat_turns':3,'isolated_parent_context':True},'family':{'chat_turns':3,'isolated_parent_context':True,'method_limit':'initial BODY DOM capture included inline client script; removed promptly; reported and not used to derive findings'}},'total_browser_chat_turns':8,'live_housing':{'actual_external_requests':1,'actual_request_kind':'robots.txt preflight','actual_response_status':403,'actual_listing_search_requests':0,'actual_detail_requests':0,'captured_failure_response_replays':sum(h['transport']=='captured_preflight_403_replay' for h in H),'synthetic_success_listing_responses':0,'retry_or_identity_rotation':False,'robots403_does_not_prove_all_listing_pages_are_unreachable':True},'other_http':{'actual_requests':sum(h.get('actual_external_request',False) for h in H),'host_status_counts':[{'host':k[0],'status':k[1],'count':v} for k,v in collections.Counter((h['host'],h.get('status')) for h in H if h.get('actual_external_request')).items()],'successful_http_is_not_automatically_task_success':True},'models':{'requests':len(M),'real_upstream_requests':sum(not m.get('synthetic_failure') and not m.get('synthetic_response') for m in M),'reported_tokens':sum(m.get('usage',{}).get('total_tokens',0) for m in M),'synthetic_responses':sum(bool(m.get('synthetic_response')) for m in M),'request_budget':100,'token_budget':600000},'tools_registered':14,'tool_integration_unique_executed':len({x['tool'] for x in T if x.get('result')}),'live_listing_details':'BLOCKED; no valid current identity for positive test','process_restart':{'checks':len(read(B/'evidence/restart-checks.json')),'passed':sum(x['passed'] for x in read(B/'evidence/restart-checks.json'))},'memory_reopen':{'checks':len(read(B/'evidence/memory-reopen-checks.json')),'passed':sum(x['passed'] for x in read(B/'evidence/memory-reopen-checks.json'))},'findings':{'P1':sum(x['priority']=='P1' for x in issues),'P2':sum(x['priority']=='P2' for x in issues),'P3':sum(x['priority']=='P3' for x in issues)},'environment':{'origin':'http://127.0.0.1:15009','architecture':'fc_loop','mcp_tools':False,'independent_test_databases':True,'background_jobs':False,'success_fixtures_for_weather_tfl_police_osm_web':False,'description_auto_enrichment':False},'limitations':['No successful live housing fetch or detail, price, availability or real property-specific qualification certified','OTM search failures exercised by replay of captured robots403, not direct responses from listing pages','Only 8 browser turns and one process restart; not a multiweek soak','POI timeout and web service degradation; no full live success claim','Current fc_loop in-process tools; optional MCP/manager architectures not E2E exercised','Browser external resources/links aborted to prevent unintended OTM traffic','No product fixes or deployment in this formal acceptance run']}
assert unit['failed']==unit['errors']==0 and not verification['product_source_verification']['product_changed_during_test']
write(D/'VERIFICATION.json',verification)
# Verify exact configured secrets and live session values are absent.
needles=[str(v) for k,v in read(B/'private/env.json').items() if v and any(s in k.upper() for s in ['KEY','SECRET','PASSWORD','TOKEN'])]
for who in ['student','professional','family']:
 for p in (B/who).glob('*storage.json'):needles += [c['value'] for c in read(p).get('cookies',[]) if c.get('value')]
leaks=[]
for p in D.rglob('*'):
 if p.is_file() and p.suffix in {'.json','.jsonl','.py','.txt','.log','.xml','.html'}:
  text=p.read_text(errors='replace')
  if any(len(n)>12 and n in text for n in needles):leaks.append(str(p.relative_to(D)))
assert not leaks, 'private credential match in '+repr(leaks)
write(D/'evidence/privacy-verification.json',{'passed':True,'exact_private_values_checked':len(needles),'credential_matches':[],'storage_files_excluded':True,'auth_headers_redacted':True})
print(json.dumps({'destination':str(D),'verdict':verification['formal_acceptance'],'regression':unit['passed'],'findings':verification['findings'],'model':verification['models'],'files':sum(p.is_file() for p in D.rglob('*'))},ensure_ascii=False))
