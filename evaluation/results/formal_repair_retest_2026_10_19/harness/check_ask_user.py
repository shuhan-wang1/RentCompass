from pathlib import Path
import os,sys,asyncio,json
B=Path('/tmp/rentcompass-fix-live-20261019');sys.path[:0]=[str(B/'source/src'),str(B/'source/app'),str(B/'source')]
os.environ.update(USE_MCP_TOOLS='0',TOOLS_OVER_MCP='0',RUNTIME_CACHE_PATH=str(B/'runtime/cache.sqlite3'),IDEMPOTENCY_DB=str(B/'runtime/idempotency.sqlite3'),MEMORY_GATE_DB=str(B/'runtime/memory_gate.sqlite3'))
from core.tool_system import create_tool_registry
async def main():
 registry=create_tool_registry()
 params={'question':'Which budget basis applies?', 'clarification_kind':'other','missing_fields':['budget_basis'],
         'options':[{'label':'Whole home total','value':'whole_total'},{'label':'Per person','value':'per_person'}]}
 result=await registry.execute_tool('ask_user',**params)
 row={'tool':'ask_user','layer':'direct_tool_integration','args':params,'result':result.to_dict(),
      'note':'Corrected the audit input to the declared clarification_kind enum. Initial invalid-input probe remains archived.'}
 (B/'evidence/ask-user-valid.json').write_text(json.dumps(row,ensure_ascii=False,indent=2));print({'success':result.success})
asyncio.run(main())
