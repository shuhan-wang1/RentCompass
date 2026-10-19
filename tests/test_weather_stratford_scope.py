"""Offline replay of the bare Stratford weather and weather/eat POI defects."""
from types import SimpleNamespace

import pytest
import requests

from core import maps_service as maps
from core.tools import get_weather as weather
from core.tools import search_nearby_pois as pois


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


CURRENT = {'current': {'time': '2026-10-19T12:00', 'temperature_2m': 13, 'weather_code': 3}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No real HTTP in weather geography regressions')

    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(maps, '_read_versioned_cache', lambda *a, **k: ('miss', None))
    monkeypatch.setattr(maps, '_write_versioned_cache', lambda *a, **k: None)


def test_bare_stratford_resolves_london_before_requesting_weather(monkeypatch):
    resolved, fetched = [], []
    monkeypatch.setattr(maps, 'geocode_address', lambda query, **kwargs:
                        resolved.append((query, kwargs)) or {'lat': 51.5401, 'lng': 0.0102})

    def get(url, params, **kwargs):
        fetched.append((url, params))
        if 'geocoding-api' in url:
            pytest.fail('The ambiguous global first hit must not be requested')
        return Response(CURRENT)

    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.get_weather_impl('Stratford')
    assert result['success']
    assert result['location'] == result['canonical_location'] == 'Stratford, London'
    assert result['requested_location'] == 'Stratford'
    assert resolved[0][0] == 'Stratford, London' and 'deadline' in resolved[0][1]
    assert fetched[0][1]['latitude'] == 51.5401 and fetched[0][1]['longitude'] == 0.0102


@pytest.mark.parametrize('location', ['Stratford-upon-Avon', 'Stratford, Texas'])
def test_explicit_other_stratford_keeps_its_geocoding_query(monkeypatch, location):
    queried = []
    monkeypatch.setattr(maps, 'geocode_address', lambda *a, **k: pytest.fail('No London override'))

    def get(url, params, **kwargs):
        if 'geocoding-api' in url:
            queried.append(params['name'])
            # R1-12: the geocoder is asked for the bare NAME, GB first; the Texas qualifier
            # then selects the worldwide candidate that carries it.
            texas = {'latitude': 36.33614, 'longitude': -102.07212, 'name': 'Stratford',
                     'country_code': 'US', 'admin1': 'Texas'}
            avon = {'latitude': 36.33614, 'longitude': -102.07212, 'name': location,
                    'country_code': 'GB', 'admin1': 'England'}
            pool = [texas, avon] if location == 'Stratford, Texas' else [avon]
            if params.get('countryCode'):
                pool = [r for r in pool if r['country_code'] == params['countryCode']]
            return Response({'results': pool})
        return Response(CURRENT)

    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.get_weather_impl(location)
    assert result['success'] and queried[-1] == location.split(',')[0]
    assert result['location'] == result['canonical_location'] == location
    assert result['latitude'] == 36.33614


def test_explicit_coordinates_are_preserved_even_with_bare_stratford(monkeypatch):
    monkeypatch.setattr(maps, 'geocode_address', lambda *a, **k: pytest.fail('No coordinate override'))

    def get(url, params, **kwargs):
        assert 'geocoding-api' not in url
        assert params['latitude'] == 36.33614 and params['longitude'] == -102.07212
        return Response(CURRENT)

    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.get_weather_impl('Stratford', 36.33614, -102.07212)
    assert result['success'] and result['latitude'] == 36.33614
    assert result['canonical_location'] == 'Stratford'


def test_failed_london_resolution_does_not_fall_back_to_a_global_stratford(monkeypatch):
    monkeypatch.setattr(maps, 'geocode_address', lambda *a, **k: None)
    monkeypatch.setattr(weather.requests, 'get', lambda *a, **k: pytest.fail('No global fallback'))
    result = weather.get_weather_impl('Stratford')
    assert not result['success'] and result['canonical_location'] == 'Stratford, London'


def test_maps_optional_deadline_is_shared_by_postcode_and_nominatim(monkeypatch):
    clock, seen = [100.0], []
    monkeypatch.setattr(maps.time, 'monotonic', lambda: clock[0])

    def get(url, timeout, **kwargs):
        seen.append(timeout)
        if 'postcodes.io' in url:
            clock[0] += 2
            return SimpleNamespace(status_code=404)
        clock[0] += timeout * 2
        return Response([{'lat': '51.54', 'lon': '0.01'}])

    monkeypatch.setattr(maps.requests, 'get', get)
    result = maps.geocode_address('Stratford, London E15 1AA', deadline=104)
    assert result['lat'] == 51.54 and seen == [2, 1]
    assert clock[0] <= 104


def test_canonical_geocoding_and_weather_remain_inside_twelve_seconds(monkeypatch):
    clock, seen = [100.0], []
    monkeypatch.setattr(maps.time, 'monotonic', lambda: clock[0])

    def get(url, timeout, **kwargs):
        seen.append(timeout)
        clock[0] += timeout * 2
        if 'nominatim' in url:
            return Response([{'lat': '51.54', 'lon': '0.01'}])
        assert 'geocoding-api' not in url
        raise requests.exceptions.ReadTimeout('Offline weather timeout')

    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.get_weather_impl('Stratford')
    assert not result['success'] and result['retryable']
    assert len(seen) == 2 and clock[0] <= 112


def test_original_family_r7_school_gp_and_weather_do_not_infer_restaurants():
    message = (
        'We are considering viewing Tennyson Road, Stratford — '
        'https://www.onthemarket.com/details/20489843/ — tomorrow afternoon. '
        'Keeping the housing search unchanged, please find primary schools and GP surgeries '
        "within 1 km for our seven-year-old and explain recorded crime for this home's actual "
        'location, including reference point, radius and data months. Nearby does not guarantee '
        'admission or GP registration. Also check the weather for the viewing and the '
        "public-transport trip from this home to King's Cross. Please be clear if that route's "
        'step-free access is not actually verified for my wheelchair.')
    assert pois._infer_poi_types_from_query(message) == ['school', 'gp']
    assert 'restaurant' in pois._infer_poi_types_from_query('Where can I eat nearby?')
