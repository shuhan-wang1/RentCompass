import pathlib,json,statistics,datetime
f=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/student')
rs=[json.loads(s)for s in(f/'student-turns.jsonl').read_text().splitlines()]
issues=[
 {'id':'STU-01','severity':'P1','title':'明确的Watford范围和整租约束在首次搜索中丢失','turns':[2,3],'expected':'按Watford、整租、恰好1卧、1800/月搜索；若存在歧义先澄清。','actual':'第2轮search_properties.area=London，未传room_type；推荐Chigwell与London E6并宣称满足全部已声明条件。第3轮用户纠正后才返回5套Watford整租。','repro':'新会话说明在伦敦UCL读研、先看Watford、1800/月、整套一卧、不要合租。','evidence':['transcript-sanitized.json:turn2/3','dispatch-excerpt.json:turn2/3','02-watford-lost.png'],'confidence':'confirmed'},
 {'id':'STU-02','severity':'P1','title':'将明确的整套总预算2400误读成每人预算1200','turns':[6,8],'expected':'两人租整套恰好2卧，总预算2400/月，每人1200；搜索上限应为2400。','actual':'第6轮max_budget=1200，返回无匹配；第8轮只重申整套总预算2400后返回10套1600–2250/月的2卧整租。','repro':'先设Watford一卧，再输入“整套恰好两卧室，总租金最高£2400/月，也就是每人最多£1200”。','evidence':['transcript-sanitized.json:turn6/8','06-total-budget-misread.png'],'confidence':'confirmed'},
 {'id':'STU-03','severity':'P1','title':'跨会话能复述长期记忆，却未把它应用为搜索条件','turns':[11,12,13],'expected':'按已复述Watford、最高1800/月、整租、恰好1卧搜索；合租与studio应排除。','actual':'第12轮正确复述；第13轮“按你刚复述的长期默认条件”实际max_budget=null、bedrooms=null、room_type=null。返回Cassio合租625、Upton Studio850与North Approach一卧1100，全部eligible；回答先称三套符合，后承认Cassio不符，却把Studio称为真正符合。最便宜详情因此选到不符合偏好的合租。','repro':'第11轮保存长期偏好并明确之前合租/两卧只是临时方案，新会话先复述再要求按该偏好找三套并查最便宜详情。','evidence':['transcript-sanitized.json:turn11/12/13','13-memory-ignored-search.png'],'confidence':'confirmed'},
 {'id':'STU-04','severity':'P2','title':'澄清整租还是合租前，结构化条件已经替用户选择合租','turns':[14],'expected':'新同学的房型尚未确认，先问问题；room_type保持未知。','actual':'回答确实询问A整套/B合租/C不确定，但同一响应的view_state.criteria.room_type已为shared，右侧房型也选中合租；constraint_sources把询问本身当作用户选择。没有后续请求，未验证其下游影响。','repro':'要求为偏好不同的新同学先别搜，一次问一题，先确认整租还是合租。','evidence':['transcript-sanitized.json:turn14','student-ui-actions.jsonl:final criteriaFields observation','14-clarification-options.png'],'confidence':'confirmed'},
 {'id':'STU-05','severity':'P2','title':'请求可点击选项，回答只有纯文本A/B/C','turns':[14],'expected':'用户明确请求可以点击选择的选项，应提供可交互选择控件。','actual':'聊天只渲染一段A/B/C文字；聊天DOM中只有编辑消息和分支按钮，没有选择按钮。实际ask_user触发状态见工具证据摘要。','repro':'同STU-04，明确请求“给出可以点击选择的选项”。','evidence':['transcript-sanitized.json:turn14','student-ui-actions.jsonl:optionButtons','14-clarification-options.png'],'confidence':'confirmed'},
 {'id':'STU-06','severity':'P2','title':'HTTP429被错误提示成连接错误','turns':[7],'expected':'明确提示请求频繁并给出稍后重试说明，保留服务可用状态。','actual':'服务0.322秒返回429及too many requests，中文UI同时显示“连接错误，请检查服务器。”与重试。触发来自同IP的根测试探针与访客共享限流桶，不能据此声称单用户7轮就被限流。等待窗口后第8轮成功。','repro':'限流窗口内触发429，观察前端错误提示。','evidence':['transcript-sanitized.json:turn7','07-rate-limit-error.png'],'confidence':'confirmed'},
 {'id':'STU-07','severity':'P3','title':'中文答复混入内部英文判定文本与不相关通勤核验脚注','turns':[1,2,5,8,10,11],'expected':'中文界面提供用户可理解且与本轮相关的说明。','actual':'第1轮没有房源仍称部分房源通勤未核实；第2/5/8轮出现alternative is outside the declared hard result set及monthly price...；第10轮末尾出现英文价格复核提示；第11轮给通勤参考后又出现移除通勤数据说明。','repro':'使用中文进行缺条件澄清、预算变更和详情比较。','evidence':['transcript-sanitized.json:turn1/2/5/8/10/11'],'confidence':'confirmed'},
 {'id':'STU-08','severity':'P2 candidate','title':'联系中介的能力表述前后不一致','turns':[4,13],'expected':'明确说明能否实际联系中介，避免在无执行入口时承诺外部动作。','actual':'第4轮称无法替用户联系或预订；第13轮却主动说可以帮你联系中介核实。测试未请求执行联系，也未观察到联系工具，因此这是承诺一致性问题，不能据此证明真实发送失败。','repro':'先问房源详情，之后跨会话按偏好搜索并查最便宜房源。','evidence':['transcript-sanitized.json:turn4/13'],'confidence':'observed wording; execution untested'}
]
(f/'findings.json').write_text(json.dumps(issues,ensure_ascii=False,indent=2))
titles=[
'首次进入、中文切换、缺条件澄清：自然语言问学校/预算/房型/通勤；无搜索。',
'Watford整租1卧1800搜索：错误变成London且房型未知，STU-01。',
'纠正范围/房型：5套Watford整租1卧，正确。',
'第二套Flanders Court详情+附近超市：指代正确，家具/账单/押金未知，OSM为合成夹具。',
'预算降到1300：仅1100的North Approach符合，1350备选明确排除。',
'改2卧整租，总2400/每人1200：上限错误设1200，无结果，STU-02。',
'纠正总预算：HTTP429；计入请求预算；错误提示STU-06。',
'等待窗口后纠正总2400：10套2卧整租1600–2250；上限恢复正确。',
'改个人合租单间1200：bedrooms清空、room_type=shared，4套625–825，正确。',
'纯指代“第二套和最便宜”详情比较：Harwoods与Cassio正确，695-625=70/月，未知项明确。',
'要求长期记住Watford1800整租1卧，区分临时方案：口头确认正确；额外给交通与超市夹具参考。',
'新会话复述长期偏好：正确，未搜索。',
'按刚复述记忆搜索3套并查最便宜详情：预算/卧室/整租全丢，混入合租与studio，STU-03。',
'新同学缺条件、请求可点选项：有文字澄清，无选择控件，且提前保存shared，STU-04/05。']
lines=['RentCompass 独立中文留学生端到端审计','日期：2026-10-19；本地服务：http://127.0.0.1:15006','身份：独立Guest；桌面1440×960；仅使用自己的browser storage。','范围：14次真实UI聊天请求，13次HTTP200、1次HTTP429；成功请求中位响应5.432秒、最长11.929秒。没有通过API额外发送聊天，没有访问外部网站或直接调用模型API。','隔离：仅阅读公共BRIEF/驱动和根测试员提供的本人dispatch摘录；未查看旧报告、其他persona证据、修复记录或产品代码。只在student目录写入脚本/证据/报告。','', '结果判断：核心搜索约束与记忆应用仍不可靠。三个P1分别涉及地点/房型丢失、总预算误读和长期偏好未应用；收藏与导出路径可用。','', '逐轮记录：']
for n,(r,t) in enumerate(zip(rs,titles),1):
 p=(r.get('result')or{}).get('payload')or{}
 lines += [f'{n:02d}. HTTP {r["result"]["status"]}，{r["seconds"]}秒；{t}',f'    turn_id={p.get("turn_id","none")}；conversation_id={p.get("conversation_id","none")}']
lines += ['', '浏览器流程及正向证据：','1. 首次默认英文，可点击中文切换；DOM中文正常。测试浏览器缺少可用CJK字形，截图中部分汉字为方框；字体和外部资源限制属于审计环境，不单列为产品缺陷。','2. 第4轮后点击Flanders Court收藏，收藏数0→1；收藏夹显示地址、1350/月、来源会话与收藏时预算1800。刷新后仍为1。关闭浏览器、保存storage并重开后仍恢复2? 不应误读：此时仅1个原会话，后来创建第2个新会话；收藏始终1。','3. 新会话聊天为空，收藏仍可见，含原来源会话；切回原会话后聊天和搜索恢复。','4. 登录/注册框中的“下载访客研究备份”真实触发下载。export-rentcompass-guest-research.json可解析，顶层exported_at/conversations/favorites；下载时有2个会话（一个空、一个12条消息）和1个收藏，含Flanders Court及第一条用户消息；顶层无cookie/password/token字段。未注册或登录，未验证访客迁移。','5. 详情链接指向OnTheMarket，为遵守约束未点击；详情核验通过本地聊天工具完成。外部图片/字体请求全部由浏览器路由阻断，不将加载失败归为生产故障。','6. 两次浏览器运行没有pageerror。已保存storage，等待服务重启后的无模型恢复检查。','', '问题明细（P1=影响核心找房结果；P2=重要交互/状态问题；P3=文案/一致性；candidate=未验证执行层）：']
for i in issues:
 lines += [f'{i["id"]} [{i["severity"]}] {i["title"]}',f'  复现：{i["repro"]}',f'  预期：{i["expected"]}',f'  实际：{i["actual"]}',f'  证据：{", ".join(i["evidence"])}；轮次{i["turns"]}；置信度{i["confidence"]}']
lines += ['', '合成数据与长期模拟的限制：','房源来自固定历史快照；只验证固定数据下的工具调用、约束传递、计算和UI状态，不代表2026年真实在租或可入住。OSM/TfL/天气/警方/网页检索为HTTP边界合成夹具，本人只实际使用OSM/TfL相关路径。Tesco130米、营业时间、32分钟路线都不能当真实实况。原工具结果中的[audit fixture]/SYNTHETIC文字未在最终答复保留，作为夹具溯源观察，不单独归类为生产数据准确性缺陷。','本次只模拟同一访客、同一机器、约数分钟到十几分钟内的两会话、刷新和浏览器重开。服务重启检查另附。未验证数周后的记忆保留、跨设备、账户迁移、登录/退出、实际中介联系、真实TfL/警方/OSM准确率、真实看房/租赁、外部图片呈现、收藏取消与冲突同步。','14请求限制已用满，不能再通过真实聊天复验失败修复或重启后的recall工具；恢复阶段仅做浏览器UI/本地读操作。','', '证据安全：','对外分享优先REPORT.txt、findings.json、transcript-sanitized.json、dispatch摘录与指定截图。student-storage.json与原始student-turns.jsonl可能含浏览器身份/请求头，保留为私有恢复证据，不放入公开报告包。','', '实际工具触发：待根测试员完成仅本人14轮dispatch摘录后，tool-coverage.json及本节更新。','重启恢复：等待通知。']
text='\n'.join(lines).replace('重开后仍恢复2? 不应误读：此时仅1个原会话，后来创建第2个新会话；收藏始终1。','重开后原会话与1个收藏恢复；后来创建第2个新会话，收藏始终1。')
(f/'REPORT.txt').write_text(text)
