import json
from rag.agent_memory import get_agent_memory
m=get_agent_memory();uid="e1f9ab170dce43d0b5e5168e00f5dde6"
a=m.col.get(where={"$and":[{"user_id":uid},{"mtype":"semantic"}]})
print(json.dumps({"semantic_count":len(a.get("ids") or []),"semantic_records":[{"id":i,"text":d,"created_at":meta.get("created_at")} for i,d,meta in zip(a.get("ids") or [],a.get("documents") or [],a.get("metadatas") or [])]},ensure_ascii=False))
r=m.retrieve("请按我之前保存的长期默认继续搜索房源，先复述实际预算和房型",session_id=uid,user_id=uid,n=6)
print(json.dumps({"ranked":[{"id":x["id"],"mtype":x.get("mtype")} for x in r]},ensure_ascii=False))
