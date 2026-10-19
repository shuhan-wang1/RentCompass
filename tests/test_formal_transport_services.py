"""Offline replay of F04/F07/F08; every external request is a controlled fake."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from core import maps_service as maps
from core import web_search as search
from core.tools import get_transport_info as transport
from core.tools import get_weather as weather
from core.tools import search_nearby_pois as poi
from core.tools import web_search as search_tool


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Live HTTP is forbidden in this regression suite')

    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(transport, '_cache_get', lambda *args: None)
    monkeypatch.setattr(transport, '_cache_set', lambda *args: None)
    monkeypatch.setattr(transport, '_london_now', lambda: datetime(2026, 10, 19, 12,
                                                                tzinfo=ZoneInfo('Europe/London')))
    monkeypatch.setattr(poi, 'get_from_cache', lambda *args, **kwargs: None)
    monkeypatch.setattr(poi, 'set_to_cache', lambda *args, **kwargs: None)
    monkeypatch.setattr(poi, 'poi_search_budget_s', lambda: 12.0)
    monkeypatch.setattr(poi, 'POI_PACING_S', 0)
    monkeypatch.setattr(maps, 'OVERPASS_MIN_INTERVAL_S', 0)
    maps.overpass_mirror_state_reset()
    yield
    maps.overpass_mirror_state_reset()


def _station(name):
    return {'token': name, 'name': name, 'in_london': True, 'station': True}


@pytest.mark.parametrize('query,fields', [
    ('from Lewisham to Bank arriving by 8:45 am tomorrow, Tuesday 20 October', {}),
    ('from Lewisham to Bank arriving at 8:45 am on Tuesday 20 October', {}),
    ('从Lewisham到Bank，明天08:45抵达', {}),
    ('', {'date': '2026-10-20', 'time': '08:45', 'time_is': 'arrival'}),
])
def test_requested_arrival_reaches_the_actual_tfl_request(monkeypatch, query, fields):
    calls = []
    monkeypatch.setattr(transport, '_resolve_station', _station)

    def tfl(path, params=None, **kwargs):
        calls.append((path, params))
        return 200, {'journeys': [
            {'duration': 20, 'fare': {'totalCost': 360}, 'legs': []},
            {'duration': 35, 'fare': {'totalCost': 210}, 'legs': []},
        ]}

    monkeypatch.setattr(transport, '_tfl_get', tfl)
    result = transport.get_transport_info_impl(from_location='Lewisham', to_location='Bank',
                                                user_query=query, **fields)
    assert result['success']
    assert calls[0][1] == {'date': '20261020', 'time': '0845', 'timeIs': 'Arriving'}
    assert result['journey_time']['requested_at'] == '2026-10-20T08:45:00+01:00'
    assert result['duration_minutes'] == 20 and result['fare_pence'] == 360
    assert result['cheapest_journey']['fare_pence'] == 210
    assert result['fare_itinerary_id'] == result['fastest_journey']['itinerary_id']
    assert result['fare_itinerary_id'] != result['cheapest_journey']['itinerary_id']


@pytest.mark.parametrize('fields', [
    {'date': '2026-02-30', 'time': '08:45'},
    {'date': '2026-10-20', 'time': '24:01'},
    {'time': '08:45', 'time_is': 'arrival'},
    {'date': '2026-10-20', 'time': '08:45', 'time_is': 'now'},
    {'date': '2026-10-25', 'time': '01:30'},  # repeated London wall time
    {'date': '2027-03-28', 'time': '01:30'},  # nonexistent London wall time
    {'user_query': 'arrive 8:45 am tomorrow on 21 October'},
    {'user_query': 'arrive 8:45 am on Monday 20 October'},
])
def test_invalid_or_undeclared_calendar_time_fails_before_http(monkeypatch, fields):
    monkeypatch.setattr(transport, '_resolve_station', lambda *a: pytest.fail('must validate first'))
    result = transport.get_transport_info_impl(from_location='Lewisham', to_location='Bank', **fields)
    assert not result['success']
    assert result['error_code'] == 'invalid_journey_time'


def test_explicit_fields_override_free_text_and_survive_tool_schema():
    fields = {'date': '2026-10-21', 'time': '09:15', 'time_is': 'departure'}
    model = transport.get_transport_info_tool.input_model.model_validate(fields)
    assert model.model_dump(exclude_none=True).items() >= fields.items()
    params, evidence = transport._requested_journey_time(**fields,
        user_query='tomorrow 20 October arriving by 8:45 am')
    assert params == {'date': '20261021', 'time': '0915', 'timeIs': 'Departing'}
    assert evidence['timezone'] == 'Europe/London'
    assert transport._requested_journey_time(user_query='how long to Bank?') is None
    assert transport._journey_time_params() == ({}, None)


def test_arrival_departure_and_time_changes_have_distinct_cache_keys(monkeypatch):
    calls, cache = [], {}
    monkeypatch.setattr(transport, '_resolve_station', _station)
    monkeypatch.setattr(transport, '_cache_get', lambda key, ttl: cache.get(key))
    monkeypatch.setattr(transport, '_cache_set', lambda key, data: cache.__setitem__(key, data))
    monkeypatch.setattr(transport, '_tfl_get', lambda path, params, **kwargs:
                        (calls.append(params.copy()) or 200,
                         {'journeys': [{'duration': 20, 'fare': {'totalCost': 360}}]}))
    for clock, mode in [('08:45', 'arrival'), ('08:45', 'departure'), ('09:00', 'arrival'),
                        ('08:45', 'arrival')]:
        result = transport.get_transport_info_impl(from_location='Lewisham', to_location='Bank',
            date='2026-10-20', time=clock, time_is=mode)
        assert result['success']
    assert len(calls) == len(cache) == 3
    assert result['from_cache'] is True


def test_tfl_outage_does_not_claim_known_london_stations_are_outside_coverage(monkeypatch):
    monkeypatch.setattr(transport, '_resolve_station', _station)
    monkeypatch.setattr(transport, '_tfl_get', lambda *a, **k: (502, None))
    result = transport.get_transport_info_impl(from_location='Lewisham', to_location='Bank')
    assert not result['success']
    assert result['coverage'] == 'london' and result['service_status'] == 'unavailable'


@pytest.mark.parametrize('modes,expected', [
    (['walking', 'dlr', 'walking', 'tube', 'walking'], 1),
    (['walking', 'dlr', 'walking'], 0),
    (['walking', 'bus', 'dlr', 'tube'], 2),
    (['walking'], 0),
    ([], None),
    (['walking', '', 'tube'], None),
    (['walking', 'unrecognised-mode'], None),
])
def test_changes_count_uses_only_known_itinerary_legs(modes, expected):
    journey = {'legs': [{'mode': {'id': mode}} for mode in modes]}
    assert maps._tfl_changes_count(journey) == expected


def test_changes_count_stays_with_its_own_itinerary_and_real_address_route(monkeypatch):
    journey = {'duration': 30, 'legs': [
        {'mode': {'name': 'walking'}}, {'mode': {'name': 'dlr'}},
        {'mode': {'name': 'walking'}}, {'mode': {'name': 'tube'}},
    ]}
    monkeypatch.setattr(maps, '_get_coordinates', lambda address: {'lat': 51.5, 'lng': 0})
    monkeypatch.setattr(maps, '_tfl_journey', lambda *args: journey)
    actual = maps.calculate_travel_details('1 Test Road London', 'Bank station')
    assert actual['changes_count'] == 1 and actual['duration_minutes'] == 30
    assert transport._journey_evidence(journey)['changes_count'] == 1
    assert transport._journey_evidence({'duration': 15})['changes_count'] is None


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


def _fake_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(maps.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(maps.time, 'sleep', clock.sleep)
    return clock


def test_pacing_cannot_start_a_request_after_the_budget(monkeypatch):
    clock = _fake_clock(monkeypatch)
    monkeypatch.setattr(maps, 'OVERPASS_MIN_INTERVAL_S', 1)
    maps._last_request_at[0] = clock.now
    monkeypatch.setattr(maps.requests, 'post', lambda *a, **k: pytest.fail('late request'))
    with pytest.raises(maps.OverpassError) as failure:
        maps.overpass_request('q', deadline=clock.now + 0.25)
    assert failure.value.service_status == 'time_budget'
    assert clock.now <= 100.25


def test_connect_and_read_share_the_deadline_without_a_one_second_floor(monkeypatch):
    clock = _fake_clock(monkeypatch)
    seen = []

    def fail(url, **kwargs):
        seen.append(kwargs['timeout'])
        clock.sleep(kwargs['timeout'] * 2)
        raise requests.exceptions.ReadTimeout('offline timeout')

    monkeypatch.setattr(maps.requests, 'post', fail)
    with pytest.raises(maps.OverpassError):
        maps.overpass_request('q', deadline=clock.now + 0.2)
    assert seen == [pytest.approx(0.1)]
    assert clock.now == pytest.approx(100.2)


def test_one_clean_empty_plus_failing_mirrors_is_unknown(monkeypatch):
    _fake_clock(monkeypatch)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return SimpleNamespace(status_code=200, json=lambda: {'elements': []})
        return SimpleNamespace(status_code=502)

    monkeypatch.setattr(maps.requests, 'post', post)
    with pytest.raises(maps.OverpassError) as failure:
        maps.overpass_request('q', expect_nonempty=True, max_rounds=1)
    assert not isinstance(failure.value, maps.OverpassEmpty)


def test_all_mirror_failures_cool_down_before_the_next_category(monkeypatch):
    _fake_clock(monkeypatch)
    seen = []
    monkeypatch.setattr(maps.requests, 'post', lambda url, **kwargs:
                        seen.append(url) or SimpleNamespace(status_code=502))
    with pytest.raises(maps.OverpassError):
        maps.overpass_request('school', max_rounds=1)
    attempts = len(seen)
    with pytest.raises(maps.OverpassError) as failure:
        maps.overpass_request('gp', max_rounds=1)
    assert len(seen) == attempts == len(maps.OVERPASS_MIRRORS)
    assert failure.value.service_status == 'cooldown'


def test_school_results_survive_gp_timeout_with_per_category_status(monkeypatch):
    clock = _fake_clock(monkeypatch)
    category_deadlines = []

    def query(lat, lon, category, *args, deadline, **kwargs):
        category_deadlines.append(deadline)
        if category == 'school':
            clock.sleep(1)
            return [{'name': 'Recorded school', 'distance_display': '200m'}]
        clock.sleep(deadline - clock.now)
        raise maps.OverpassError('timeout', service_status='time_budget', attempts=2)

    monkeypatch.setattr(poi, 'query_osm_pois', query)
    result = poi.search_nearby_pois_impl('Stratford', poi_type='school,gp',
                                       latitude=51.54, longitude=0.0)
    assert result['success'] and result['partial']
    assert result['pois']['school'][0]['name'] == 'Recorded school'
    assert result['completed_types'] == ['school']
    assert result['failed_types'] == ['gp'] and result['empty_types'] == []
    assert result['unknown_types'] == ['gp']
    assert result['category_status']['gp']['service_status'] == 'time_budget'
    assert category_deadlines == [106.0, 112.0]
    assert result['elapsed_seconds'] <= 12


def test_first_failed_category_reserves_budget_for_second_success(monkeypatch):
    clock = _fake_clock(monkeypatch)

    def query(lat, lon, category, *args, deadline, **kwargs):
        if category == 'school':
            clock.sleep(deadline - clock.now)
            raise maps.OverpassError('timeout', service_status='time_budget')
        clock.sleep(1)
        return [{'name': 'Recorded GP'}]

    monkeypatch.setattr(poi, 'query_osm_pois', query)
    result = poi.search_nearby_pois_impl('Stratford', poi_type='school,gp',
                                       latitude=51.54, longitude=0.0)
    assert result['completed_types'] == ['gp'] and result['unknown_types'] == ['school']
    assert result['success'] and result['partial']
    assert clock.now < 112


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_weather_forecast_uses_the_requested_hour_and_reports_validity(monkeypatch):
    seen = []
    data = {'timezone': 'Europe/London', 'current': {'temperature_2m': 30},
            'hourly': {'time': ['2026-10-20T08:00', '2026-10-20T09:00'],
                       'temperature_2m': [9, 10], 'weather_code': [61, 3],
                       'precipitation_probability': [70, 10], 'uv_index': [0, 1]}}
    monkeypatch.setattr(weather.requests, 'get', lambda url, params, **kwargs:
                        seen.append(params) or Response(data))
    result = weather.get_weather_impl('Lewisham', 51.46, -0.01,
                                     target_date='2026-10-20', target_time='08:00')
    assert result['weather_type'] == 'forecast' and result['forecast_temp'] == 9
    assert result['valid_at'] == '2026-10-20T08:00:00+01:00'
    assert result['rainfall_chance'] == 70 and result['assumed_time'] is False
    assert 'current_temp' not in result and 'current' not in seen[0]
    assert seen[0]['start_date'] == seen[0]['end_date'] == '2026-10-20'
    default = weather.get_weather_impl('Lewisham', 51.46, -0.01, target_date='2026-10-20')
    assert default['forecast_temp'] == 10 and default['assumed_time'] is True


def test_weather_unavailable_future_hour_never_falls_back_to_current(monkeypatch):
    monkeypatch.setattr(weather.requests, 'get', lambda *args, **kwargs:
                        Response({'current': {'temperature_2m': 30, 'weather_code': 0}}))
    result = weather.get_weather_impl('Lewisham', 51.46, -0.01, target_date='2026-10-20')
    assert not result['success'] and result['weather_type'] == 'forecast'
    assert 'current_temp' not in result and 'forecast_temp' not in result


def test_forecast_for_an_unrelated_hour_cannot_answer_the_requested_morning(monkeypatch):
    monkeypatch.setattr(weather.requests, 'get', lambda *args, **kwargs:
                        Response({'hourly': {'time': ['2026-10-20T22:00'],
                                             'temperature_2m': [15], 'weather_code': [0]}}))
    result = weather.get_weather_impl('Lewisham', 51.46, -0.01, target_date='2026-10-20')
    assert not result['success'] and result['weather_type'] == 'forecast'


def test_current_weather_uv_is_aligned_with_current_hour(monkeypatch):
    monkeypatch.setattr(weather.requests, 'get', lambda *args, **kwargs: Response({
        'current': {'time': '2026-10-19T12:15', 'temperature_2m': 15, 'weather_code': 0},
        'hourly': {'time': ['2026-10-19T00:00', '2026-10-19T12:00'], 'uv_index': [0, 3]},
    }))
    result = weather.get_weather_impl('Lewisham', 51.46, -0.01)
    assert result['weather_type'] == 'current' and result['current_temp'] == 15
    assert result['uv_index'] == 3


@pytest.mark.parametrize('fields', [
    {'target_date': '2026-02-30'}, {'target_time': '08:00'},
    {'target_date': '2026-10-25', 'target_time': '01:30'},
    {'target_date': '2027-03-28', 'target_time': '01:30'},
])
def test_invalid_weather_calendar_is_rejected_without_http(monkeypatch, fields):
    monkeypatch.setattr(weather.requests, 'get', lambda *args, **kwargs: pytest.fail('invalid request'))
    result = weather.get_weather_impl('Lewisham', 51.46, -0.01, **fields)
    assert not result['success'] and result['error_code'] == 'invalid_forecast_time'


def test_weather_geocode_and_forecast_share_one_budget(monkeypatch):
    clock = _fake_clock(monkeypatch)
    calls = []

    def get(url, params, timeout):
        calls.append(timeout)
        clock.sleep(timeout * 2)
        if 'geocoding' in url:
            return Response({'results': [{'latitude': 51.46, 'longitude': -0.01}]})
        raise requests.exceptions.ReadTimeout('offline timeout')

    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.get_weather_impl('Lewisham', target_date='2026-10-20')
    assert not result['success'] and result['retryable']
    assert len(calls) == 2 and clock.now <= 112


def test_searx_unresponsive_engines_are_unavailable_and_enter_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(search.requests, 'get', lambda *args, **kwargs:
                        calls.append(kwargs) or Response({'results': [],
                            'unresponsive_engines': [['google', 'timeout'], ['bing', 'blocked']]}))
    client = search.SearXNGSearch()
    assert client.search('Stratford schools') == []
    assert client.last_health['service_status'] == 'unavailable'
    assert client.last_health['attempts'][-1]['unresponsive_engine_count'] == 2
    assert client.search('Stratford GPs') == [] and len(calls) == 2
    assert client.last_health['reason'] == 'cooldown'


def test_clean_empty_search_is_distinct_and_health_is_thread_local(monkeypatch):
    barrier = threading.Barrier(2)
    client = search.SearXNGSearch()

    def get(url, params, **kwargs):
        if params['q'] == 'school reviews':
            return Response({'results': [{'title': 'school', 'url': 'https://example.test/',
                                          'content': 'school'}]})
        return Response({'results': []})

    def run(query):
        results = client.search(query)
        barrier.wait(timeout=5)
        return results, client.last_health['service_status']

    monkeypatch.setattr(search.requests, 'get', get)
    with ThreadPoolExecutor(max_workers=2) as workers:
        good = workers.submit(run, 'school reviews')
        empty = workers.submit(run, 'empty reviews')
        assert good.result()[1] == 'healthy'
        assert empty.result() == ([], 'empty')


def test_exact_search_health_crosses_executor_boundary_into_tool_result(monkeypatch):
    snippet = search.SearchSnippet('No search results found for this query.',
                                  {'service_status': 'unavailable', 'attempt_count': 2})
    monkeypatch.setattr(search_tool, 'get_search_snippets', lambda *args: snippet)
    result = asyncio.run(search_tool.web_search_func('Stratford school'))
    assert not result['success'] and result['error_code'] == 'search_service_unavailable'
    assert result['service_status'] == 'unavailable'
    assert result['search_health']['web_search']['attempt_count'] == 2


def test_search_snippets_carry_backend_health_with_compatible_string_api(monkeypatch):
    monkeypatch.setattr(search, '_read_search_cache', lambda key: ('miss', None))
    monkeypatch.setattr(search, '_searxng_client', SimpleNamespace(
        search=lambda *args, **kwargs: [],
        last_health={'service_status': 'unavailable', 'attempt_count': 2},
    ))
    snippet = search.get_search_snippets('school')
    assert isinstance(snippet, str) and snippet == 'No search results found for this query.'
    assert snippet.service_health['service_status'] == 'unavailable'
