"""Offline regressions for the October end-to-end audit's evidence contracts.

The tests exercise actual tool assembly and HTTP-boundary responses; no model,
external service, live listing, or application cache is needed.
"""
import asyncio
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import requests

from core import maps_service as maps
from core.tools import check_safety as safety
from core.tools import calculate_commute_cost as commute_cost
from core.tools import check_transport_cost as transport_cost
from core.tools import get_property_details as details
from core.tools import get_transport_info as transport
from core.tools import search_nearby_pois as poi


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('External HTTP is forbidden in this regression suite')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    monkeypatch.setattr(transport, '_cache_get', lambda *args: None)
    monkeypatch.setattr(transport, '_cache_set', lambda *args: None)
    monkeypatch.setattr(poi, 'get_from_cache', lambda *args, **kwargs: None)
    monkeypatch.setattr(poi, 'set_to_cache', lambda *args, **kwargs: None)
    monkeypatch.setattr(poi, 'POI_PACING_S', 0)


def itinerary(minutes, pence, name):
    row = {'duration': minutes, 'legs': [{
        'mode': {'name': 'tube'}, 'duration': minutes,
        'routeOptions': [{'name': name}],
        'departurePoint': {'commonName': 'A'},
        'arrivalPoint': {'commonName': 'B'},
        'instruction': {'summary': name},
    }]}
    if pence is not None:
        row['fare'] = {'totalCost': pence}
    return row


def journey_setup(monkeypatch, journeys):
    monkeypatch.setattr(transport, '_resolve_station', lambda text: {
        'input': text, 'in_london': True, 'station': True,
        'token': 'stop-' + text, 'name': text,
    })
    monkeypatch.setattr(transport, '_journey_time_params', lambda: ({}, None))
    monkeypatch.setattr(transport, '_tfl_get', lambda *args, **kwargs: (200, {'journeys': journeys}))
    monkeypatch.setattr(transport, '_station_fare', lambda *args: None)


@pytest.mark.parametrize('mode', ['journey', 'fare'])
def test_fastest_and_cheapest_remain_whole_itineraries(monkeypatch, mode):
    journey_setup(monkeypatch, [itinerary(32, 425, 'Fast'), itinerary(44, 280, 'Cheap')])
    out = transport.get_transport_info_impl(query_type=mode, from_location='A', to_location='B')
    assert (out['duration_minutes'], out['fare_gbp']) == (32, 4.25)
    fast, cheap = out['fastest_journey'], out['cheapest_journey']
    assert (fast['duration_minutes'], fast['fare_gbp']) == (32, 4.25)
    assert (cheap['duration_minutes'], cheap['fare_gbp']) == (44, 2.8)
    assert 'Fast' in fast['route_summary'] and 'Cheap' in cheap['route_summary']
    assert fast['itinerary_id'] != cheap['itinerary_id']
    assert out['fare_itinerary_id'] == out['selected_itinerary_id'] == fast['itinerary_id']
    assert cheap['fare_itinerary_id'] == cheap['itinerary_id']
    assert out['fare_matches_itinerary'] is True


def test_unpriced_fastest_never_borrows_a_slower_itinerary_fare(monkeypatch):
    journey_setup(monkeypatch, [itinerary(32, None, 'Fast'), itinerary(44, 280, 'Cheap')])
    out = transport._do_journey('A', 'B', want_fare=True)
    assert out['duration_minutes'] == 32 and not out['fare_available']
    assert 'fare_gbp' not in out
    assert not out['fastest_journey']['fare_available']
    assert out['cheapest_journey']['fare_gbp'] == 2.8


def test_station_fare_fallback_does_not_claim_itinerary_binding(monkeypatch):
    journey_setup(monkeypatch, [itinerary(32, None, 'Fast')])
    monkeypatch.setattr(transport, '_station_fare', lambda *args: {'peak_gbp': 3.6, 'off_peak_gbp': 3.1})
    out = transport._do_journey('A', 'B', want_fare=True)
    assert out['fare_gbp'] == 3.6  # compatibility with existing station-fare callers
    assert out['fare_basis'] == 'station_pair' and not out['fare_matches_itinerary']
    assert out['fare_itinerary_id'] is None
    assert out['station_fare']['source'] == 'TfL Single Fare Finder'
    assert not out['fastest_journey']['fare_available']
    assert 'not a fare confirmed' in out['fare_note']


def test_cached_journey_is_not_labelled_live(monkeypatch):
    rows = [itinerary(32, 425, 'Fast')]
    journey_setup(monkeypatch, rows)
    monkeypatch.setattr(transport, '_cache_get', lambda *args: {'journeys': rows})
    out = transport._do_journey('A', 'B', want_fare=False)
    assert out['from_cache'] is True and 'live' not in out['source'].lower()


def crime_setup(monkeypatch):
    cache, calls = {}, []
    monkeypatch.setattr(maps, '_read_versioned_cache', lambda key, **kwargs:
                        ('fresh', cache[key]) if key in cache else ('miss', None))
    monkeypatch.setattr(maps, '_write_versioned_cache', lambda key, value, **kwargs:
                        cache.__setitem__(key, value))
    class Response:
        def __init__(self, month):
            self.month = month
        def raise_for_status(self):
            pass
        def json(self):
            return [{'month': self.month, 'category': 'burglary'}] * 400
    def get(url, **kwargs):
        calls.append(url)
        return Response(parse_qs(urlparse(url).query)['date'][0])
    monkeypatch.setattr(maps.requests, 'get', get)
    return cache, calls, get


def test_safety_coordinates_reach_http_and_partition_cache(monkeypatch):
    _, calls, _ = crime_setup(monkeypatch)
    monkeypatch.setattr(maps, '_get_coordinates', lambda address: pytest.fail('Explicit coordinates must skip geocoding'))
    first = maps.get_crime_data_by_location('same address', latitude=51.51, longitude=-0.11)
    maps.get_crime_data_by_location('same address', latitude=51.62, longitude=-0.22)
    again = maps.get_crime_data_by_location('same address', latitude=51.51, longitude=-0.11)
    assert len(calls) == 2 * maps.MONTHS_OF_CRIME_DATA
    points = [(parse_qs(urlparse(url).query)['lat'][0], parse_qs(urlparse(url).query)['lng'][0]) for url in calls]
    assert points[:3] == [('51.51', '-0.11')] * 3
    assert points[3:] == [('51.62', '-0.22')] * 3
    assert again['from_cache'] is True
    assert first['reference_point']['lat'] == again['reference_point']['lat'] == 51.51
    assert first['reference_point']['precision'] == 'supplied_coordinates'
    assert first['source'] == 'data.police.uk' and first['radius_m'] == 1609.344


@pytest.mark.parametrize('lat,lon', [(51.5, None), (None, -0.1), (float('nan'), -0.1), (91, 0)])
def test_invalid_safety_coordinates_never_fall_back_to_another_place(monkeypatch, lat, lon):
    monkeypatch.setattr(maps, '_get_coordinates', lambda address: pytest.fail('Invalid explicit coordinates must not relocate'))
    with pytest.raises(ValueError):
        maps.get_crime_data_by_location('Watford', latitude=lat, longitude=lon)


def test_safety_tool_schema_preserves_coordinates_and_own_reference(monkeypatch):
    crime_setup(monkeypatch)
    monkeypatch.setattr(maps, '_get_coordinates', lambda address: pytest.fail('Unexpected geocoding'))
    result = asyncio.run(safety.check_safety_tool.execute(
        address='Watford', latitude=51.646597, longitude=-0.423485,
        user_query='Crime near these coordinates',
    ))
    out = result.data
    assert out['reference_point'] == out['crime_data']['reference_point']
    assert out['reference_point']['lon'] == -0.423485
    assert out['source'] == 'data.police.uk'
    assert out['data_months'] and out['months_covered'] == 3
    assert '3-month total' in out['safety_analysis'] and '6-month total' not in out['safety_analysis']
    assert 'below London average' not in out['safety_analysis']


def test_area_safety_does_not_claim_a_property_point(monkeypatch):
    crime_setup(monkeypatch)
    monkeypatch.setattr(maps, '_get_coordinates', lambda address: {
        'lat': 51.65, 'lng': -0.4, 'geocoder': 'nominatim',
        'match_type': 'town', 'place_rank': 16, 'resolved_name': 'Watford town centre',
    })
    out = safety.check_safety_impl(area='Watford')
    assert out['reference_point']['precision'] == 'area'
    assert not out['reference_point']['is_specific_address']
    assert out['reference_point']['coordinate_source'] == 'nominatim'


def test_failed_month_is_reported_and_not_frozen_in_crime_cache(monkeypatch):
    cache, calls, get = crime_setup(monkeypatch)
    def first_fails(url, **kwargs):
        if not calls:
            calls.append(url)
            raise requests.exceptions.Timeout()
        return get(url, **kwargs)
    monkeypatch.setattr(maps.requests, 'get', first_fails)
    out = maps.get_crime_data_by_location('Watford', latitude=51.65, longitude=-0.4)
    assert out['partial'] and len(out['failed_months']) == 1
    assert out['months_covered'] == 2 and out['crimes_per_month'] == 400
    assert cache == {}, 'A failed month must be retried, not cached as a complete period'


def test_zero_record_successful_month_is_part_of_the_denominator(monkeypatch):
    _, calls, get = crime_setup(monkeypatch)
    class Empty:
        def raise_for_status(self):
            pass
        def json(self):
            return []
    def first_empty(url, **kwargs):
        if not calls:
            calls.append(url)
            return Empty()
        return get(url, **kwargs)
    monkeypatch.setattr(maps.requests, 'get', first_empty)
    out = maps.get_crime_data_by_location('Watford', latitude=51.65, longitude=-0.4)
    assert out['months_covered'] == 3 and len(out['data_months']) == 3
    assert out['crimes_per_month'] == round(800 / 3, 1)


def test_detail_snapshot_never_becomes_a_live_price(monkeypatch):
    row = {'Address': 'North Approach, Watford WD25', 'URL': 'https://www.onthemarket.com/details/123/',
           'Price': '£1,100 pcm', 'Room_Type_Category': '1 bed Flat',
           '_cache_fetched_at': time.time() - 86400, '_cache_ttl_seconds': 3600}
    monkeypatch.setattr(details, 'load_property_database', lambda: pd.DataFrame([row]))
    from core.scraping import on_demand
    monkeypatch.setattr(on_demand, 'find_cached_listing_by_url', lambda url: {
        key: value for key, value in row.items() if not key.startswith('_cache_')})
    out = details.get_property_details_impl(property_url=row['URL'])
    assert out['found'] and out['match']['verdict'] == 'exact_url'
    assert out['cache_status'] == 'stale' and out['cache_age_seconds'] >= 86400
    assert out['from_cache'] and not out['live_verified'] and out['possibly_outdated']
    assert out['source_url'] == row['URL']
    assert out['property']['cache_status'] == out['provenance']['cache_status'] == 'stale'
    assert 'Expired cache entry' in out['formatted_details']
    assert 'not verified as live/current' in out['formatted_details']


def test_unknown_detail_age_stays_unknown_not_fresh():
    out = details._listing_provenance({'URL': 'https://www.onthemarket.com/details/123/'})
    assert out['cache_status'] == 'unknown' and out['fetched_at'] is None
    assert out['from_cache'] and out['possibly_outdated'] and not out['live_verified']


def test_cache_timestamp_only_belongs_to_the_identical_detail_row(monkeypatch, tmp_path):
    from core.scraping import on_demand
    original = {'Address': 'North Approach, Watford WD25', 'URL': 'https://www.onthemarket.com/details/123/', 'Price': '£1100 pcm'}
    db_path = tmp_path / 'listings.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE listings(rows TEXT,fetched REAL)')
        db.execute('INSERT INTO listings VALUES (?,?)', (json.dumps([original]), time.time() - 9000))
    class Cache:
        _lock = threading.RLock()
        def _connect(self):
            return sqlite3.connect(db_path)
    monkeypatch.setattr(on_demand, '_cache', lambda: Cache())
    monkeypatch.setattr(on_demand, 'iter_cached_listings', lambda: [dict(original)])
    monkeypatch.setattr(on_demand, 'TTL_HOURS', 1)
    first = details.load_property_database().iloc[0].to_dict()
    assert details._listing_provenance(first)['cache_status'] == 'stale'
    with sqlite3.connect(db_path) as db:
        db.execute('UPDATE listings SET rows=?, fetched=?', (json.dumps([{**original, 'Price': '£1300 pcm'}]), time.time()))
    stale_snapshot = details.load_property_database().iloc[0].to_dict()
    assert details._listing_provenance(stale_snapshot)['cache_status'] == 'unknown'


def poi_setup(monkeypatch, lookup):
    monkeypatch.setattr(poi, 'query_osm_pois', lookup)
    monkeypatch.setattr(poi, 'geocode_address', lambda *args, **kwargs: (51.5, -0.1))


def test_school_gp_and_actual_radius_are_explicit(monkeypatch):
    calls = []
    poi_setup(monkeypatch, lambda lat, lon, kind, radius, **kwargs: calls.append((kind, radius)) or [])
    out = poi.search_nearby_pois_impl('Watford', radius=1000,
        user_query='Primary schools, a GP and a supermarket within 1 km')
    assert set(out['completed_types']) == {'school', 'gp', 'supermarket'}
    assert set(out['empty_types']) == set(out['completed_types'])
    assert all(radius == poi.MAX_RADIUS for _, radius in calls)
    assert out['requested_radius_m'] == out['user_requested_radius_m'] == 1000
    assert out['radius_m'] == poi.MAX_RADIUS and out['partial']
    assert out['reference_point']['lat'] == 51.5
    assert out['category_status']['school']['status'] == 'empty'
    assert 'only' in out['note'] and 'not verify' in out['note']


def test_unsupported_category_never_queries_unrelated_restaurants(monkeypatch):
    poi_setup(monkeypatch, lambda *args, **kwargs: pytest.fail('Unsupported category must not issue a query'))
    out = poi.search_nearby_pois_impl('Watford', poi_type='airport')
    assert not out['success'] and out['unsupported_types'] == ['airport']
    assert out['executed_types'] == out['empty_types'] == []
    assert out['search_status'] == 'unsupported' and out['radius_m'] == poi.DEFAULT_RADIUS


def test_failed_category_does_not_erase_a_successful_category(monkeypatch):
    def lookup(lat, lon, kind, radius, **kwargs):
        if kind == 'gp':
            raise RuntimeError('Provider unavailable')
        return [{'name': 'Example School', 'icon': '🏫', 'distance_display': '100m'}]
    poi_setup(monkeypatch, lookup)
    out = poi.search_nearby_pois_impl('Watford', poi_type='school,gp,airport')
    assert out['success'] and out['partial']
    assert out['pois']['school'][0]['name'] == 'Example School'
    assert out['executed_types'] == ['school', 'gp'] and out['completed_types'] == ['school']
    assert out['failed_types'] == ['gp'] and out['empty_types'] == []
    assert out['unsupported_types'] == ['airport']
    assert out['category_status']['gp']['status'] == 'failed'
    assert 'not an empty result' in out['note']


def test_mentioned_unqueried_school_gp_are_not_reported_empty(monkeypatch):
    poi_setup(monkeypatch, lambda *args, **kwargs: [])
    out = poi.search_nearby_pois_impl('Watford', poi_type='supermarket',
        user_query='Please check primary schools, GP and a supermarket within 1 km')
    assert out['completed_types'] == out['empty_types'] == ['supermarket']
    assert set(out['unqueried_types']) == {'school', 'gp'}
    assert out['category_status']['school']['status'] == 'not_requested'
    assert out['radius_m'] == poi.DEFAULT_RADIUS and out['user_requested_radius_m'] == 1000
    assert 'NOT requested or queried' in out['note']


def test_budget_skipped_category_is_unknown_not_empty(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(poi.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(poi, 'poi_search_budget_s', lambda: 1.0)
    def lookup(*args, **kwargs):
        clock[0] = 2.0
        return []
    poi_setup(monkeypatch, lookup)
    out = poi.search_nearby_pois_impl('Watford', poi_type='school,gp')
    assert out['completed_types'] == out['empty_types'] == ['school']
    assert out['skipped_types'] == ['gp'] and out['executed_types'] == ['school']
    assert out['category_status']['gp']['status'] == 'skipped'


def test_school_gp_osm_queries_use_the_requested_categories(monkeypatch):
    calls = []
    def overpass(query, **kwargs):
        calls.append(query)
        return {'elements': [{'type': 'node', 'id': 123, 'lat': 51.5001, 'lon': -0.1001,
                             'tags': {'name': 'Example', 'amenity': 'school'}}]}
    monkeypatch.setattr(poi, 'overpass_request', overpass)
    school = poi.query_osm_pois(51.5, -0.1, 'school', radius=600)
    gp = poi.query_osm_pois(51.5, -0.1, 'gp', radius=600)
    assert '["amenity"="school"]' in calls[0]
    assert 'doctors|clinic' in calls[1] and 'around:600,51.5,-0.1' in calls[1]
    assert school[0]['osm_id'] == gp[0]['osm_id'] == 123
    assert school[0]['source'] == 'OpenStreetMap Overpass'


def test_geocode_failure_does_not_produce_empty_category_claims(monkeypatch):
    poi_setup(monkeypatch, lambda *args, **kwargs: pytest.fail('Geocoding failed'))
    monkeypatch.setattr(poi, 'geocode_address', lambda *args, **kwargs: None)
    out = poi.search_nearby_pois_impl('Unknown', poi_type='school,gp', radius=1000)
    assert not out['success'] and out['search_status'] == 'not_executed'
    assert out['reference_point'] is None
    assert out['executed_types'] == out['completed_types'] == out['empty_types'] == []
    assert out['radius_m'] == poi.MAX_RADIUS


@pytest.mark.parametrize('days', [0, 2, 3, 5])
def test_hybrid_cost_uses_office_days_and_average_month_in_both_tools(monkeypatch, days):
    monkeypatch.setattr(maps, 'calculate_travel_basis', lambda *args: {
        'duration_minutes': 30, 'source': 'TfL Journey Planner',
        'basis': 'tfl_journey_plan', 'basis_note': 'Provider journey',
    })
    monkeypatch.setattr(commute_cost, '_get_zone_from_address', lambda address:
                        1 if address == 'Office' else 2)
    route = commute_cost.calculate_commute_cost_impl('Home', 'Office', travel_type='adult',
                                                    office_days_per_week=days)
    checked = asyncio.run(transport_cost.check_transport_cost_impl(
        start_zone=1, end_zone=2, travel_type='adult', office_days_per_week=days))['data']
    priced = route['transport_cost']
    cap = priced['daily_cap']
    assert priced['payg_weekly_estimate'] == checked['payg_weekly_estimate'] == round(cap * days, 2)
    assert priced['payg_monthly_estimate'] == checked['payg_monthly_estimate'] == round(cap * days * 52 / 12, 2)
    assert priced['monthly_cost'] == min(priced['payg_monthly_estimate'], priced['monthly_travelcard'])
    assert priced['commuting_days_per_month'] == pytest.approx(days * 52 / 12)
    assert priced['payg_estimate_kind'] == 'daily_cap_upper_bound'
    assert not priced['coverage_verified'] and not checked['coverage_verified']
    assert route['office_days_per_week'] == days


def test_omitted_attendance_preserves_legacy_22_days():
    out = transport_cost._payg_commute_estimates(8.9)
    assert out['payg_monthly_estimate'] == 195.8 and out['commuting_days_per_month'] == 22
    assert out['office_days_per_week'] is None and out['payg_weekly_estimate'] is None
    assert out['frequency_basis'] == 'legacy_22_commuting_days_per_month'


@pytest.mark.parametrize('days', [-1, 8, float('nan'), float('inf'), True])
def test_invalid_office_days_fail_before_any_route_query(monkeypatch, days):
    monkeypatch.setattr(maps, 'calculate_travel_basis', lambda *args: pytest.fail('Invalid days must not query routes'))
    out = commute_cost.calculate_commute_cost_impl('Home', 'Office', office_days_per_week=days)
    assert not out['success'] and 'office_days_per_week' in out['error']
    out = asyncio.run(transport_cost.check_transport_cost_impl(office_days_per_week=days))
    assert not out['success']


def test_unknown_watford_zone_is_not_priced_as_zone_six_or_one_two(monkeypatch):
    monkeypatch.setattr(maps, 'calculate_travel_basis', lambda *args: {
        'duration_minutes': 32, 'source': 'TfL Journey Planner', 'basis': 'tfl_journey_plan',
    })
    monkeypatch.setattr(maps, 'geocode_address', lambda *args:
                        pytest.fail('An explicit unknown postcode must not be relocated'))
    out = commute_cost.calculate_commute_cost_impl('Watford Junction WD17 1EU',
        'Office WC1H 9BT', travel_type='adult', office_days_per_week=2)
    assert commute_cost._get_zone_from_postcode('WD17 1EU') is None
    assert out['transport_cost']['from_zone'] is None
    assert out['transport_cost']['zone_basis'] == 'not_established'
    assert 'monthly_cost' not in out['transport_cost']
    assert out['office_days_per_week'] == 2


def test_zone_one_prefix_does_not_misclassify_w10_or_sw19():
    assert commute_cost._get_zone_from_postcode('W10 5AB') != 1
    assert commute_cost._get_zone_from_postcode('SW19 1AA') != 1
    assert commute_cost._get_zone_from_postcode('W1F 8JT') == 1


def test_office_days_parameter_survives_tool_validation(monkeypatch):
    result = asyncio.run(transport_cost.check_transport_cost_tool.execute(
        start_zone=1, end_zone=2, travel_type='adult', office_days_per_week=2))
    assert result.success
    assert result.data['data']['office_days_per_week'] == 2
    assert result.data['data']['payg_monthly_estimate'] == 77.13


@pytest.mark.parametrize('origin,destination', [('Watford', 'Kings Cross'), ('Kings Cross', 'Watford')])
def test_travelcard_inference_requires_every_supplied_endpoint_zone(monkeypatch, origin, destination):
    monkeypatch.setattr(commute_cost, '_get_zone_from_address', lambda text:
                        None if text == 'Watford' else 1)
    out = transport._do_travelcard(None, origin, destination, 'adult')
    assert not out['success'] and not out['coverage_verified']
    assert out['unknown_zone_locations'] == ['Watford']
    assert 'monthly_gbp' not in out and 'zones' not in out


def test_explicit_travelcard_zone_is_only_a_hypothetical_price_table(monkeypatch):
    monkeypatch.setattr(commute_cost, '_get_zone_from_address', lambda text:
                        pytest.fail('Explicit table request need not geocode'))
    out = transport._do_travelcard(6, 'Watford Junction', 'Kings Cross', 'adult')
    assert out['success'] and out['monthly_gbp'] == 313.4
    assert not out['coverage_verified'] and out['zone_basis'] == 'explicit_zone_table'
    assert out['endpoint_zones'] == {} and out['coverage_scope'] == 'zonal_price_table_only'


def test_travelcard_out_of_range_zone_is_not_silently_clamped():
    out = transport._do_travelcard(9, '', '', 'adult')
    assert not out['success'] and not out['coverage_verified']
    assert 'monthly_gbp' not in out
