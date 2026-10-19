# RentCompass 独立黑盒测试：英文工作通勤画像

结论：历史、收藏、登录身份和明确保存的最新偏好能经浏览器关闭及一次实际服务重启恢复；但候选资格和搜索状态仍不可靠。整租一居被解析成Studio、合租被列为“全部条件已验证”、房源引用价格覆盖预算，足以干扰上班族的租金与通勤总成本决策。

这是一次独立黑盒测试。未读取产品源码、先前对话背景、其他画像报告、私有harness/runtime或旧审计。只使用公开brief/snapshot/helper、网页交付内容及本人身份的本地HTTP响应。所有产物仅写入本目录。

## 测试范围与数量

- 26个实际英文自然对话回合，另1次真实可见表单搜索；共27次用户—助手交互。四个有实质聊天的对话、四次加速使用会话；C1在S1内连续12轮自然追问。
- 自然对话窗口：2026-10-19T05:09:54+00:00 至 2026-10-19T05:22:46+00:00，约12.87分钟。不是数周实测。
- 桌面Chromium 1440×960、手机视口390×844，均完成真实聊天；手机还完成历史与收藏浏览。多次关闭重开保留storage_state。
- 真实DeepSeek通过正常应用路由；26个自然聊天HTTP均200，驱动无聊天超时，未遇429。中位延迟5.991秒、最大28.183秒。200只说明HTTP完成，不代表任务成功。
- T22后安全闲置请求实际停止/启动服务，等待父agent明确健康确认后才继续；重启结果由网页独立核实。

会话和逐回合矩阵见[SESSION_MATRIX.md](SESSION_MATRIX.md)，完整英文会话见[TRANSCRIPT.md](../../../../.runtime-persona-audit-2026-10-19/professional/TRANSCRIPT.md)，结构化证据见[transcript.json](../../../../.runtime-persona-audit-2026-10-19/professional/transcript.json)。

## 已观察的产品缺陷

### PRO-01 · P1 · 否定房型被写成要选的房型，明确一居要求连续修正后仍是 Studio

预期：whole one-bedroom flat, not a room or studio 应设置1居并排除房间/Studio；后续明确修正应覆盖错误条件。

实际：T01 bedrooms=0、room_type=studio；T02明确 exactly 1仍0/studio并额外把办公站名的一部分Liverpool加入住宅地区；T03 bedrooms=1、room type=Any仍0/studio。T09明确exclude shared rooms却room_type=shared。

影响：普通英文整租用户被送往错误房型，且自然语言纠错不能可靠恢复。

复现：使用C1的T01原文开始伦敦三区一居搜索。 → 按T02、T03依次纠正Bedrooms和Room type。 → 核对响应search_criteria、可见下拉框及Studio结果。 → T03后用可见表单手动改Bedrooms=1、Room type=Any再搜索，可返回多项一居，证明恢复路径存在。

证据与重复性：T01, T02, T03, T09；同一独立对话内连续3轮观测；T09另见否定shared反向写入。未对每种文案做多次全新对话重复，因此不宣称固定失败率。

关键文件：[s1-t01-negated-studio.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t01-negated-studio.png), [s1-ui-onebed-results.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-ui-onebed-results.png)

### PRO-02 · P1 · 明确要求self-contained flats，却将4套house share列为全部条件已验证

预期：整租一居搜索应排除明确house share的条目，或至少标为不满足/未验证。

实际：重启后的T25要求1-bedroom self-contained flats并flag room shares，响应却在Meets all declared conditions (verified)下列Cassio Road £625、Harwoods Road £695、Princess Avenue £725、Princes Avenue £825。返回条目的explanation均明确1 bedroom in a house share；search_criteria.property_features=[]、room_type=null。

影响：低价合租被包装成已验证整租候选，使多区域预算比较失真。

复现：先完成T22保存Watford整租偏好，重启后T23在新对话回忆。 → 执行T24 Cambridge试查后执行T25原文回到Watford/£1500。 → 对照Meets all declared conditions段落与各卡片house share标题。

证据与重复性：T18, T23, T25；该完整回访路径观测1次；T18曾正确说明同类房源是合租，因此表现不一致而非所有回答均无法区分。

关键文件：[s4-t25-back-to-watford-1500.png](../../../../.runtime-persona-audit-2026-10-19/professional/s4-t25-back-to-watford-1500.png), [s4-t25-back-to-watford-1500.txt](../../../../.runtime-persona-audit-2026-10-19/professional/s4-t25-back-to-watford-1500.txt)

### PRO-03 · P1 · 引用候选价格会覆盖明确要求保留的真实预算

预期：房源价格引用与用户最高租金应分开；T19明确保留£1800/month就应保持1800。

实际：T18上限1800；T19引用Sydney1495与Flanders1350并明确Keep the £1,800 monthly ceiling，响应max_budget却1495，同时room_type=shared、areas缩为Watford。C1的£2000上限在价格/交通比较及身份追问之后也变7362，T05截图可见。

影响：用户无意间扩大或收紧预算，后续结果集与长期偏好发生漂移。

复现：以T18得到Watford/High Barnet、£1800搜索。 → 发送T19完整原文，引用1495和1350并要求保留1800。 → 检查返回search_criteria.max_budget=1495。 → 辅助路径按C1 T01–T05及UI01执行，T05表单预算可见7362而非2000。

证据与重复性：T04, T05, T18, T19；在两个不同身份/对话中观察到预算被引用数字影响；T19定位明确。C1仅能定位发生于比较/追问链，未证明具体内部换算来源。

关键文件：[s1-t05-question-mutates-results.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t05-question-mutates-results.png), [s3-t19-output.jsonl](../../../../.runtime-persona-audit-2026-10-19/professional/s3-t19-output.jsonl), [s3-t19-whole-flat-refinement.png](../../../../.runtime-persona-audit-2026-10-19/professional/s3-t19-whole-flat-refinement.png)

### PRO-04 · P2 · 房型身份询问被当成筛选命令，尽管用户明确说不要改条件

预期：回答Otter Close是整租还是合租，并保留已有5个结果。

实际：T05询问Is Otter Close a whole one-bedroom flat or a room in a shared flat?并说Please don’t change my search criteria，系统却kept only shared listings，删4项留1项。T06承认误把问题当筛选，但未恢复原列表。

影响：正常事实核实会破坏候选集，需要用户重新搜索。

复现：先用UI01得到5个一居条目。 → 发送T05原文。 → 观察共享房筛选与4个候选被移除；再用T06请求恢复。

证据与重复性：T05, T06；该事实追问完整路径观测1次；后续T06明确承认行为错误。

关键文件：[s1-t05-question-mutates-results.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t05-question-mutates-results.png)

### PRO-05 · P2 · 通勤无法验证的兜底答复覆盖无关的房源费用与纯算术问题

预期：无法验证交通时仍可回答已知房源字段、标记未知，或完成用户给定的算术。

实际：T07要求读取Canning Road费用字段、T08明确Separate calculation, not a search change询问£400/week×52/12及5周押金，均只返回The commute condition could not be verified for the listing this round。此前T05已显示no_commute=true。

影响：一个不可用数据源阻断与它无关的正常工作流。

复现：按C1 T01–T06及UI01达到现有状态。 → 依次发送T07与T08原文。 → 核对不同问题收到相同通勤兜底句，而非未知字段或£1733.33/month、£2000押金计算。

证据与重复性：T04, T07, T08, T11；同一对话T04/T07/T08多次出现；T09重设偏好后T10/T11可恢复有意义回答。交通提供方不可用属环境限制，但无法解释无关算术也失效。

关键文件：[s1-t12-long-context-summary.txt](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t12-long-context-summary.txt)

### PRO-06 · P2 · 不改搜索的地区比较追问清空搜索条件，回答却称未改条件

预期：Manchester/Fallowfield、£1400、1卧、无通勤保持不变。

实际：T17问题明确without changing the search；正文说I have not changed your search criteria，却返回search_criteria={}。手机表单地区、预算、卧室等字段变空，结果仍是原来两套。

影响：用户对当前搜索状态产生误判，后续回访或点击Search可能丢失真实条件。

复现：T15创建Manchester/Fallowfield一居、£1200对话。 → T16提高到£1400得到两套。 → T17只询问哪些在Fallowfield及样本能否支持地区比较。 → 检查空criteria响应和表单空值。

证据与重复性：T16, T17；该路径观测1次；已保留真实表单值与截图。

关键文件：[s2-t17-criteria-cleared.png](../../../../.runtime-persona-audit-2026-10-19/professional/s2-t17-criteria-cleared.png), [s2-t17-criteria-cleared.txt](../../../../.runtime-persona-audit-2026-10-19/professional/s2-t17-criteria-cleared.txt), [mechanical-actions.jsonl](../../../../.runtime-persona-audit-2026-10-19/professional/mechanical-actions.jsonl)

### PRO-07 · P2 · 12轮总结将算术示例当预算，并虚构£1699/year导致不必要冲突

预期：识别T09明确替换为£1700/month、T11为独立假设；T12又明确real £1700 ceiling，应能直接总结。

实际：T12称预算分别是£400/week和£1699/year，用户尚未指定哪个覆盖哪个，因此拒绝确定1700。前者来自明确独立算术题，后者从未作为annual预算提出。地区缩小与Canning Road身份则记对。

影响：长对话中用户已明确的预算仍被反复要求澄清，降低长期信任。

复现：完整执行C1 T01–T12及UI01，特别保留T08独立算术、T09新实际预算、T11假设例子。 → 发送T12总结原文。 → 对照答复的annual预算与所有真实用户输入。

证据与重复性：T08, T09, T11, T12, T13；在一条12轮连续自然对话中观测1次；后续T13明确修正后可恢复，不能据此认定所有持久记忆失效。

关键文件：[s1-t12-long-context-summary.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t12-long-context-summary.png)

### PRO-08 · P3 · 英文界面的超预算状态残留中文

预期：EN界面和英语对话中状态标签用英文。

实际：T01卡片显示超预算 £300；Manchester T15卡片显示超预算 £150与£155。普通Within Budget标签又是英文，形成混杂。

影响：以英文为主的用户不易理解关键预算告警。

复现：EN界面执行T01或T15，返回预算外替代项。 → 查看卡片上的预算状态。

证据与重复性：T01, T15；跨伦敦和Manchester两次独立搜索均可见。

关键文件：[s1-t01-negated-studio.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t01-negated-studio.png), [s1-t01-negated-studio.txt](../../../../.runtime-persona-audit-2026-10-19/professional/s1-t01-negated-studio.txt)

## 设计问题

- **PRO-D01 / P2：过期数据提示未持续出现在浏览和收藏卡片中。** 原始推荐possibly_outdated=true，卡片及Saved视图只见租金、Within Budget、Contact agent等，截图无过期/快照提示。T18回答有明确possibly outdated说明，说明提示主要依赖某次聊天正文。 回访只看收藏时容易把旧价格和可租性当作已核实；本测试不证明真实房源已下架。 证据：[s1-saved-canning.png](../../../../.runtime-persona-audit-2026-10-19/professional/s1-saved-canning.png), [s2-mobile-return-conversation.png](../../../../.runtime-persona-audit-2026-10-19/professional/s2-mobile-return-conversation.png), [s4-after-restart-saved.png](../../../../.runtime-persona-audit-2026-10-19/professional/s4-after-restart-saved.png)

- **PRO-D02 / P3：不可验证通勤费用时仅给通用重试，缺少用户要求的预算替代步骤。** T21已明确要求useful way to budget，等待28.183秒后只得到没有可靠数据、稍后重试或改写问题。T11/T26证明使用用户假设完成公式本身可以帮助。 用户不知道如何继续比较租金加通勤总成本。 证据：[s3-t21-commute-unknown.png](../../../../.runtime-persona-audit-2026-10-19/professional/s3-t21-commute-unknown.png)

## 成功项与长期使用检查

- **PASS-01** 可见表单可修正Bedrooms=1、Room type=Any，并返回5项伦敦候选；地区chips可表达多区域。
- **PASS-02** 新Manchester对话的£1200、1卧、无通勤不携带London地区；提高到1400后返回1350与1355两套。
- **PASS-03** T17地区解释明确这两套并非Fallowfield，样本不足以比较地区，不编造Fallowfield价格；但筛选状态被清空另列缺陷。
- **PASS-04** Ask AI选择Sydney焦点后，T20以精确URL匹配地址和£1495月租，押金、税档、账单未知均明示。
- **PASS-05** 用户给定通勤成本的两次算术正确：1699+6×13=1777、余223；1495+12×9=1603、余147，明确区分真实租金上限。
- **PASS-06** Guest C1历史及Canning收藏在浏览器关闭、手机重开后恢复；真实服务重启后原Guest仍只见自己的两段历史及Canning。
- **PASS-07** 注册虚构账户后历史/收藏为空；退出后新Guest为空，登录后账户历史及Sydney收藏恢复；对话重命名成功。
- **PASS-08** 实际服务停止/启动后账户仍认证，Watford working plan的10条消息与Sydney1495收藏恢复。
- **PASS-09** 重启后C4新对话正确回忆T22最新1600pcm、Watford only、self-contained1bed、无硬通勤过滤，并说明早期1800已被替换。Sydney也被正确识别；该候选同时存在浏览器viewed_properties上下文，所以不能单独证明其来自服务端收藏工具。
- **PASS-10** Cambridge缺缓存时明确不等于市场无房，没有静默替换其他城市，也未宣称实时可租。
- **PASS-11** 独立新Guest历史和收藏为空，对本人另一账户C3与原Guest C1的已知对话ID请求均404；未接触其他测试员数据。
- **PASS-12** 桌面1440×960和手机390×844均完成真实聊天与保存/历史操作；26次自然对话HTTP均200、驱动均无超时，未触发429。不能把200等同于任务成功。

重启前后独立检查：同一账户认证有效，`Watford working plan`仍为同一个C3、10条消息、同一Sydney Road £1495收藏；原Guest随后也恢复其独有的C1/C2和Canning Road收藏。新Guest对这两个本人已知的其他身份对话ID均404。证据：[重启前基线](../../../../.runtime-persona-audit-2026-10-19/professional/s3-before-restart-account-baseline.png)、[重启后收藏](../../../../.runtime-persona-audit-2026-10-19/professional/s4-after-restart-saved.png)、[身份HTTP检查](../../../../.runtime-persona-audit-2026-10-19/professional/identity-output.jsonl)。

T22说“saved to memory”之后，服务实际重启并在C4新对话发起无预算提示的召回，T23正确识别1600/月、Watford-only、整租一居、无硬通勤过滤，并明确1800为旧偏好。这是保存与回访方面的实证成功。候选地址也正确，但请求中有Sydney的viewed_properties上下文，不能单凭它证明服务端收藏检索的来源。

## 环境限制（不算产品缺陷）

- **ENV-01** 883行历史公开房源快照，33个地区；Watford原抓取2026-10-06，多数其他地区2026-07至09。不是实时租赁市场；Cambridge无快照覆盖。
- **ENV-02** 应用/浏览器外网被限制，外部图片、字体、地图、实时交通/犯罪/周边查询可能不可用。空图片和无法验证现时交通本身不计产品缺陷。未访问外部网页、房源链接、图片或地图。
- **ENV-03** 执行环境bwrap loopback失败，按公开brief使用require_escalated运行本地工具。一次持久控制器输入/等待故障、一次自己eval脚本语法错误记为测试基础设施问题，改用普通阶段脚本后继续；不归因产品。
- **ENV-04** 真实配置DeepSeek调用走应用正常路径；未检查模型内部、私有harness、产品源代码、其他画像资料或旧报告。公开交付页面HTML/JS只用于识别UI控件。

## 未验证及不能证明的部分

- **UNV-01** 没有经过真实数周/数月；不能证明真实长期使用的记忆衰退、自动日报、提醒、后台定时任务或每日更新行为。四会话是约13分钟内的加速模拟。
- **UNV-02** 未验证真实房源现时价格、可租性、通勤时间/票价、治安、周边、押金与法律费用准确性；未联系中介、付款或提交真实个人数据。
- **UNV-03** 新Guest/API404和基本退出登录隔离不构成完整安全审计；未测试认证绕过、会话劫持、密码恢复、用户名枚举、其他真实账户或其他测试员数据。
- **UNV-04** 没有负载、429恢复、取消途中请求、断网重试、并发聊天、浏览器真实移动系统键盘、更多设备或广泛无障碍测试。移动验证是Chromium390×844视口。
- **UNV-05** 仅黑盒行为，未证明根因在规则解析、记忆写入、工具裁剪或LLM；没有对每条提示做多次全新身份复跑，不估计全体用户失败率。

## 长期使用判断

保存层和明确写入的最新记忆在浏览器重开、登录切换及一次实际服务重启后表现良好，但自然语言搜索状态在连续使用中不稳定，整租资格/预算容易漂移或被错误宣称已验证。目前适合作为需要用户核对的发现与整理工具；尚不足以让重视总成本的上班族长期独立依赖其候选资格和筛选状态。

保存正确与筛选正确必须分别看待：本次能证明既有历史/收藏和一条明确最新偏好在重启后仍可使用，也能证明连续自然追问会让预算、房型和可见条件漂移。不能由一次12.87分钟的加速测试推出真实多周稳定性、提醒有效或实时数据质量。

## 证据索引

- [findings.json](findings.json)：8项产品缺陷、2项设计问题，含预期/实际、影响、复现、证据、严重性和重复性限制。
- [primary-turns.jsonl](../../../../.runtime-persona-audit-2026-10-19/professional/primary-turns.jsonl)：26次真实UI发送的原始输入、请求body、SSE、turn_id、耗时、累计DOM。
- [mechanical-actions.jsonl](../../../../.runtime-persona-audit-2026-10-19/professional/mechanical-actions.jsonl)：UI点击、表单值、账户、收藏、列表与本地HTTP检查；T18起补充X-Request-ID。
- [transcript.json](../../../../.runtime-persona-audit-2026-10-19/professional/transcript.json)、[TRANSCRIPT.md](../../../../.runtime-persona-audit-2026-10-19/professional/TRANSCRIPT.md)：规范化逐回合记录及UI01完成数据。
- [SESSION_MATRIX.md](SESSION_MATRIX.md)、[metrics.json](../../../../.runtime-persona-audit-2026-10-19/professional/metrics.json)：会话矩阵、数量与时间。
- [restart-confirmation.json](../../../../.runtime-persona-audit-2026-10-19/professional/restart-confirmation.json)：停启请求与健康确认边界；自身网页复核在s4-restart-check-output.jsonl。
- `*-storage.json`为本人测试浏览器身份状态，`*-actions.jsonl`与`browser_worker.py`为复现实验脚本；虚构账户凭据仅为测试数据，不需在摘要中公开。
- `infrastructure-errors.jsonl`记录测试驱动自身错误；首个shell环境错误与控制器取消在OBSERVATIONS.md说明，不计产品缺陷。
