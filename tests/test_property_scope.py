"""Exact-property read scope; all provider/cache lookups are offline fakes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests

from core import maps_service as maps
from core import property_scope as scope


URL = 'https://www.onthemarket.com/details/20489843/'
OTHER_URL = 'https://www.onthemarket.com/details/20489844/'
RECORD = {'address': 'Tennyson Road, Stratford', 'url': URL,
          'geo_location': '51.5401, 0.0102', 'area': 'Stratford'}
OTHER = {'address': 'Borthwick Road, London, E15', 'url': OTHER_URL,
         'geo_location': {'lat': 51.5422, 'lng': 0.0173}, 'area': 'Stratford'}
FAMILY_R3 = (
    'For Tennyson Road, Stratford — the home at https://www.onthemarket.com/details/20489843/ — '
    'could you find primary schools and GP surgeries within 1 km for our seven-year-old, '
    'and tell us about the local recorded crime? Please give the actual reference point, '
    'distance/radius and crime data month so I can judge how local the evidence is. '
    'Nearby does not mean we have a school place or can register with a GP. '
    'Leave the housing search conditions unchanged for this question.'
)


@pytest.fixture(autouse=True)
def no_external_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('This scope regression must not contact external providers')

    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(scope, '_cached_records', lambda: [])


@pytest.mark.parametrize('tool,args', [
    ('check_safety', {'area': 'Stratford', 'user_query': FAMILY_R3}),
    ('search_nearby_pois', {'address': 'Stratford', 'poi_type': 'school,gp', 'radius': 1000}),
])
def test_exact_family_r3_binds_cached_listing_and_preserves_other_arguments(monkeypatch, tool, args):
    cached = {'Address': RECORD['address'], 'URL': URL, 'geo_location': RECORD['geo_location']}
    monkeypatch.setattr(scope, '_cached_records', lambda: [OTHER, cached])
    original = deepcopy(args)
    result = scope.grounded_property_read_args(tool, args, FAMILY_R3, {})
    assert result == dict(args, address=RECORD['address'], latitude=51.5401, longitude=0.0102)
    assert args == original


@pytest.mark.parametrize('message', [
    'Find schools around this flat.', 'Is this property safe?', '这个房源附近有哪些学校和GP？',
    '请检查这套房附近的治安。',
])
def test_explicit_deictic_uses_server_focus_top_not_first_result(message):
    context = {'focus_stack': [OTHER, RECORD], 'last_results': [OTHER]}
    saved = deepcopy(context)
    result = scope.grounded_property_read_args('check_safety', {'area': 'Stratford'}, message, context)
    assert result['address'] == RECORD['address'] and result['latitude'] == 51.5401
    assert context == saved


def test_focused_url_can_complete_coordinates_only_from_same_cached_identity(monkeypatch):
    monkeypatch.setattr(scope, '_cached_records', lambda: [OTHER, RECORD])
    focus = {'address': RECORD['address'], 'url': URL}
    result = scope.grounded_property_read_args('search_nearby_pois',
        {'address': 'Stratford', 'radius': 1000, 'poi_type': 'school,gp'},
        'Find schools near this flat.', {'focus_stack': [focus]})
    assert result['address'] == RECORD['address'] and result['longitude'] == 0.0102


def test_explicit_url_wins_over_a_different_focused_property():
    result = scope.grounded_property_read_args('check_safety', {'area': 'Stratford'}, FAMILY_R3,
        {'focus_stack': [OTHER], 'last_results_full': [RECORD]})
    assert result['address'] == RECORD['address'] and result['latitude'] != 51.5422


@pytest.mark.parametrize('tool,args', [
    ('check_safety', {'area': 'Stratford'}),
    ('search_nearby_pois', {'address': 'Stratford', 'radius': 1000, 'poi_type': 'school,gp',
                          'latitude': 51.5422, 'longitude': 0.0173}),
])
def test_explicit_url_beats_wrong_focus_coords_and_search_area_metadata(monkeypatch, tool, args):
    # The row may have been scraped during a Leyton search. That metadata never
    # changes identity or lets another Stratford listing donate coordinates.
    target = dict(RECORD, area='Leyton')
    monkeypatch.setattr(scope, '_cached_records', lambda: [OTHER, target])
    result = scope.grounded_property_read_args(tool, args, FAMILY_R3,
                                            {'focus_stack': [OTHER]})
    assert result['address'] == RECORD['address'] and result['latitude'] == 51.5401
    assert result['longitude'] == 0.0102


@pytest.mark.parametrize('tool,args,question', [
    ('check_safety', {'area': 'Camden'}, 'check recorded crime in Camden'),
    ('search_nearby_pois', {'address': 'Camden', 'radius': 1000, 'poi_type': 'school,gp'},
     'find schools and GP surgeries in Camden'),
])
def test_an_explicit_url_does_not_override_a_different_named_place(tool, args, question):
    message = f'The home is {URL}. For a separate visit, please {question}.'
    assert scope.grounded_property_read_args(tool, args, message,
                                            {'focus_stack': [RECORD]}) == args


@pytest.mark.parametrize('context', [
    {'focus_stack': [OTHER]},
    {'focus_stack': [{'address': RECORD['address'], 'url': URL}], 'last_results': [OTHER]},
    {'focus_stack': [dict(RECORD, geo_location=None)], 'last_results': [OTHER]},
    {},
])
def test_missing_exact_identity_or_its_coordinates_cannot_borrow_another_home(context):
    args = {'area': 'Stratford'}
    assert scope.grounded_property_read_args('check_safety', args, FAMILY_R3, context) == args


@pytest.mark.parametrize('args,message', [
    ({'area': 'Camden'}, 'This flat looks interesting. What is recorded crime in Camden?'),
    ({'area': 'Stratford-upon-Avon'}, 'This flat looks interesting; check Stratford-upon-Avon crime.'),
    ({'address': 'Flat 2, Tennyson Road, Stratford'},
     'This flat is fine. Check crime around Flat 2, Tennyson Road, Stratford instead.'),
    ({'area': 'Stratford'}, 'This flat looks interesting. Separately, check recorded crime in Camden.'),
    ({'address': 'Stratford'}, '这个房源不错。另外请单独查Camden的治安。'),
])
def test_different_explicit_place_or_separate_topic_is_not_rescoped(args, message):
    assert scope.grounded_property_read_args('check_safety', args, message,
                                            {'focus_stack': [RECORD]}) == args


def test_separate_weather_request_does_not_suppress_property_school_scope():
    result = scope.grounded_property_read_args('search_nearby_pois',
        {'address': 'Stratford', 'poi_type': 'school'},
        'Find schools around this flat. Separately, check weather in Camden.',
        {'focus_stack': [RECORD]})
    assert result['address'] == RECORD['address']


@pytest.mark.parametrize('context,message', [
    ({}, 'Is this flat safe?'),
    ({'last_results': [RECORD]}, 'Is this flat safe?'),
    ({'focus_stack': [RECORD]}, 'Is Stratford safe?'),
    ({'focus_stack': [dict(RECORD, url=None)]}, 'Is this flat safe?'),
    ({'focus_stack': [RECORD, None]}, 'Is this flat safe?'),
    ({'focus_stack': [RECORD, OTHER]}, 'Is the previous flat safe?'),
])
def test_no_explicit_property_reference_or_resolved_focus_is_no_binding(context, message):
    args = {'area': 'Stratford'}
    assert scope.grounded_property_read_args('check_safety', args, message, context) == args


def test_two_explicit_urls_are_not_silently_reduced_to_one_property():
    args = {'area': 'Stratford'}
    message = f'Compare local crime for {URL} and {OTHER_URL}.'
    assert scope.grounded_property_read_args('check_safety', args, message,
                                            {'focus_stack': [RECORD, OTHER]}) == args


@pytest.mark.parametrize('message,expected', [
    (FAMILY_R3, True), ('Is this flat safe?', True), ('这个房源附近有什么学校？', True),
    ('https://www.onthemarket.com/details/99999999/', True),
    (f'Compare {URL} and {OTHER_URL}', True), ('Is Stratford safe?', False),
    ('Is this area safe?', False),
])
def test_declared_property_reference_skips_legacy_area_coordinate_borrowing(message, expected):
    assert scope.has_property_reference(message, {}) is expected
    assert scope.has_property_reference(message, {'focus_stack': [RECORD]}) is expected


def test_unknown_explicit_url_does_not_borrow_cache_or_focus_coordinates(monkeypatch):
    monkeypatch.setattr(scope, '_cached_records', lambda: [OTHER, RECORD])
    message = FAMILY_R3.replace('/20489843/', '/99999999/')
    args = {'area': 'Stratford', 'latitude': 51.5, 'longitude': -0.1}
    assert scope.has_property_reference(message)
    assert scope.grounded_property_read_args('check_safety', args, message,
                                            {'focus_stack': [RECORD]}) == args


@pytest.mark.parametrize('coordinates', [
    {'lat': 51.5401, 'lng': 0.0}, {'latitude': 51.5401, 'longitude': 0.0102},
    [51.5401, 0.0102], '51.5401, 0.0102',
])
def test_structured_coordinates_from_the_same_listing_are_supported(coordinates):
    row = dict(RECORD, geo_location=None, coordinates=coordinates)
    result = scope.grounded_property_read_args('check_safety', {}, FAMILY_R3,
                                            {'focus_stack': [row]})
    assert result['latitude'] == 51.5401
    assert result['longitude'] == (0.0 if isinstance(coordinates, dict) and 'lng' in coordinates else 0.0102)


@pytest.mark.parametrize('coordinates', [
    'nan, 0', '51.54, inf', '0, 0', {'latitude': 51.54, 'longitude': False},
    {'lat': 51.54}, '51.54, 0, 1',
])
def test_invalid_coordinates_are_unknown_not_replaced_with_another_focus(coordinates):
    row = dict(RECORD, geo_location=coordinates)
    args = {'area': 'Stratford'}
    assert scope.grounded_property_read_args('check_safety', args, FAMILY_R3,
                                            {'focus_stack': [OTHER, row]}) == args


def test_flattened_server_focus_fields_are_supported_and_wrong_model_coords_are_corrected():
    context = {'property_address': RECORD['address'], 'property_url': URL,
               'geo_location': RECORD['geo_location'], 'area': 'Stratford'}
    result = scope.grounded_property_read_args('check_safety',
        {'area': 'Stratford', 'latitude': 52.19, 'longitude': -1.70},
        'Is this flat safe?', context)
    assert result['address'] == RECORD['address'] and result['latitude'] == 51.5401
    assert result['longitude'] == 0.0102


def test_other_tools_and_url_prefixes_do_not_gain_a_property_scope(monkeypatch):
    monkeypatch.setattr(scope, '_cached_records', lambda: [RECORD])
    assert scope.grounded_property_read_args('get_weather', {'location': 'Stratford'},
        FAMILY_R3, {'focus_stack': [RECORD]}) == {'location': 'Stratford'}
    args = {'area': 'Stratford'}
    assert scope.grounded_property_read_args('check_safety', args,
        FAMILY_R3.replace('/20489843/', '/2048984/'), {'focus_stack': [RECORD]}) == args


def test_bare_stratford_uses_london_query_and_cannot_hit_old_avon_cache(monkeypatch):
    read_keys, queries, writes = [], [], {}
    old_key = maps.create_cache_key('_free_geocode_v3', 'Stratford')
    london_key = maps.create_cache_key('_free_geocode_v3', 'Stratford, London')

    def cache(key, **kwargs):
        read_keys.append(key)
        if key == old_key:
            return 'fresh', {'lat': 52.19, 'lng': -1.70}
        return 'miss', None

    def get(url, params, **kwargs):
        queries.append(params['q'])
        return SimpleNamespace(status_code=200, json=lambda: [
            {'lat': '51.54', 'lon': '0.01', 'display_name': 'Stratford, London'}])

    monkeypatch.setattr(maps, '_read_versioned_cache', cache)
    monkeypatch.setattr(maps, '_write_versioned_cache', lambda key, data, **kw: writes.update({key: data}))
    monkeypatch.setattr(maps.requests, 'get', get)
    result = maps.geocode_address('Stratford')
    assert result['lat'] == 51.54
    assert queries == ['Stratford, London'] and read_keys == [london_key]
    assert old_key not in writes and london_key in writes


@pytest.mark.parametrize('address', ['Stratford-upon-Avon', 'Stratford upon Avon',
                                  '10 Tennyson Road, Stratford', 'Stratford, Warwickshire'])
def test_explicit_other_stratford_or_full_address_is_preserved(address):
    assert maps._normalize_address_for_routing(address) == address
