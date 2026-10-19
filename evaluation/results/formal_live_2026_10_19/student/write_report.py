import json,pathlib
from decimal import Decimal
p=pathlib.Path('/tmp/rentcompass-live-20261019/student')
turns=[json.loads(s) for s in (p/'student_ucl-turns.jsonl').read_text().splitlines()]
a,b=[r['result']['payload'] for r in turns]
r1=json.loads((p/'02-refresh-after-clarification.json').read_text())
r2=json.loads((p/'04-refresh-after-search.json').read_text())
history=json.loads((p/'own-messages-latest.json').read_text())
profile={'persona':'中文国际学生；UCL Bloomsbury；预算有限；开始时可独居或与一位同学整租，澄清后选择两人整租两居','interface_language':'zh','user_id':'b2c519cca16b46a593ea484ba63fd1c0','conversation_id':a['conversation_id'],'requested_move_in':'2026-11-15','max_commute_minutes':30,'individual_budget_gbp_per_week':250,'whole_home_budget_gbp_per_week':500,'whole_home_budget_gbp_per_month':2166.67,'chat_count_completed':len(turns),'browser_closed':True}
(p/'profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2))
for i,r in enumerate(turns,1):
 (p/f'turn-{i}-response-payload.json').write_text(json.dumps(r['result'],ensure_ascii=False,indent=2))
 (p/f'turn-{i}-request.json').write_text(json.dumps(r['request'],ensure_ascii=False,indent=2))
checks=[]
def check(key,condition,expected,actual,evidence,severity=None):
 checks.append({'id':key,'status':'PASS' if condition else 'FAIL','expected':expected,'actual':actual,'evidence':evidence,'severity_if_failed':severity})
check('CHAT_LIMIT',len(turns)<=3,'最多3次真实聊天',len(turns),'student_ucl-turns.jsonl; own-messages-latest.json')
check('CLARIFY_BEFORE_SEARCH',a.get('response_type')=='clarification' and a.get('agent_state')=='waiting_for_input','先澄清，不返回房源',{'response_type':a.get('response_type'),'agent_state':a.get('agent_state')},'turn-1-response-payload.json')
check('BUDGET_BASIS_CLARIFICATION','个人分摊' in a['message'] and '整套' in a['message'],'询问个人/整套预算口径',a['message'],'turn-1-response-payload.json')
check('WEEK_TO_MONTH',abs(Decimal(str(a['known_criteria']['max_budget']))-Decimal(250)*52/12)<1,'个人周£250折月约£1083.33',a['known_criteria']['max_budget'],'turn-1-response-payload.json')
check('REFRESH_CLARIFICATION','每周 250 镑是我个人分摊的房租，想和同学整租两居' in r1['body'] and r1['local_storage'].get('rentcompass.language')=='zh','刷新保留中文、澄清文本和选项',{'language':r1['local_storage'].get('rentcompass.language'),'option_preserved':'每周 250 镑是我个人分摊的房租，想和同学整租两居' in r1['body']},'02-refresh-after-clarification.json; .png')
check('WHOLE_BUDGET_NOT_PERSONAL',abs(Decimal(str(b['search_criteria']['max_budget']))-Decimal(500)*52/12)<1,'两人整租总预算约月£2166.67，而非月£1083',b['search_criteria']['max_budget'],'turn-2-response-payload.json')
check('HARD_BUDGET_PRECISION',Decimal(str(b['search_criteria']['max_budget']))<=Decimal('2166.67'),'保存的最多月租不超£2166.67',b['search_criteria']['max_budget'],'turn-2-response-payload.json; 04-refresh-after-search.json','P3：保存/显示上限多£0.33，尚无房源可验证过滤影响')
expected_inputs=['2167','University College London, Gower Street, London WC1E 6BT','30','2026-11-15']
values=[i['value'] for i in r2['inputs']]
check('REFRESH_CONFIRMED_CRITERIA',all(v in values for v in expected_inputs),'刷新后保留已确认预算、UCL地址、30分钟和日期',values,'04-refresh-after-search.json; .png')
check('SAME_CONVERSATION',r1['local_storage'].get('ukra_active_conversation')==a['conversation_id']==b['conversation_id']==r2['local_storage'].get('ukra_active_conversation'),'两次刷新和两轮聊天在同一会话',a['conversation_id'],'student_ucl-turns.jsonl; 02/04-refresh*.json')
check('PERSISTED_MESSAGES',len(history['messages'])==4 and sum(m['role']=='user' for m in history['messages'])==2,'本画像持久化2条用户消息、2条助手消息',{'count':len(history['messages']),'roles':[m['role'] for m in history['messages']]},'own-messages-latest.json')
check('NO_INVENTED_LISTINGS',b.get('recommendations')==[] and b['view_state']['results']==[],'拒绝数据条件下不虚构候选房源',{'recommendations':b.get('recommendations'),'view_results':b['view_state']['results']},'turn-2-response-payload.json')
check('NO_MARKET_ABSENCE_CLAIM','不能据此判断市场上没有房源' in b['message'],'说明不能据空数据判断市场无房',b['message'],'turn-2-response-payload.json')
check('EXPLICIT_ACCESS_FAILURE',any(w in b['message'] for w in ['403','拒绝','无法访问','访问失败']),'明确解释本次访问被拒绝，避免让用户误以为有效检索零匹配',b['message'],'turn-2-response-payload.json; 03-confirm-shared-budget-search.png','P2：误导性失败/空结果解释')
panel=r2['body'].split('房源列表',1)[-1]
check('EMPTY_PANEL_EXPLAINS_FAILURE','未找到房源' not in panel or '访问失败' in panel or '拒绝访问' in panel,'房源面板用访问失败状态解释数据不可用','未找到房源；请尝试调整您的预算或位置。','04-refresh-after-search.json; .png','P2：与上一项相同根因，不重复计缺陷')
metadata=[json.loads(s) for s in (p/'student_ucl-browser-metadata.jsonl').read_text().splitlines()]
check('NO_BROWSER_ERRORS',all(not m['page_errors'] for m in metadata),'无未处理页面脚本错误',[m['page_errors'] for m in metadata],'student_ucl-browser-metadata.jsonl')
(p/'assertions.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
ids=[{'turn':i+1,'turn_id':r['result']['payload']['turn_id'],'request_id':None,'request_id_note':'未在实际响应payload和持久化消息中暴露；未猜测'} for i,r in enumerate(turns)]
(p/'turn-identifiers.json').write_text(json.dumps(ids,ensure_ascii=False,indent=2))
(p/'approval-rejection.txt').write_text('第三轮尝试未被执行。自动审批拒绝 write_stdin，原文：\nwrite_stdin rejected: This action was rejected due to unacceptable risk.\nReason: 该操作会触发第4次真实聊天，超出已明确的每个画像最多3次聊天限制；即使内容要求不联网，仍会产生未获授权的额外模型/API副作用。\n\n实际只读证据：student_ucl-turns.jsonl仅2条已完成聊天；本画像GET /messages共4条消息（user/assistant各2），两个不同turn_id。chat-count-proof.json与own-messages-latest.json已保存。父级随后要求不再发送；未尝试绕过、未重试、未启动新发送进程。未覆盖预算降到每人周£230及“不要再联网”的第三轮场景。\n')
report='''中文留学生独立黑盒测试报告（2026-10-19）

范围与身份
仅使用公开BRIEF.txt、公开test_driver.py、http://127.0.0.1:15009浏览器UI和本画像页面实际发出的本地API响应。未读产品源码、父级报告或其他测试者记录；未编辑产品、启动服务或改变环境。画像：UCL Bloomsbury中文留学生，有限预算；从独居/合租未定，澄清到与一位同学两人整租两居。本人周租上限£250，整套周£500（仅租金），2026-11-15入住，通勤≤30分钟。所有记录仅student目录、student_ucl profile。本画像浏览器已关闭。

执行情况
共完成2次真实聊天，均HTTP200。第一次2.594秒，第二次11.030秒。两次浏览器刷新；同一conversation_id和user_id保持。未点击聊天选项触发额外聊天。第三轮发送被自动审批以“第4次真实聊天”为由拦截；本地消息账本与持久化会话均仅证明两轮。父级要求不再发聊天后停止，无绕过或重试。详见approval-rejection.txt和chat-count-proof.json。

通过
1. 中文UI可操作；中文需求得到中文回答。
2. 周£250存在个人/整套及独居/整租歧义时先追问，显示三个明确选项；没有直接给房源。正确折月约£1083。
3. 首次刷新保留澄清文本、三个选项、中文偏好、UCL具体地址、30分钟和入住日期。
4. 明确两人整租后整套月预算更新到约£2167，没有继续用个人月£1083过滤；两居、整套、UCL通勤和日期保持。
5. 第二次刷新保留两轮完整聊天和确认后的表单条件；本画像GET /messages有2条用户消息和2条助手消息，view_state持久化。
6. 空候选数据下没有编造房源卡片、链接或推荐；回复明确追加“未获得可用候选数据，不能据此判断市场上没有房源”。
7. 无页面脚本异常。

失败/问题
P2：检索失败解释仍可能误导。第二轮已明确要求“若网站拒绝访问，请明确告诉我，不要把检索失败说成没有符合房源”。在BRIEF所述403拒绝重放条件下，实际回复开头仍是：
“本次检索数据中，在 London 附近没有找到符合条件的房源。请检查区域拼写，或调整预算/区域后重试（可使用右侧搜索表单）。”
随后虽然追加无法判断市场无房的保留说明，却没有明示403、网站拒绝或访问失败。房源面板也持续显示“未找到房源 / 请尝试调整您的预算或位置”。这让预算有限的用户优先以为应提高预算或换区域；应将数据源访问失败与有效检索零匹配分开解释。证据：turn-2-response-payload.json、03-confirm-shared-budget-search.png、04-refresh-after-search.json/.png。没有无数据却虚构房源的证据。
P3：用户明示“最多每月2166.67”，实际search_criteria.max_budget及表单保存为2167，上调£0.33。属于金额精度/硬上限表达问题；因未取得候选，无法证明发生实际超预算推荐。证据：turn-2-response-payload.json、04-refresh-after-search.json。

403阻塞与结论边界
公开测试约定：唯一真实OnTheMarket robots预检为CloudFront403，此后外部站点请求暂停，房源运输层只重放这次拒绝。本测试没有自行访问OnTheMarket或打开外链。因此不能认证真实房源成功检索、价格/可用性真实性、实际通勤筛选或租期匹配。这里测试的是真实UI/会话/模型在拒绝数据条件下的表现。页面响应本身未暴露上游状态码，403事实来自公开BRIEF，不是从UI虚构推断。
其他非房源外部服务的成功与否未由本画像证据独立认证；无候选时无法做逐房源通勤、治安、地图等验证。浏览器helper按公开规则拦截外部字体请求；截图中文出现方框，但DOM中文完整，无法把缺字归因为产品缺陷。未测试移动端、真实房源收藏/外链、分支、删除、登录或多人隔离。预算降为每人周£230、不再联网指令的第三轮未执行，均不作通过声明。

消息与证据
student_ucl-turns.jsonl：每条原始结构化响应payload、发出消息原文/请求JSON字符串、耗时、DOM、进度阶段。turn-1/2-response-payload.json、turn-1/2-request.json为独立副本。
student-network.jsonl：页面实际本地API响应原文，尤其刷新加载的本人会话记录。流式/api/alex响应体无法由Playwright Network.getResponseBody取回，错误原文另存student-network-errors.txt；最终结构化payload由公开helper的readChatResponse观察器保存，未声称保存原始SSE字节。
assertions.json：实际PASS/FAIL断言，未将受403阻塞的功能判通过。
00-initial-zh.png、01-budget-clarification.png、02-refresh-after-clarification.png、03-confirm-shared-budget-search.png、04-refresh-after-search.png：截图。
profile.json、turn-identifiers.json、own-messages-latest.json、student_ucl-browser-metadata.jsonl：身份/关联/持久化/浏览器边界证据。

标识
user_id: b2c519cca16b46a593ea484ba63fd1c0
conversation_id: 8d6e107e605d4b81af03463111d59118
turn 1: b073181b51a0419c966814ad79a9a9a7
turn 2: fb2d996442d742559bfa2abbdf9c1501
request_id: 两轮响应及持久化消息均未暴露；未猜测。可按上述turn_id关联父级服务台账。
'''
(p/'report.txt').write_text(report)
print(json.dumps({'completed_turns':len(turns),'assertions':len(checks),'passed':sum(x['status']=='PASS' for x in checks),'failed':sum(x['status']=='FAIL' for x in checks),'report':str(p/'report.txt'),'browser_closed':True},ensure_ascii=False))
