"""Bind read-only neighbourhood tools to an explicitly selected listing.

``extracted_context`` must be the server-resolved context, never raw frontend
focus payloads or model-authored arguments. Resolution uses complete records
already in that context; an explicit/focused URL may also read the local cache.
No geocoder, fetch, mutation, or address/name search is performed here.
"""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r'https?://[^\s<>"\]\)\u3002\uff0c\uff1b]+', re.I)
_THIS_PROPERTY = re.compile(
    r'\bthis\s+(?:flat|apartment|home|house|property|listing)\b|'
    r'这个(?:房源|房子|公寓|住宅)|这(?:套|间)(?:房(?:源|子)?|公寓|住宅|住房)?', re.I)
_OTHER_SCOPE = re.compile(
    r'\b(?:separately|instead|unrelated|another\s+(?:place|area)|'
    r'different\s+(?:place|area)|for\s+a\s+separate)\b|另外|单独|另一个(?:地方|地区)', re.I)
_TOPIC = {
    'get_transport_info': re.compile(r'\b(?:journey|route|trip|commute|transport|fare)\b|路线|行程|通勤|交通|票价', re.I),
    'check_safety': re.compile(r'\b(?:crime|safety|safe)\b|治安|犯罪|安全', re.I),
    'search_nearby_pois': re.compile(
        r'\b(?:schools?|gps?|surgeries|clinics?|amenities|facilities|nearby|shops?)\b|'
        r'学校|诊所|全科|设施|超市', re.I),
}


def _url_key(value) -> str:
    if not isinstance(value, str):
        return ''
    try:
        url = urlsplit(value.strip().rstrip('.,;，。；'))
        if url.scheme.lower() not in ('http', 'https') or not url.netloc or url.username:
            return ''
        return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path.rstrip('/'),
                           url.query, ''))
    except ValueError:
        return ''


def _address(record) -> str:
    return str(record.get('address') or record.get('Address') or
               record.get('property_address') or '').strip()


def _identity(record) -> str:
    return _url_key(record.get('url') or record.get('URL') or record.get('property_url'))


def _normal(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip(' ,.;:').lower())


def _coordinates(record):
    values = [record.get(key) for key in ('geo_location', 'Geo_Location', 'coordinates')]
    values.append(record)
    for value in values:
        if isinstance(value, str):
            parts = value.split(',')
            if len(parts) != 2:
                continue
            lat, lon = parts
        elif isinstance(value, dict):
            lat = value.get('latitude', value.get('lat'))
            lon = value.get('longitude', value.get('lng', value.get('lon')))
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            lat, lon = value
        else:
            continue
        if isinstance(lat, bool) or isinstance(lon, bool):
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon) and 49 <= lat <= 61 and -8 <= lon <= 2:
            return lat, lon
    return None


def _focus(context):
    stack = context.get('focus_stack')
    if isinstance(stack, list) and stack:
        return stack[-1] if isinstance(stack[-1], dict) else None
    focused = context.get('focused_property')
    if isinstance(focused, dict) and focused:
        return focused
    if context.get('property_address') and context.get('property_url'):
        return {'address': context['property_address'], 'url': context['property_url'],
                'geo_location': context.get('geo_location') or context.get('property_geo_location'),
                'coordinates': context.get('property_coordinates'),
                'latitude': context.get('property_latitude'),
                'longitude': context.get('property_longitude'), 'area': context.get('area')}
    return None


def _records(context):
    focus = _focus(context)
    records = [focus] if focus else []
    for key in ('focus_stack', 'last_results_full', 'last_results', 'recommended_registry'):
        rows = context.get(key)
        if isinstance(rows, list):
            records.extend(row for row in rows if isinstance(row, dict))
    return records


def _cached_records():
    """The sole optional I/O: exact URL lookup in the existing local store."""
    try:
        from core.scraping.on_demand import iter_cached_listings
        return iter_cached_listings()
    except Exception:
        return []


def _same_scope(location, record) -> bool:
    text = _normal(location)
    if not text or _THIS_PROPERTY.fullmatch(text):
        return True
    address = _normal(_address(record))
    segments = [part.strip() for part in address.split(',') if part.strip()]
    references = {address, _normal(record.get('name')), _normal(record.get('area')),
                  _identity(record), 'the property', 'the flat', 'the home'}
    references.update(segments)
    references.update(', '.join(segments[:index]) for index in range(2, len(segments) + 1))
    return text in references


def has_property_reference(message, extracted_context=None) -> bool:
    """Whether broad legacy coordinate injection must yield to property binding.

    True also for an unknown URL, multiple URLs, or an unresolved "this flat":
    those declare a property scope without authorising coordinates from another
    focus. This predicate does not claim that the identity can be resolved.
    ``extracted_context`` is accepted for the same dispatch-call interface.
    """
    text = str(message or '')
    return bool(_THIS_PROPERTY.search(text) or
                any(_url_key(match.group()) for match in _URL.finditer(text)))


def grounded_property_read_args(tool_name, args, message, extracted_context) -> dict:
    """Return copied args, grounding only a declared property's local read.

    One exact URL takes precedence over another current focus. Without a URL,
    an explicit "this flat/这个房源" needs a server-resolved focused identity.
    Names, result position, area overlap and proximity never select a property.
    A separate place question or conflicting tool location remains untouched.
    """
    params = dict(args or {})
    if tool_name not in _TOPIC or not has_property_reference(message, extracted_context):
        return params
    if tool_name == 'get_transport_info' and (
            params.get('query_type') not in ('journey', 'fare')
            or not _THIS_PROPERTY.fullmatch(str(params.get('from_location') or '').strip())):
        return params
    text = str(message or '')
    context = extracted_context if isinstance(extracted_context, dict) else {}
    urls = {_url_key(match.group()) for match in _URL.finditer(text)} - {''}
    if len(urls) > 1:
        return params
    if urls:
        identity = next(iter(urls))
    else:
        if not _THIS_PROPERTY.search(text):
            return params
        focused = _focus(context)
        identity = _identity(focused) if focused else ''
    if not identity:
        return params
    # A separate weather query does not suppress the property's school/crime
    # lookup. A separate query for THIS tool's topic does suppress re-scoping.
    without_urls = _URL.sub('', text)
    for clause in re.split(r'[.!?。！？;；\n]+', without_urls):
        if _OTHER_SCOPE.search(clause) and _TOPIC[tool_name].search(clause):
            return params
    rows = _records(context)
    target = next((row for row in rows if _identity(row) == identity and
                   _address(row) and _coordinates(row) is not None), None)
    if target is None:
        target = next((row for row in _cached_records() if isinstance(row, dict) and
                       _identity(row) == identity and _address(row) and
                       _coordinates(row) is not None), None)
    if target is None or any(not _same_scope(params.get(key), target)
                             for key in ('address', 'area')):
        return params
    if tool_name == 'get_transport_info':
        params['from_location'] = _address(target)
        return params
    lat, lon = _coordinates(target)
    params.update(address=_address(target), latitude=lat, longitude=lon,
                  _coordinate_source="listing")
    return params
