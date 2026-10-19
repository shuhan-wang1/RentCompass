"""Supplementary integration probes; these are NOT browser/LLM E2E journeys."""
import asyncio
import json
import os
import pathlib
import sqlite3
import time

# Give direct probes their own mutable stores; shared listing snapshot is read-only.
os.environ['IDEMPOTENCY_DB'] = '/audit/runtime/contract-idempotency-v4.sqlite3'
os.environ['AGENT_MEMORY_DB_PATH'] = '/audit/runtime/contract-memory-v4'

import transport_fixtures
transport_fixtures.install()
from core.scraping import on_demand
get_listings_original = on_demand.get_listings


def cached_listings(*args, **kwargs):
    kwargs.update(cache_only=True, force_refresh=False)
    return get_listings_original(*args, **kwargs)


on_demand.get_listings = cached_listings
from core.tool_system import create_tool_registry
from core.tools.web_search import set_tool_registry

B = pathlib.Path('/audit')
registry = create_tool_registry()
set_tool_registry(registry)
checks = []


async def probe(name, tool, args, expected=True, predicate=None):
    start = time.time()
    result = await registry.execute_tool(tool, **args)
    passed = result.success is expected and (predicate(result.data) if predicate else True)
    checks.append({'case': name, 'layer': 'tool_integration', 'tool': tool, 'args': args,
                   'started': start, 'seconds': round(time.time() - start, 3),
                   'passed': bool(passed), 'expected_success': expected, 'result': result.to_dict()})
    return result


async def main():
    with sqlite3.connect(B / 'runtime/listing_cache.sqlite3') as con:
        rows = [row for blob, in con.execute('SELECT rows FROM listings') for row in json.loads(blob)]
    first = next(r for r in rows if r.get('url') or r.get('URL'))
    cases = {
        'search_properties': {'area': 'Watford', 'max_budget': 1800, 'bedrooms': 1,
                              'room_type': 'whole', 'no_commute': True, 'confirmed': True},
        'calculate_commute': {'from_address': 'Camden, London NW1', 'to_address': 'UCL, London WC1E'},
        'calculate_commute_cost': {'from_address': 'Camden, London NW1', 'to_address': 'UCL, London WC1E', 'travel_type': 'adult'},
        'check_safety': {'address': 'Bloomsbury London'},
        'get_weather': {'location': 'Bloomsbury London'},
        'web_search': {'query': 'London renting neighbourhood guidance audit fixture'},
        'search_nearby_pois': {'address': 'Bloomsbury London', 'poi_type': 'supermarket,pharmacy',
                              'latitude': 51.523, 'longitude': -0.127, 'radius': 300},
        'get_property_details': {'property_url': first.get('url') or first['URL']},
        'check_transport_cost': {'end_zone': 3, 'travel_type': 'adult'},
        'get_transport_info': {'query_type': 'journey', 'from_location': 'Camden London', 'to_location': 'Liverpool Street London'},
        'remember': {'content': 'SYNTHETIC_CONTRACT_PRIVATE: quiet housing near UCL',
                     'user_id': 'e2e_contract_a', 'session_id': 'contract_one', 'idempotency_key': 'contract-memory-one'},
        'recall_memory': {'query': 'SYNTHETIC_CONTRACT_PRIVATE quiet UCL', 'user_id': 'e2e_contract_a', 'session_id': 'contract_two'},
        'ask_user': {'question': 'Which city would you like to live in?', 'clarification_kind': 'missing_area', 'missing_fields': ['area']},
        'compare_or_rank_areas': {'city': 'London', 'candidate_areas': ['Camden', 'Islington', 'Bloomsbury'],
                                  'priorities': ['value', 'safety'], 'budget_hint': {'amount': 1800, 'period': 'month'}},
    }
    for tool in registry.list_tool_names():
        await probe('positive-' + tool, tool, cases[tool])
    for tool in registry.list_tool_names():
        await probe('unknown-field-rejected-' + tool, tool, {**cases[tool], 'UNRECOGNIZED_AUDIT_FIELD': True}, False)
    for qt in ['fare', 'travelcard', 'line_status', 'auto']:
        args = {'query_type': qt, 'from_location': 'Camden London', 'to_location': 'Liverpool Street London',
                'end_zone': 3, 'line': 'Central', 'user_query': 'Are there delays on Central line?'}
        await probe('transport-mode-' + qt, 'get_transport_info', args)
    for mode in ['walking', 'bicycling', 'driving']:
        await probe('commute-mode-' + mode, 'calculate_commute', {**cases['calculate_commute'], 'mode': mode})
    await probe('student-fares', 'check_transport_cost', {'end_zone': 3, 'travel_type': 'student'})
    await probe('outside-london-coverage', 'get_transport_info', {'query_type': 'journey', 'from_location': 'Manchester', 'to_location': 'UCL London'})
    await probe('exact-url-miss', 'get_property_details', {'property_url': 'https://rental-fixture.invalid/no-such-listing'}, False,
                predicate=lambda d: d.get('found') is False)
    await probe('memory-replay', 'remember', cases['remember'])
    await probe('memory-other-user-isolation', 'recall_memory', {'query': 'SYNTHETIC_CONTRACT_PRIVATE', 'user_id': 'e2e_contract_b'},
                predicate=lambda d: d.get('count') == 0)
    await probe('memory-missing-user', 'recall_memory', {'query': 'SYNTHETIC_CONTRACT_PRIVATE'}, False)
    await probe('memory-write-no-key', 'remember', {'content': 'do not store', 'user_id': 'e2e_contract_b'}, False)
    await probe('nested-web-readonly-composition', 'web_search', {'query': 'Bloomsbury weather and travel research', 'sub_queries': [
        {'tool': 'get_weather', 'params': {'location': 'Bloomsbury London'}},
        {'tool': 'calculate_commute', 'params': cases['calculate_commute']},
        {'tool': 'web_search_only', 'params': {'query': 'London renting guidance audit fixture'}},
    ]}, predicate=lambda d: len(d.get('detailed_data', {})) == 3)
    await probe('nested-web-all-six-allowed-children', 'web_search', {'query': 'Bloomsbury viewing research', 'sub_queries': [
        {'tool': 'check_safety', 'params': cases['check_safety']},
        {'tool': 'get_weather', 'params': cases['get_weather']},
        {'tool': 'search_nearby_pois', 'params': cases['search_nearby_pois']},
        {'tool': 'get_property_details', 'params': cases['get_property_details']},
        {'tool': 'calculate_commute', 'params': cases['calculate_commute']},
        {'tool': 'web_search_only', 'params': {'query': 'London renting guidance audit fixture'}},
    ]}, predicate=lambda d: len(d.get('detailed_data', {})) == 6)
    await probe('nested-web-write-rejected', 'web_search', {'query': 'test rejected write', 'sub_queries': [
        {'tool': 'remember', 'params': {'content': 'forbidden nested write', 'user_id': 'e2e_contract_a'}}]}, False)
    await probe('nested-web-recursion-rejected', 'web_search', {'query': 'test rejected recursion', 'sub_queries': [
        {'tool': 'web_search', 'params': {'query': 'nested recursive'}}]}, False)
    await probe('ask-user-invalid-kind', 'ask_user', {'question': 'q', 'clarification_kind': 'invalid'}, False)
    await probe('transport-zone-invalid', 'get_transport_info', {'query_type': 'travelcard', 'end_zone': 99}, False)
    await probe('commute-invalid-mode', 'calculate_commute', {**cases['calculate_commute'], 'mode': 'teleport'}, False)
    await probe('unknown-tool', 'no_such_tool', {}, False)
    path = B / 'evidence/contract-checks.json'
    path.write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    summary = {'layer': 'tool_integration', 'checks': len(checks), 'passed': sum(c['passed'] for c in checks),
               'failed': [c['case'] for c in checks if not c['passed']]}
    (B / 'evidence/contract-summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    raise SystemExit(bool(summary['failed']))


asyncio.run(main())
