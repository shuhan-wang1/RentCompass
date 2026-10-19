"""
Tool 4: Get Weather Tool
获取地点的天气信息
"""

import requests
import re
import time
from datetime import date as calendar_date, datetime
from zoneinfo import ZoneInfo
from core.tool_system import Tool
from typing import Optional

# Open-Meteo weather_code -> human-readable condition
_WEATHER_CODE = {
    0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 71: "Light Snow", 73: "Snow",
    75: "Heavy Snow", 80: "Showers", 81: "Showers", 82: "Heavy Showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


_UK_SUFFIXES = {"uk", "u.k.", "united kingdom", "gb", "great britain", "britain", "england",
                "scotland", "wales", "northern ireland"}
_FOREIGN_COUNTRIES = {"usa", "us", "united states", "america", "canada", "australia", "ireland",
                      "france", "spain", "germany", "italy", "china", "india", "japan",
                      "new zealand", "south africa", "jamaica", "netherlands", "portugal"}


def _split_place_query(location: str) -> tuple[str, list[str], bool]:
    """("Boston, Lincolnshire, UK") -> ("Boston", ["lincolnshire"], False).

    Returns the bare name the geocoder matches, the remaining qualifiers (county, city,
    "London") used to choose among candidates, and whether the user named a non-UK country
    explicitly ("Richmond, Virginia, USA"), which is the only case searched worldwide."""
    parts = [p.strip() for p in str(location or "").split(",") if p.strip()]
    if not parts:
        return str(location or "").strip(), [], False
    name, rest = parts[0], [p.lower() for p in parts[1:]]
    foreign = any(p in _FOREIGN_COUNTRIES for p in rest)
    hints = [p for p in rest if p not in _UK_SUFFIXES and p not in _FOREIGN_COUNTRIES]
    words = name.split()
    if not rest and len(words) > 1 and words[-1].lower() in {"uk", "london"}:
        # "Richmond London" / "Reading UK" without a comma.
        hints = [words[-1].lower()] if words[-1].lower() == "london" else []
        name = " ".join(words[:-1])
    return name, hints, foreign


def _choose_place(results: list, hints: list[str], *, uk_only: bool):
    """The candidate matching every qualifier (admin areas or name), preferring GB; else the
    first GB candidate when there are no qualifiers. None when nothing fits."""
    pool = [r for r in results if isinstance(r, dict) and "latitude" in r and "longitude" in r]
    if uk_only:
        pool = [r for r in pool if str(r.get("country_code") or "GB").upper() == "GB"]
    if not pool:
        return None
    if not hints:
        return pool[0]

    def _matches(candidate, hint):
        fields = " | ".join(str(candidate.get(k) or "") for k in
                            ("name", "admin1", "admin2", "admin3", "admin4")).lower()
        if hint == "london":
            return "london" in fields
        return hint in fields
    for candidate in pool:
        if all(_matches(candidate, hint) for hint in hints):
            return candidate
    return None


def get_weather_impl(
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    target_date: Optional[str] = None,
    target_time: Optional[str] = None,
) -> dict:
    """
    获取地点的真实天气信息（Open-Meteo —— 免费、无需 API key）

    NOTE: this is a PLAIN SYNC function on purpose. It performs SYNCHRONOUS network I/O
    (requests.get to Open-Meteo geocoding + forecast). Registering it as sync means
    Tool.execute offloads it to an executor thread (tool_system.py :279-284), keeping the
    asyncio event loop responsive so the fc-loop's per-tool timeout / batch budget can fire.
    """
    request_started = False
    deadline = time.monotonic() + 12.0

    def http_timeout(requests_left=1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.exceptions.Timeout('Weather lookup time budget exhausted')
        return min(5.0, remaining / (2 * requests_left))

    try:
        forecast = target_date is not None or target_time is not None
        desired = None
        if forecast:
            if not isinstance(target_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', target_date):
                return {'success': False, 'error': 'A forecast requires target_date as YYYY-MM-DD.',
                        'error_code': 'invalid_forecast_time', 'retryable': False}
            day = calendar_date.fromisoformat(target_date)
            clock = target_time or '09:00'
            if not re.fullmatch(r'\d{2}:\d{2}', clock):
                raise ValueError('Invalid forecast time')
            desired = datetime.combine(day, datetime.strptime(clock, '%H:%M').time())
            zone = ZoneInfo('Europe/London')
            offsets = {aware.utcoffset() for fold in (0, 1)
                       if (aware := desired.replace(tzinfo=zone, fold=fold))
                       .astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None) == desired}
            if len(offsets) != 1:
                return {'success': False, 'error': 'The requested London time is ambiguous or nonexistent.',
                        'error_code': 'invalid_forecast_time', 'retryable': False}
        print(
            f"   🌤️  获取天气 location_chars={len(location)} "
            f"coordinates_supplied={latitude is not None and longitude is not None}"
        )

        canonical_location = location
        resolved_place = None
        # The product's bare Stratford area means east London. Resolve that
        # canonical UK query before asking for weather; the global geocoder's
        # first bare-name hit can be Stratford, Texas.
        request_started = True
        if latitude is None and longitude is None and location.strip().casefold() == 'stratford':
            from core.maps_service import geocode_address
            canonical_location = 'Stratford, London'
            http_timeout(2)  # Reject an exhausted budget before geocoding.
            geocode_deadline = time.monotonic() + (deadline - time.monotonic()) / 2
            geo = geocode_address(canonical_location, deadline=geocode_deadline)
            if not geo or not (51.25 <= float(geo.get('lat', 0)) <= 51.72 and
                               -0.56 <= float(geo.get('lng', 0)) <= 0.34):
                return {'success': False, 'error': 'Could not resolve Stratford, London for weather.',
                        'requested_location': location, 'canonical_location': canonical_location,
                        'retryable': True}
            latitude, longitude = geo['lat'], geo['lng']
        elif latitude is None or longitude is None:
            # UK first (R1-12): the global geocoder's first hit for "Richmond" was Richmond,
            # Virginia, returned with success=true, while "Reading, UK", "Newcastle upon
            # Tyne, UK" and "Boston, Lincolnshire, UK" did not resolve at all — the API
            # matches a bare place NAME, so the qualifiers have to be taken off and used
            # to choose among the GB candidates instead.
            name, hints, explicit_foreign = _split_place_query(location)
            chosen = None
            # GB first; a qualifier that no GB candidate carries ("Stratford, Texas") or an
            # explicit foreign country is then looked up worldwide, still by bare name.
            for uk_only in ((False,) if explicit_foreign else ((True, False) if hints else (True,))):
                g = requests.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": name, "count": 10, "language": "en",
                            **({"countryCode": "GB"} if uk_only else {})},
                    timeout=http_timeout(2),
                )
                results = (g.json() or {}).get("results") if g.status_code == 200 else None
                chosen = _choose_place(results or [], hints, uk_only=uk_only)
                if chosen is not None:
                    break
            if chosen is None:
                return {"success": False, "requested_location": location,
                        "error": f"Could not resolve {location} to a UK place for weather.",
                        "error_code": "location_unresolved", "retryable": False}
            latitude = chosen["latitude"]
            longitude = chosen["longitude"]
            resolved_place = ", ".join(dict.fromkeys(
                str(part) for part in (chosen.get("name"), chosen.get("admin2") or chosen.get("admin1"),
                                       "UK" if str(chosen.get("country_code") or "").upper() == "GB"
                                       else chosen.get("country"))
                if part)) or None

        params = {
            "latitude": latitude, "longitude": longitude,
            "timezone": "Europe/London", "wind_speed_unit": "kmh",
        }
        variables = ('temperature_2m,relative_humidity_2m,apparent_temperature,'
                     'precipitation,weather_code,wind_speed_10m')
        if forecast:
            params.update(hourly=variables + ',uv_index,precipitation_probability',
                          start_date=target_date, end_date=target_date)
        else:
            params.update(current=variables, hourly='uv_index')
        w = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=http_timeout())
        w.raise_for_status()
        data = w.json()
        hourly = data.get('hourly') or {}
        times = hourly.get('time') or []
        if forecast:
            slots = [(index, datetime.fromisoformat(value)) for index, value in enumerate(times)
                     if str(value).startswith(target_date)]
            slots = [(index, value) for index, value in slots
                     if abs((value - desired).total_seconds()) <= 3600]
            if not slots:
                return {'success': False, 'weather_type': 'forecast', 'source': 'Open-Meteo',
                        'error': 'Open-Meteo did not return a forecast near the requested hour.',
                        'requested_at': desired.replace(tzinfo=ZoneInfo('Europe/London')).isoformat(),
                        'timezone': 'Europe/London',
                        'retryable': True}
            index, valid = min(slots, key=lambda row: abs((row[1] - desired).total_seconds()))
            c = {key: values[index] for key, values in hourly.items()
                 if isinstance(values, list) and len(values) > index}
            if c.get('temperature_2m') is None or c.get('weather_code') is None:
                return {'success': False, 'weather_type': 'forecast', 'source': 'Open-Meteo',
                        'error': 'The requested forecast has missing temperature or weather condition.',
                        'retryable': True}
            valid_at = valid.replace(tzinfo=ZoneInfo('Europe/London')).isoformat()
            uv_index = c.get('uv_index')
        else:
            c = data.get("current", {})
            if c.get('temperature_2m') is None or c.get('weather_code') is None:
                return {'success': False, 'weather_type': 'current', 'source': 'Open-Meteo',
                        'error': 'Open-Meteo returned no usable current weather.', 'retryable': True}
            valid_at = c.get('time')
            index = next((i for i, value in enumerate(times)
                          if str(value)[:13] == str(valid_at)[:13]), None)
            values = hourly.get('uv_index') or []
            uv_index = values[index] if index is not None and len(values) > index else None
        precip = c.get("precipitation")
        wet = ((isinstance(precip, (int, float)) and precip > 0) or
               c.get('weather_code') in {51, 53, 55, 61, 63, 65, 71, 73, 75, 80, 81, 82, 95, 96, 99})
        result = {
            'success': True, 'weather_type': 'forecast' if forecast else 'current',
            'source': 'Open-Meteo', 'timezone': data.get('timezone') or 'Europe/London',
            'valid_at': valid_at,
            "location": canonical_location,
            'requested_location': location,
            'canonical_location': canonical_location,
            # Which place the geocoder actually resolved (R1-12): "Richmond, Greater London,
            # UK", so an answer can say which Richmond it is reporting.
            'resolved_place': resolved_place,
            "latitude": latitude,
            "longitude": longitude,
            "temperature": c.get("temperature_2m"),
            "condition": _WEATHER_CODE.get(c.get("weather_code"), "Unknown"),
            "humidity": c.get("relative_humidity_2m"),
            "wind_speed": c.get("wind_speed_10m"),
            "uv_index": uv_index,
            "feels_like": c.get("apparent_temperature"),
            "rainfall_chance": c.get('precipitation_probability'),
            "recommendation": ('可能有降水，看房记得带伞' if wet else
                               '未报告降水，外出前可再次核对天气' if precip == 0 else
                               '未返回降水数据'),
        }
        if forecast:
            result.update(forecast_temp=c.get('temperature_2m'),
                          requested_at=desired.replace(tzinfo=ZoneInfo('Europe/London')).isoformat(),
                          assumed_time=target_time is None,
                          time_note=('09:00 London local time used for the requested morning.'
                                     if target_time is None else
                                     'Nearest available hourly forecast; valid_at is the forecast hour.'))
        else:
            result['current_temp'] = c.get('temperature_2m')
        return result

    except ValueError:
        if request_started:
            return {'success': False, 'error': 'Weather service returned an invalid response.',
                    'retryable': True}
        return {'success': False, 'error': 'Invalid weather date/time or provider response.',
                'error_code': 'invalid_forecast_time', 'retryable': False}

    except Exception as exc:
        print(f"   ❌ 天气获取出错 exception_type={type(exc).__name__}")
        return {"success": False, "error": "天气查询失败", "retryable": True}


# 创建工具实例
get_weather_tool = Tool(
    name="get_weather",
    
    description="""Get current weather by default, or an hourly forecast when target_date is supplied. For tomorrow/morning/viewing-day questions pass the requested target_date (YYYY-MM-DD, Europe/London) and optional target_time (HH:MM); omitted forecast time uses 09:00 and is labelled assumed_time. weather_type and valid_at distinguish current conditions from a forecast; never present current weather as tomorrow's forecast. Returns temperature, condition, humidity, wind speed, rainfall chance and UV.
获取指定地点的天气信息。""",
    
    func=get_weather_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'location': {
                'type': 'string',
                'description': '地点名称（如 Bloomsbury, London）'
            },
            'latitude': {
                'type': 'number',
                'description': '纬度（可选）'
            },
            'longitude': {
                'type': 'number',
                'description': '经度（可选）'
            },
            'target_date': {'type': 'string', 'format': 'date',
                            'description': 'Requested forecast date (YYYY-MM-DD), Europe/London.'},
            'target_time': {'type': 'string', 'pattern': '^([01][0-9]|2[0-3]):[0-5][0-9]$',
                            'description': 'Requested London local time, HH:MM; requires target_date. Default forecast morning hour: 09:00.'},
        },
        'required': ['location']
    },
    
    max_retries=2
)
