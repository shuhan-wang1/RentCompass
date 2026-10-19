"""Record actual tool dispatches without changing tool results or policy."""
import time

from transport_fixtures import emit


def install():
    from core.tool_system import Tool
    from uk_rent_agent.observability import current_agent_context, request_id_var
    original = Tool.execute

    async def execute(self, /, **kwargs):
        start = time.time()
        record = {'started': start, 'request_id': request_id_var.get(), 'tool': self.name, 'args': {
            k: v for k, v in kwargs.items() if not k.startswith('_')
        }, 'context': current_agent_context()}
        try:
            result = await original(self, **kwargs)
            record['result'] = result.to_dict()
            return result
        except BaseException as exc:
            record['exception'] = type(exc).__name__
            raise
        finally:
            record['elapsed_s'] = round(time.time() - start, 3)
            emit('tool-dispatches.jsonl', record)

    Tool.execute = execute
