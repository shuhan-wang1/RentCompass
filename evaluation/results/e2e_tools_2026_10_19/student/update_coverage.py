import json,pathlib,collections
f=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/student')
d=json.loads((f/'dispatch-excerpt.json').read_text());rs=[json.loads(s)for s in(f/'student-turns.jsonl').read_text().splitlines()]
matched=[];all_counts=collections.Counter();dispatch_counts=collections.Counter()
for r,t in zip(d,rs):
 cid=(t.get('result')or{}).get('payload',{}).get('conversation_id')
 l=[x for x in r.get('completed_turn_ledger',[])if cid and x.get('conversation_id')==cid]
 matched.append({'turn':r['turn'],'completed_turn_ledger':l})
 for x in l:all_counts.update({k:v['count']for k,v in x.get('tool_latency',{}).items()})
 dispatch_counts.update(x['tool']for x in r['tool_dispatches'])
assert sum(len(x['completed_turn_ledger'])for x in matched)==13
assert all_counts['ask_user']==1
(f/'completed-turn-ledger-matched.json').write_text(json.dumps(matched,ensure_ascii=False,indent=2))
out={'evidence_basis':'Matching response conversation_id completed executor canary ledgers plus per-tool Tool.execute dispatch; terminal ask_user bypasses Tool.execute.','matched_completed_turns':13,'executor_observed_counts':dict(all_counts),'tool_execute_observed_counts':dict(dispatch_counts),'ask_user':{'called':True,'turns':[14],'path':'executor terminal artifact, canary tool_latency.ask_user.count=1','note':'公开响应是澄清；UI仅A/B/C纯文本，没有可点选择控件。第1轮只有自然语言澄清，无工具。'},'recall_memory':{'called':False,'tested_turns':[12],'note':'新会话能复述已保存偏好，Tool.execute与完整executor ledger均无显式recall_memory；无法黑盒确认上下文注入机制。'},'multi_tool_chain':{'turn':13,'sequence':[x['tool']for x in sorted(d[12]['tool_dispatches'],key=lambda x:x['started'])],'note':'搜索后查最便宜详情实际触发；search args丢失预算/卧室/整租。'}}
(f/'tool-coverage.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
p=f/'REPORT.txt';s=p.read_text();start=s.index('实际工具触发（');end=s.index('重启恢复：等待通知。',start)
s=s[:start]+'实际工具触发（匹配本人响应conversation_id的13个完成executor账本与Tool.execute，非助手自述；429无执行账本）：\n'+'\n'.join(f'{k}: {v}次'for k,v in all_counts.items())+'\nask_user第14轮确实由执行器终止路径触发，不经过Tool.execute；ledger.tool_latency.ask_user.count=1。第1轮只有自然语言澄清。可点选项仍未渲染，见STU-05。\nremember第11轮真实成功存储。第12轮复述正确，但Tool.execute及完整executor账本均无recall_memory，机制不能由本次黑盒证据确认。\n第13轮实际组合链：search_nearby_pois + search_properties(area=Watford, commute_destination=UCL, limit=3，预算/卧室/room_type未传)后get_property_details(Cassio URL)。工具调用成功，但错误条件导致选择不合偏好的房源。第4轮details+POI、第10轮两次details也确实执行。\n详细工具args/输出：dispatch-excerpt.json；匹配后的本人账本：completed-turn-ledger-matched.json；摘要：tool-coverage.json。\n无聊天恢复脚本：recovery_check.py，待服务重启通知后执行，阻断任何/api/alex请求。\n'+s[end:]
p.write_text(s)
print(json.dumps({'matching_completed_turns':13,'executor_counts':dict(all_counts)},ensure_ascii=False))
