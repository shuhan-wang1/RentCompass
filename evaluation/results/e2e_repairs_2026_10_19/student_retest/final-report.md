# 中文留学生黑盒复测：候选3最终追加结论

**候选3的新会话长期默认恢复通过；通勤证据在响应与卡片之间仍不一致。** 共完成12次真实聊天，最后2次为本次授权追加。浏览器已关闭，没有继续调用模型。候选1/2的10轮历史完整保留在 [final_report.md](final_report.md)，本报告不追改其失败事实。

## 本次条件与身份

- 仍为原Guest身份：15f470821e334cc68b70d7f959b67421，沿用原浏览器storage，未访问其他用户。
- UI新建第三会话：fe73ac63d23c48bf9bb80c0a86f02257。29-candidate3-new-conversation-state.json/png显示新会话筛选为空。
- 第11轮原文：“我开了一个新会话，继续按我之前已经保存的长期默认帮我找房。请说明本次实际使用的筛选条件，以及有哪些已经核实符合或仍待核实的候选。”
- 此消息未提供预算、卧室、租住类型、学校或通勤数字；请求context仅viewed_properties=[]，未从消息或前端请求传入这些硬条件。

## 逐项结果

| 项目 | 结果 | 实际证据 |
|---|---|---|
| 新会话恢复之前保存的长期默认 | PASS | 第11轮真实search_criteria为London、max_budget=2400/月、bedrooms=2、room_type=whole、commute_destination=Gower Street, London WC1E 6BT（UCL）、max_travel_time=30；字段来源为saved_preference、memory_id=semantic_0d7f04738ad93d97f5c671870971093a。30原始响应、candidate3-validation.json。 |
| UI即时应用筛选并显示卡片 | PASS | 第11轮后未刷新，表单2400/2/whole/Gower Street/30，Barrowgate备选卡片立即出现。30截图/DOM。此前候选1新会话首搜卡片空的问题在本次新建会话路径没有重现。 |
| 不合格候选不标为符合 | PASS | Barrowgate £2450超预算2400，实际recommendations中32分钟超30上限，candidate_status=excluded；文本和卡片均未标为符合。 |
| 通勤证据在响应与视图一致 | FAIL | 同一第11轮recommendations.verified_commute_minutes=32，candidate_reasons含32>30；view_state.results里该字段丢失，candidate_unknown_reasons改为commute evidence is missing。卡片显示缺少通勤证据，正文却说已核实32分钟。刷新前后均矛盾。 |
| 第11轮刷新持久化 | PASS | 新会话messages API刷新前后完全一致；表单及卡片保留。31截图与candidate3-turn11-before/after-refresh-messages-api.json。持久化保留了上述证据矛盾，不能将保留状态等同于内容正确。 |
| 第12轮只读追问后保留条件与结果 | PASS（状态） | 仅问正文/卡片通勤证据为何矛盾，未重给条件。response_type=chat；筛选字段与第11轮完全相同，备选仍excluded，收藏仍1。32截图。后台是否完全没有新搜索由协调者工具账本补证。 |
| 第12轮证据解释可靠性 | 未通过 | 回答改称“以卡片为准、通勤未核实”，并称路线不是该备选的起点。第11轮已发出的结构化recommendations却有该房源verified_commute_minutes=32，正文也列出Barrowgate→Gower Street路线；本次口头撤回无法解释同一响应两份结构数据为何矛盾，不能据此关闭缺陷。 |
| 最终刷新、收藏、身份 | PASS | 第12轮messages API刷新前后完全相同，全部表单字段不变；收藏与最初重启前完全相同；身份未变。33最终刷新及candidate3-validation.json。 |

## 仍需处理的具体缺陷

第11轮一个响应同时携带两种通勤证据状态：

```json
{
  "recommendations[0]": {
    "address": "Barrowgate Road, London W4",
    "verified_commute_minutes": 32,
    "candidate_status": "excluded",
    "candidate_reasons": ["...", "measured commute 32 min exceeds limit 30 min"],
    "candidate_unknown_reasons": []
  },
  "view_state.results[0]": {
    "address": "Barrowgate Road, London W4",
    "verified_commute_minutes": null,
    "candidate_status": "excluded",
    "candidate_unknown_reasons": ["commute evidence is missing"]
  }
}
```

view_state中实际省略verified_commute_minutes；上面用null表示无值。UI跟随view_state显示“缺少该房源的通勤证据”，正文仍说“经核实通勤为32分钟”。这不会把本次超预算备选误标为合格，但会使用户无法判断已经查过的路线是否可信。证据在30-turn11-candidate3-defaults-no-restatement-network.json（完整SSE）、同名state/png、31刷新、32只读追问。未读取实现，因此不推断丢失位置。

## 保留的候选2失败历史

候选2第8轮在之前创建的新会话51817b80f9ea41649667074bee9a3b7f，明确要求按已保存长期默认继续时，预算/卧室/租住类型/最长通勤仍为空；第9轮重述需求后正确不能追溯视为第8轮恢复成功。该历史记录仍在21证据和final_report.md中。

候选3第11轮是在全新会话、不重述任何条件的独立成功，因此只支持本次新会话默认应用修复有效。未重放所有历史路径，未测试临时默认覆盖作用域，也未复测保存确认文本截断。

## 新增导出标识

| 轮次 | turn_id | HTTP x-request-id |
|---|---|---|
| 11 | d771d4cce1b545e591f0af65e6eaeb1c | dc5c75a2347a4689bc9bde2130100ba4 |
| 12 | dd360e324233473caefab6ebbcb50337 | 5d7901f2162a487c9d4e072a07b7bec9 |

机器可读标识：candidate3-turn-identifiers.json；验证摘要：candidate3-validation.json；所有12轮原始请求与解析响应：student-turns.jsonl；第11/12轮完整SSE：30/32对应network.json。

房源、天气、路线均为题设历史快照/受控合成HTTP夹具。本次是多会话、刷新、真实容器重启的有限模拟，不代表真实数周、真实线路或真实市场验证。继续拦截所有非本地浏览器资源，未打开房源外链或访问生产端口。
