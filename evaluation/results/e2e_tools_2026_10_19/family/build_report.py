from pathlib import Path
import json,statistics,collections
f=Path('/tmp/rentcompass-e2e-tools-20261019/family')
rows=[json.loads(p.read_text()) for p in sorted(f.glob('turn??-response.json'))]
trace=json.loads((f/'dispatch-excerpt.json').read_text())
settled=json.loads((f/'ui-after-restart.json').read_text())
meta=[json.loads(x) for x in (f/'family-browser-metadata.jsonl').read_text().splitlines()]
counts=collections.Counter(x['tool'] for t in trace for x in t.get('tool_dispatches',[]))
summary={'persona':'轮椅使用者、两成人与6岁孩子、猫的家庭','chat_endpoint':'POST /api/alex','chat_requests':len(rows),'form_search_requests':0,'conversation_id':rows[0]['result']['payload']['conversation_id'],'http_statuses':dict(collections.Counter(r['result']['status'] for r in rows)),'latency_seconds':{'min':min(r['seconds'] for r in rows),'median':statistics.median(r['seconds'] for r in rows),'max':max(r['seconds'] for r in rows),'sum':round(sum(r['seconds'] for r in rows),3)},'client_timeout_or_error_count':sum(bool(r['error']) for r in rows),'page_errors':[e for m in meta for e in m.get('page_errors',[])],'browser_sessions':len(meta),'external_browser_requests_blocked':sum(len(m.get('external_requests_blocked',[])) for m in meta),'tool_calls':dict(counts),'tool_call_total':sum(counts.values()),'failed_tool_calls':sum(not (x.get('result')or{}).get('success',False) for t in trace for x in t.get('tool_dispatches',[])),'restart_restored_user_messages':settled['chat_count'],'read_only_final_probe_chat_requests':len(settled['chat_requests']),'evidence_notice':'Never publish family-storage.json contents or browser cookies. No such values are included in this report.'}
(f/'family-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
issues=[
 {'id':'F01','severity':'P1','title':'条件修改在模型、工具、返回状态和UI之间不一致，实际按错误条件找房','turns':[8,11,13,14],'expected':'明确的地点、预算与卧室替换应同步到工具参数、结果及表单；撤销的条件不应继续筛选。','actual':'R8要求Camden instead of Watford，工具area=Watford、areas=[Camden]，工具合并两区15套，最终只显示Watford10套。R11要求£500，实际search_properties仍max_budget=3000。R13明确撤销4卧并指定至少2，实际搜索仍bedrooms=4、bedrooms_minimum=false，错误地筛成无结果；工具area为Watford，但返回/UI仍Camden。R14口头回顾Watford/至少2正确，实际恢复的表单仍Camden/4卧且至少复选框未选。','evidence':['turn08-response.json','turn11-response.json','turn13-response.json','turn14-response.json','dispatch-excerpt.json (turns 8,11,13,14)','ui-after-restart.json','15-restart-restored-settled.png']},
 {'id':'F02','severity':'P1','title':'地区级警方结果被最终答案声称为房源坐标级检查','turns':[5],'expected':'要么以房源坐标实际检查，要么清楚说只检查了Watford地区。','actual':'R5最终说“check ran around the listing’s own coordinates”；真实check_safety仅传area=Watford，返回address=Watford，没有房源纬经度。POI调用才有51.646597/-0.423485。用户因而无法区分地区数据与指定房源证据。R4曾正确承认地区范围，R5反而错误升级了证据精度。','evidence':['turn05-response.json','dispatch-excerpt.json (turn 5 check_safety versus search_nearby_pois)']},
 {'id':'F03','severity':'P2','title':'天气请求污染房屋地点，并在明确禁止时仍自动执行通勤','turns':[7,9,10,13,14],'expected':'天气地点不修改找房条件；“不要通勤计算”应阻止该额外调用或至少准确披露实际行为。','actual':'R9请求Camden天气且写Leave housing search criteria unchanged；后续payload constraint_sources.area却引用该天气消息作为Camden房屋地点来源。R7、R10、R14均明确禁止commute，真实仍各调用calculate_commute；R14还用旧Camden→UCL。R7/R14最终称未运行，R10一句称未运行、括号又承认调用，自相矛盾。','evidence':['turn07-response.json','turn09-response.json','turn10-response.json','turn13-response.json (constraint_sources.area)','turn14-response.json','dispatch-excerpt.json (turns 7,10,14)']},
 {'id':'F04','severity':'P2','title':'学校/GP实际未查询，却以无结果口吻回答；半径也偏离请求','turns':[3,5],'expected':'查询学校、GP和超市的所需类别与1km范围；无法支持的类别应说未查询/不支持，不能据未查作无结果。','actual':'R3 search_nearby_pois仅poi_type=supermarket,convenience,pharmacy、radius=800，未请求school或GP。最终称附近搜索没有学校/医疗中心。R5调用省略poi_type/radius，实际只默认supermarket且300m，最终仍说学校/GP无结果。R5坦白半径不是1km，但未重试完成任务。','evidence':['turn03-response.json','turn05-response.json','dispatch-excerpt.json (turns 3,5)']},
 {'id':'F05','severity':'P2','title':'否定与撤销的lift条件仍被作为硬要求，宠物条件跨层丢失','turns':[1,2,5,8,13,14],'expected':'“不要假设有lift就是无障碍”和“lift optional”不能变成必须有lift；猫许可应作为需要核实的家庭条件保留。','actual':'R1把含否定/警示的lift提及加入property_features；R2/R5口头承诺不再要求lift，R8/R13真实搜索仍硬性lift。猫并非被模型完全忘记：R1/R2详情问题确实询问宠物，R8 accumulated_preferences及工具soft_preferences有cat permitted；但R8最终payload soft_preferences为空，R13实际accumulated_preferences只有Would like lift，未保留猫。R14口头仍正确记得猫并明确许可无证据。','evidence':['turn01-response.json','turn02-response.json','turn08-response.json','turn13-response.json','turn14-response.json','dispatch-excerpt.json (turns 1,2,8,13,14)']},
 {'id':'F06','severity':'P2','title':'刷新后所选房源上下文退化，已存在URL仍重新按歧义地址查询','turns':[2,3,4],'expected':'从Ask AI选定的精确房源应可在重新打开页面后继续使用；已保留的viewed_properties URL应可用于解析。','actual':'R2点击首卡Ask AI后request带property/focus_stack及精确URL，详情精确成功。重开浏览器后的R3仅剩viewed_properties仍含相同URL，模型却按Croxley View地址查询，得到两条同名歧义并让用户重新提供链接。R4仍未利用已有URL，直到R5粘贴完整链接才恢复。','evidence':['turn02-response.json (pre.ask_ai_requests=[] and request context)','turn03-response.json','turn04-response.json','dispatch-excerpt.json (turns 2-5)']},
 {'id':'F07','severity':'P2','title':'多工具组合的天气答案被整段吞掉','turns':[6],'expected':'即使通勤无法确认为轮椅可用，也应返回已取得的天气，并分项说明通勤限制。','actual':'R6 get_weather成功，calculate_commute也返回32分钟合成路线；最终只有“The commute condition could not be verified for the listing this round.”，完全没有天气、出处或进一步说明。合成路线不应充当真实无障碍证据，但这不应抹去独立天气结果。R7另问才得到当前天气。','evidence':['turn06-response.json','dispatch-excerpt.json (turns 6,7)']},
 {'id':'F08','severity':'P2','title':'明确撤销旧预算后仍被不必要的冲突确认阻断','turns':[12],'expected':'Discard that £500/four-bedroom experiment + Restore ... £2500应视为明确替换。','actual':'R12零工具调用，助手坚持“尚未明确撤销£500”，要求£500/£2500二选一；用户已清楚表示丢弃旧实验，额外确认消耗一轮并阻断恢复。','evidence':['turn12-response.json','dispatch-excerpt.json (turn 12 empty tool_dispatches)']},
 {'id':'F09','severity':'P2','title':'历史缓存价格被描述为live/current asking rent','turns':[2],'expected':'历史快照的£1600只能称缓存记录价格，不能宣称当前市场有效。','actual':'卡片原本正确标注cached/possibly_outdated，R2却说“live listing record”“current asking rent as published”。随后建议联系中介核实，减轻风险但并未撤回当前性断言。R14改称cached record更准确。','evidence':['turn01-response.json','turn02-response.json','turn14-response.json','dispatch-excerpt.json (get_property_details thin record)']},
 {'id':'F10','severity':'P2','title':'手机侧栏关闭时焦点进入屏外，打开后Escape无效且焦点进入遮罩背景','turns':[],'expected':'关闭的侧栏控件不可Tab访问；打开菜单应有清楚焦点行为和退出方式。','actual':'390×844关闭菜单时Tab落到New x=-88及Guest x=-270，焦点完全看不见；Enter打开后Escape仍保持侧栏和遮罩。320px打开菜单后Tab继续进入聊天输入/搜索等被遮罩背景。Menu按钮无aria-expanded。条件面板则有role=button/tabindex/aria-expanded并可Enter展开。','evidence':['ui-initial.json','ui-controls.json','ui-narrow.json','02-menu-open.png','03-menu-escape.png']}
]
(f/'family-findings.json').write_text(json.dumps({'summary':summary,'issues':issues},ensure_ascii=False,indent=2))
steps=[
('1','家庭原始条件找房','应返回预算/最少两居整租候选，未知无障碍/猫不能确认。','返回13套unknown；缓存及需核实标签正确；lift误加硬条件。','search_properties×1; calculate_commute×11; get_property_details×2'),
('2','移动端首卡Ask AI→具体详情/修正lift','应精确房源详情、未知入口/尺寸/宠物与缓存当前性区分。','精确URL命中；三类信息均未确认；误称价格live/current，lift仅口头修正。','get_property_details×1'),
('3','重开后同房源学校/GP/超市1km','应沿精确房源与所需类别查询。','地址查询歧义；POI只800m超市/便利店/药店，未查学校/GP。','get_property_details×1 failed; search_nearby_pois×1'),
('4','同房源ID的警方安全','应房源坐标检查或说明精度不足。','ID未解析；Watford地区安全，最终诚实承认非房源级并指出99分/高犯罪叙述矛盾。','check_safety×1; get_property_details×1 failed'),
('5','完整URL恢复详情+学校/医疗/超市+安全','应恢复精确身份及正确证据范围。','详情恢复；安全仍仅Watford却被称房源坐标；POI只有300m默认超市。','check_safety×1; search_nearby_pois×1; get_property_details×1'),
('6','天气三天+房源到UCL通勤组合','应分别输出天气与无障碍通勤未知。','两工具成功，但最终只有通勤不能验证一句，天气丢失。','get_weather×1; calculate_commute×1'),
('7','仅天气，不跑通勤、不改条件','应当前数据据实、无预测不编造。','正确说明仅当前14.2°C/小雨、无三天预报；但实际仍通勤且声称未跑。','get_weather×1; calculate_commute×1'),
('8','改Camden/£3000，保留其他家庭条件','应替换Watford并应用lift修正。','工具合并Watford+Camden，最终仍Watford10套；lift硬条件未撤销；cat跨层不一致。','search_properties×1; calculate_commute×11'),
('9','Camden当前天气（固定HTTP503窗口）','应有界尝试后说明不可用，不复用旧天气伪造。','三次真实get_weather失败；最终泛化不可用文案，未编造天气。','get_weather×3 failed'),
('10','同会话天气故障恢复','应重试成功且不污染房屋条件。','天气成功14.2°C/小雨；额外Camden→UCL通勤仍被调用，文字自相矛盾。','get_weather×1; calculate_commute×1'),
('11','Camden仅四居£500严条件空结果','应按500筛选、无结果清楚且不擅自放宽。','无候选且不声称全市场空；但实际预算仍3000，故不能认定正确执行500筛选。','search_properties×1; calculate_commute×1'),
('12','丢弃500/四居实验，恢复原条件','应理解明确替代并搜索。','误判预算冲突而要求二次确认；零工具。','none'),
('13','再次明确撤销，Watford£2500至少两居','应返回恢复后的正确搜索。','实际筛Watford精确4居而非至少2；最终UI仍Camden/4居，错误无结果。','search_properties×1; calculate_commute×1'),
('14','真实服务重启后重开、精确URL详情与条件回顾','应恢复聊天/来源/状态并连续使用。','14条用户消息恢复、同会话URL详情成功；入口/尺寸/猫仍诚实未知；口头条件正确但UI仍Camden/4居，且禁止通勤仍执行。','get_property_details×1; calculate_commute×1')]
lines=[
'RentCompass 独立家庭端到端审计（实测结果）',
'日期：2026-10-19。身份：两成人、6岁孩子、轮椅使用者、猫。地点起点Watford，整租、至少两卧、£2500/月、UCL 50分钟，强调街道至房门及屋内可达性。',
'',
'范围与方法',
'只读取公共测试驱动/BRIEF、公开页面DOM和本persona导出的真实调度日志；不读其他persona结论或旧报告、不修改产品代码。390×844移动视口为主要操作面，另测320×640和键盘Tab/Enter/Escape。所有浏览器外部路由被阻断；没有点击外部View Deal/Map或直接访问OnTheMarket。服务端房源为历史快照，天气/TfL/警方/OSM/web是HTTP边界固定合成数据；LLM、工具dispatch、筛选及持久化是真实执行。',
'',
'真实请求与工具覆盖',
json.dumps(summary,ensure_ascii=False,indent=2),
'',
'逐轮步骤 / 预期 / 实际 / 真实工具',
]
for n,step,expect,actual,tools in steps:
 lines.extend([f'R{n} {step}',f'预期：{expect}',f'实际：{actual}',f'实际工具：{tools}',f'证据：turn{int(n):02d}-response.json、turn{int(n):02d}-mobile.png、dispatch-excerpt.json对应turn。',''])
lines.extend(['问题及复现证据（P1=可能显著误导租房决定/证据归属；P2=功能或访问障碍）',''])
for x in issues:
 lines.extend([f"{x['id']} {x['severity']} {x['title']}",f"预期：{x['expected']}",f"实际：{x['actual']}",'证据：'+'；'.join(x['evidence']),''])
lines.extend([
'正确行为与边界',
'1. R1卡片全部unknown，显示Conditions need verification及缓存可能变化，未把无障碍/猫视为已证实。R2/R14精确URL详情均明确没有入口、门宽/浴室尺寸和猫许可证据。',
'2. 未承诺任何学校学位、机构轮椅入口或街道路由无台阶；距离明确为直线。学校/GP未真正查询仍是缺陷，不能以此正确免责声明替代功能完成。',
'3. R4/R5助手识别安全工具99/100/Very Safe与高犯罪/夜间避免步行/低于London平均的矛盾，不给家庭晚间路线安全保证。该保守回答不能修正R5错误的地点归属。',
'4. R7对只有当前天气、null雨概率及无警报数据保持诚实，没有编造未来三天。R9三个get_weather失败后无伪造；R10恢复后同一会话成功继续使用。',
'5. 空结果没有声称整个现实市场不存在房源，也未将超预算候选标为精确匹配。但R11预算与R13卧室实际错误，所以不算严格条件完整通过。',
'6. 390px和320px无整体横向滚动，19次浏览器打开未记录pageerror；条件面板的Enter/Tab基本可用。字体、图片被阻断造成的缺图/字形不计产品缺陷。',
'',
'真实重启与刷新恢复',
'根agent在R13关闭浏览器、保存state后真实重启隔离容器并报告健康200；R14才重开同一family profile。结果仍同conversation_id，完整恢复先前消息并继续使用精确URL详情。R14之后另做零聊天的UI复查，等待最后消息attached再记录，确认14条用户消息全部存在；ui-after-restart.json的chat_requests=[]。恢复后的表单为Camden、2500、4卧、至少复选框false、whole、Gower Street和50分钟。故技术持久化通过，错误语义状态同样被持久化，口头正确回顾未修正UI。页面初开是异步水合；最初瞬时空表单不作为数据丢失证据。',
'Ask AI按钮在R2只设置焦点，不自动请求；pre.ask_ai_requests=[]，用户实际输入问题再发送。刷新后focus_stack/property未保留、viewed_properties保留，构成F06具体复现。当前无结果列表使最后收藏流程不适用，本审计没有声称完成收藏保存。',
'',
'无障碍检查的实际范围',
'确实实测：真实Chromium中的Tab顺序、屏外焦点坐标、可见focus outline、Enter触发、Escape关闭、遮罩背景仍可聚焦、390/320布局及表单滚动。观察到聊天区域无role/log或aria-live，聊天输入及Area/预算单位缺显式label/aria-label；仅作为DOM观察，未将其直接等同于已实测屏幕阅读器故障。未运行NVDA/VoiceOver、真实移动系统软键盘、实体触摸/缩放或轮椅路由验证。',
'',
'长期模拟局限',
'这是单个guest会话的14轮、约129秒服务等待时间加UI操作，包含多次浏览器重开和一次在本persona流程中实际发生的服务重启；不代表跨日/跨月记忆、房源价格或可租状态更新、多个设备同步或真实市场可达性。所有外部数据为明确合成夹具，不能将固定32分钟、14.2°C、135宗案或Tesco条目当作真实伦敦/Watford事实。只有weather503在本persona真实聊天中注入并恢复；本persona未用真实聊天单独注入POI/警方失败。工具结果是实际dispatch输出，不是单看模型说自己查过。',
'',
'公开归档注意',
'可归档本报告、family-findings.json、family-summary.json、turn??-response.json、各PNG及dispatch-excerpt.json。family-storage.json是复现用浏览器状态，包含cookie；不要公开其内容。报告没有复制任何cookie、密码或访问令牌。'
])
(f/'family-report.txt').write_text('\n'.join(lines))
print(json.dumps({'report':str(f/'family-report.txt'),'findings':str(f/'family-findings.json'),'summary':summary},ensure_ascii=False))
