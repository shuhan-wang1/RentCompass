"""Offline HTTP-boundary fixtures for an explicitly synthetic E2E audit.

Tool implementations, their schemas, orchestration and persistence remain real.
No request handled here reaches a network. Unknown destinations fail closed.
"""
import json
import os
import pathlib
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import requests

BASE = pathlib.Path(os.getenv('AUDIT_BASE', '/audit'))
LOCK = threading.Lock()
PLACES = {
    'watford': (51.6565, -0.3903), 'manchester': (53.4808, -2.2426),
    'stratford': (51.5413, -0.0032), 'canary wharf': (51.5054, -0.0235),
    'liverpool street': (51.5178, -0.0823), 'camden': (51.5390, -0.1426),
    'islington': (51.5380, -0.1027), 'hackney': (51.5450, -0.0553),
    'bloomsbury': (51.5230, -0.1270), 'ucl': (51.5246, -0.1340),
    'gower': (51.5246, -0.1340), 'euston': (51.5281, -0.1337),
    'kings cross': (51.5308, -0.1238), "king's cross": (51.5308, -0.1238),
    'finsbury': (51.5645, -0.1066), 'holloway': (51.5570, -0.1170),
    'marylebone': (51.5220, -0.1560), 'grays inn': (51.5250, -0.1140),
    "gray's inn": (51.5250, -0.1140), 'russell': (51.5230, -0.1240),
    'holborn': (51.5170, -0.1200), 'angel': (51.5320, -0.1060),
    'shoreditch': (51.5240, -0.0770), 'walthamstow': (51.5830, -0.0200),
    'greenwich': (51.4826, -0.0077), 'richmond': (51.4610, -0.3030),
    'london': (51.5074, -0.1278),
}


def emit(name, data):
    with LOCK:
        with (BASE / 'evidence' / name).open('a') as f:
            f.write(json.dumps(data, ensure_ascii=False, default=str) + '\n')


def coords(query):
    text = str(query).lower()
    for key, value in PLACES.items():
        if key in text:
            return value
    if re.search(r'\bwd\d', text):
        return PLACES['watford']
    if re.search(r'\b(wc[12]|nw[15]|n1|ec[12]|e1)\b', text):
        return PLACES['bloomsbury']
    return None


def response(request, payload, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = request.url
    result.request = request
    result.headers['Content-Type'] = 'application/json'
    result.headers['X-Audit-Source'] = 'synthetic-transport-fixture'
    result._content = json.dumps(payload).encode()
    result.encoding = 'utf-8'
    return result


ORIGINAL_SEND = requests.Session.send


def send(session, request, **kwargs):
    u = urlsplit(request.url)
    host = (u.hostname or '').lower()
    if host in {'localhost', '127.0.0.1'} and u.port == 19091:
        return ORIGINAL_SEND(session, request, **kwargs)
    if 'onthemarket' in host:
        emit('forbidden-attempts.jsonl', {'host': host, 'path': u.path, 'at': time.time()})
        raise requests.ConnectionError('Housing upstream forbidden by audit')
    modefile = BASE / 'runtime' / 'fixture-mode.json'
    modes = json.loads(modefile.read_text()) if modefile.exists() else {}
    q = parse_qs(u.query)
    group = ('weather' if 'open-meteo' in host else 'tfl' if 'tfl.gov.uk' in host
             else 'police' if 'police.uk' in host else 'osm' if 'overpass' in host
             else 'geocode' if host in {'nominatim.openstreetmap.org', 'api.postcodes.io'}
             else 'web' if host == '127.0.0.1' and u.port == 19092 else 'unknown')
    mode = modes.get(group, 'ok')
    emit('fixture-http.jsonl', {'at': time.time(), 'host': host, 'path': u.path,
                              'group': group, 'mode': mode, 'source': 'synthetic'})
    if group == 'unknown':
        raise requests.ConnectionError('Unconfigured upstream blocked by audit')
    if mode == 'timeout':
        raise requests.Timeout('Synthetic upstream timeout')
    if mode == 'unavailable':
        return response(request, {'audit_fixture': True, 'error': 'Synthetic unavailable'}, 503)
    if group == 'geocode':
        name = q.get('q', [''])[0] or unquote(u.path.rsplit('/', 1)[-1])
        xy = coords(name)
        if host == 'api.postcodes.io':
            return response(request, {'status': 404, 'result': None}, 404)
        return response(request, [] if not xy else [{
            'lat': str(xy[0]), 'lon': str(xy[1]), 'display_name': name,
            'addresstype': 'suburb', 'type': 'suburb', 'place_rank': 18,
            'address': {'country_code': 'gb', 'country': 'United Kingdom'},
        }])
    if group == 'weather':
        if 'geocoding' in host:
            xy = coords(q.get('name', [''])[0])
            return response(request, {'results': [] if not xy else [{'latitude': xy[0], 'longitude': xy[1]}]})
        return response(request, {'current': {'temperature_2m': 14.2, 'relative_humidity_2m': 72,
            'apparent_temperature': 12.8, 'precipitation': 0.4, 'weather_code': 61,
            'wind_speed_10m': 11.5}, 'hourly': {'uv_index': [1.2]}, 'audit_fixture': True})
    if group == 'police':
        month = q.get('date', ['2026-08'])[0]
        return response(request, [] if mode == 'empty' else [
            {'month': month, 'category': 'anti-social-behaviour' if i % 3 else 'burglary',
             'id': i, 'location': {'latitude': '51.523', 'longitude': '-0.127'}} for i in range(45)])
    if group == 'tfl':
        if '/Status' in u.path:
            names = ['Victoria', 'Central', 'Northern'] if '/Mode/' in u.path else [u.path.split('/')[2].title()]
            return response(request, [{'id': name.lower(), 'name': name, 'lineStatuses': [{
                'statusSeverity': 10 if name != 'Central' else 6,
                'statusSeverityDescription': 'Good Service' if name != 'Central' else 'Severe Delays',
                'reason': 'SYNTHETIC TEST DATA: signalling scenario' if name == 'Central' else None,
            }]} for name in names])
        if '/StopPoint' in u.path and '/FareTo/' not in u.path:
            lat = float(q.get('lat', ['51.52'])[0]); lon = float(q.get('lon', ['-0.12'])[0])
            name = min(PLACES, key=lambda x: (PLACES[x][0] - lat) ** 2 + (PLACES[x][1] - lon) ** 2).title()
            stop = {'id': '940GZZTEST' + str(int(abs(lat * 1000))) + str(int(abs(lon * 1000))),
                    'commonName': name + ' [audit fixture]', 'name': name + ' [audit fixture]',
                    'stopType': 'NaptanMetroStation', 'distance': 120}
            return response(request, {'stopPoints': [stop], 'matches': [stop]})
        if '/FareTo/' in u.path:
            return response(request, [{'rows': [{'ticketsAvailable': [{
                'passengerType': 'Adult', 'ticketType': {'type': 'Pay as you go'},
                'ticketTime': {'type': 'Peak'}, 'cost': '4.25'}]}]}])
        leg = {'duration': 32, 'mode': {'name': 'tube'},
               'instruction': {'summary': 'SYNTHETIC TEST DATA: audit route'},
               'departurePoint': {'commonName': 'Audit origin'},
               'arrivalPoint': {'commonName': 'Audit destination'},
               'routeOptions': [{'name': 'Audit fixture line'}]}
        journeys = [] if mode == 'empty' else [
            {'duration': 32, 'legs': [leg], 'fare': {'totalCost': 425}},
            {'duration': 44, 'legs': [{**leg, 'duration': 44}], 'fare': {'totalCost': 280}},
        ]
        return response(request, {'journeys': journeys, 'audit_fixture': True})
    if group == 'web':
        return response(request, {'results': [] if mode == 'empty' else [{
            'title': 'SYNTHETIC TEST DATA — neighbourhood research',
            'url': 'https://www.gov.uk/private-renting', 'engine': 'audit-fixture',
            'content': 'SYNTHETIC TEST DATA, not live advice. Check lease terms and local authority sources. '
                       'This fixture does not verify accessibility, school admission, tenancy availability or agent contact details.'
        }]})
    body = request.body.decode() if isinstance(request.body, bytes) else (request.body or '')
    query = parse_qs(body).get('data', [body])[0]
    match = re.search(r'around:\s*\d+(?:\.\d+)?,\s*([-\d.]+),\s*([-\d.]+)', query)
    lat, lon = (float(match[1]), float(match[2])) if match else PLACES['bloomsbury']
    kind = next((k for k in ['supermarket', 'convenience', 'school', 'hospital', 'pharmacy',
                             'doctors', 'park', 'gym', 'cafe', 'restaurant'] if k in query), 'school')
    return response(request, {'elements': [] if mode == 'empty' else [{
        'type': 'node', 'id': 900001, 'lat': lat + 0.001, 'lon': lon + 0.001,
        'tags': {'name': ('Tesco Express' if kind in {'supermarket', 'convenience'} else kind.title()) + ' [audit fixture]',
                 'brand': 'Tesco' if kind in {'supermarket', 'convenience'} else '',
                 'amenity': kind, 'shop': kind, 'opening_hours': 'Mo-Su 08:00-20:00'},
    }]})


def install():
    requests.Session.send = send
