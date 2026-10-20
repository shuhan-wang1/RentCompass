"""Isolated tool-integration outage probes; never mutates the user-journey cache."""
import asyncio
import json
import os
import pathlib
import sqlite3
import time

B = pathlib.Path('/audit/fault')
for directory in ['runtime', 'evidence']:
    (B / directory).mkdir(parents=True, exist_ok=True)
os.environ.update(AUDIT_BASE=str(B), RUNTIME_CACHE_PATH=str(B / 'runtime/cache.sqlite3'),
                  IDEMPOTENCY_DB=str(B / 'runtime/idempotency.sqlite3'))
import transport_fixtures
transport_fixtures.install()
from core.tool_system import create_tool_registry
from core.tools.web_search import set_tool_registry

registry = create_tool_registry()
set_tool_registry(registry)
checks = []


def empty_cache():
    p = B / 'runtime/cache.sqlite3'
    if p.exists():
        with sqlite3.connect(p) as con:
            con.execute('DELETE FROM cache')


async def run_case(label, modes, tool, args, predicate):
    empty_cache()
    (B / 'runtime/fixture-mode.json').write_text(json.dumps(modes))
    started = time.time()
    result = await registry.execute_tool(tool, **args)
    checks.append({'case': label, 'layer': 'tool_integration_fault', 'modes': modes,
                   'tool': tool, 'args': args, 'seconds': round(time.time() - started, 3),
                   'passed': bool(predicate(result)), 'result': result.to_dict()})


async def main():
    pairs = [
        ('weather', 'get_weather', {'location': 'Bloomsbury London'}, lambda r: not r.success),
        ('police', 'check_safety', {'address': 'Bloomsbury London'},
         lambda r: r.data.get('safety_score') is None and bool(r.data.get('crime_data', {}).get('error'))),
        ('tfl', 'get_transport_info', {'query_type': 'line_status', 'line': 'Victoria'}, lambda r: not r.success),
        ('web', 'web_search', {'query': 'London renting fault probe audit fixture'}, lambda r: not r.success),
    ]
    for group, tool, args, pred in pairs:
        for mode in ['unavailable', 'timeout']:
            await run_case(group + '-' + mode, {group: mode}, tool, args, pred)
        await run_case(group + '-recovery', {}, tool, args,
                       lambda r: r.success and (r.data.get('safety_score') is not None if tool == 'check_safety' else True))
    await run_case('tfl-no-journey', {'tfl': 'empty'}, 'calculate_commute',
                   {'from_address': 'Camden London', 'to_address': 'UCL London'},
                   lambda r: r.success and r.data.get('duration_minutes') is None and r.data.get('is_acceptable') is None)
    await run_case('police-empty-not-safe', {'police': 'empty'}, 'check_safety', {'address': 'Bloomsbury London'},
                   lambda r: r.data.get('safety_score') is None)
    await run_case('web-empty-not-evidence', {'web': 'empty'}, 'web_search', {'query': 'audit empty search'}, lambda r: not r.success)
    await run_case('unknown-geocode', {}, 'calculate_commute',
                   {'from_address': 'UnknownAuditPlaceXYZ', 'to_address': 'UnknownAuditPlaceABC'}, lambda r: not r.success)
    poi = {'address': 'Bloomsbury London', 'poi_type': 'pharmacy', 'radius': 300,
           'latitude': 51.523, 'longitude': -0.127}
    await run_case('osm-unavailable-not-zero', {'osm': 'unavailable'}, 'search_nearby_pois', poi,
                   lambda r: not r.success or r.data.get('partial') is True or bool(r.data.get('errors')))
    await run_case('osm-recovery', {}, 'search_nearby_pois', poi,
                   lambda r: r.success and bool(r.data))
    await run_case('nested-composition-one-child-fails', {'weather': 'unavailable'}, 'web_search', {
        'query': 'London viewing audit partial upstream', 'sub_queries': [
            {'tool': 'get_weather', 'params': {'location': 'Bloomsbury London'}},
            {'tool': 'web_search_only', 'params': {'query': 'London audit neighbourhood'}},
        ]}, lambda r: r.success and len(r.data.get('detailed_data', {})) == 1 and 'FAILED' in r.data.get('results', ''))
    (B / 'runtime/fixture-mode.json').write_text('{}')
    (B / 'evidence/fault-checks.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    summary = {'checks': len(checks), 'passed': sum(c['passed'] for c in checks),
               'failed': [c['case'] for c in checks if not c['passed']]}
    (B / 'evidence/fault-summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    raise SystemExit(bool(summary['failed']))


asyncio.run(main())
