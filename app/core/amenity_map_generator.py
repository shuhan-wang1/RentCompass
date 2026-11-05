"""
Property Amenity Map Generator
Integrates OpenStreetMap visualization for property recommendations
"""

import os

import folium
from branca.element import MacroElement
from folium import plugins
from folium.template import Template
import time
from typing import Dict, List, Tuple, Optional
import math
from pathlib import Path

# Leaflet renders popup/tooltip content as innerHTML, so every value we
# interpolate into popup HTML must be HTML-escaped. Both the client-supplied
# address and the OSM-derived amenity fields (name/cuisine/opening_hours) are
# attacker-controllable (OSM POI names are publicly editable), so an unescaped
# f-string here is a stored-XSS sink. markupsafe.escape ships with Flask/Jinja.
from markupsafe import escape

from .maps_service import overpass_request, OverpassError
from .cache_service import get_from_cache, set_to_cache, create_cache_key

# Amenities change slowly, so POI results are cached for a week keyed by rounded
# coordinates + radius. This keeps repeat map generations (and live tests) off
# the shared Overpass servers.
POI_CACHE_TTL_SECONDS = 7 * 24 * 3600

# Map tiles come through this site, not straight from tile.openstreetmap.org. The map is shown
# in a sandboxed srcdoc iframe without allow-same-origin, so its document has an opaque origin
# and the browser sends no Referer with tile requests; OSM's tile servers answer those with an
# "Access blocked" image. The edge (or app.py's /tiles/osm route) fetches the tile with this
# site's identity and caches it. A relative URL resolves against the parent page's address
# because a srcdoc document inherits its base URL.
MAP_TILE_URL = os.getenv("MAP_TILE_URL", "/tiles/osm/{z}/{x}/{y}.png")
MAP_TILE_ATTRIBUTION = ('&copy; <a href="https://www.openstreetmap.org/copyright" '
                        'target="_blank" rel="noopener">OpenStreetMap</a> contributors')

# Amenity layers switched on when the map opens. The rest start off but stay in the layer
# control: ten categories at once piled hundreds of markers onto a busy street (2026-11-05).
DEFAULT_VISIBLE_CATEGORIES = ('supermarkets', 'metro_stations', 'parks')
# Amenity clusters split into single markers from this zoom level on (street level).
CLUSTER_OFF_AT_ZOOM = 18
# Leaflet orders markers by screen y plus this offset; the listing's marker stays on top.
PROPERTY_MARKER_Z_OFFSET = 10000
# A base map with no tile loaded after this long is reported as failed.
TILE_TIMEOUT_MS = 15000

MAP_NOTICES_ID = 'rc-map-notices'
_NOTICE_STACK_STYLE = (
    'position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 10000; '
    'display: flex; flex-direction: column; align-items: center; gap: 8px; '
    'width: max-content; max-width: 90%; pointer-events: none;')
_NOTICE_STYLE = (
    'padding: 10px 18px; border-radius: 6px; font-family: Arial, sans-serif; '
    'font-weight: bold; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.25); '
    'pointer-events: auto;')

_MAP_TEXT = {
    'en': {
        'property_label': 'This property',
        'amenities_unavailable': ('Nearby amenity data is temporarily unavailable — '
                                  'showing property location only.'),
        'tiles_failed': 'The base map could not be loaded. The markers still work.',
        'tiles_partly_failed': 'Parts of the base map could not be loaded.',
        'tiles_retrying': 'Loading the base map again…',
        'retry': 'Retry',
    },
    'zh': {
        'property_label': '本房源',
        'amenities_unavailable': '周边设施数据暂时无法获取，仅显示房源位置。',
        'tiles_failed': '底图未能加载，标记仍可正常使用。',
        'tiles_partly_failed': '部分底图未能加载。',
        'tiles_retrying': '正在重新加载底图…',
        'retry': '重试',
    },
}


def _map_text(lang) -> dict:
    """The map's own strings in the UI language ('zh…' → Chinese, anything else → English)."""
    return _MAP_TEXT['zh' if str(lang or '').strip().lower().startswith('zh') else 'en']


def _cluster_icon_js(color: str) -> str:
    """A Leaflet iconCreateFunction drawing a cluster bubble in its category's colour,
    so a bubble says which layer it belongs to. ``color`` comes from amenity_config."""
    return (
        "function (cluster) {\n"
        "    return L.divIcon({\n"
        "        html: '<div style=\"background-color: " + color + "; color: #fff; "
        "font-weight: bold; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0,0,0,0.4);\">"
        "<span>' + cluster.getChildCount() + '</span></div>',\n"
        "        className: 'marker-cluster',\n"
        "        iconSize: new L.Point(40, 40)\n"
        "    });\n"
        "}"
    )


class _TileFailureNotice(MacroElement):
    """A visible notice with a Retry button when the base-map tiles do not load.

    Leaflet reports each failed tile with ``tileerror`` and the end of a batch with
    ``load``; a batch where no tile has loaded after TILE_TIMEOUT_MS counts as failed too.
    Retry redraws the tile layer in place, without reloading the page. The notice only
    covers its own strip of the map, so the markers stay usable while tiles are down.
    """

    _template = Template("""
        {% macro script(this, kwargs) %}
        (function (map, layer, text) {
            var stack = document.getElementById(text.stack_id);
            if (stack) {
                map.getContainer().appendChild(stack);
                stack.style.position = 'absolute';
            }
            var box = null, label = null, button = null;
            var loaded = 0, failed = 0, watchdog = null;
            function show(message, canRetry) {
                if (!box) {
                    box = L.DomUtil.create('div', 'rc-tile-notice');
                    box.setAttribute('role', 'alert');
                    box.style.cssText = text.box_style;
                    label = L.DomUtil.create('span', 'rc-tile-notice-text', box);
                    button = L.DomUtil.create('button', 'rc-tile-notice-retry', box);
                    button.type = 'button';
                    button.style.cssText = text.button_style;
                    button.textContent = text.retry;
                    L.DomEvent.disableClickPropagation(box);
                    L.DomEvent.disableScrollPropagation(box);
                    L.DomEvent.on(button, 'click', retry);
                    (stack || map.getContainer()).appendChild(box);
                }
                label.textContent = message;
                button.disabled = !canRetry;
                button.style.opacity = canRetry ? '1' : '0.6';
                box.style.display = 'flex';
            }
            function hide() { if (box) box.style.display = 'none'; }
            function reportFailure() {
                show(loaded ? text.tiles_partly_failed : text.tiles_failed, true);
            }
            function arm() {
                clearTimeout(watchdog);
                watchdog = setTimeout(function () {
                    if (!loaded) show(text.tiles_failed, true);
                }, text.timeout_ms);
            }
            function retry() {
                loaded = 0;
                failed = 0;
                show(text.tiles_retrying, false);
                arm();
                layer.redraw();
            }
            layer.on('loading', function () { loaded = 0; failed = 0; arm(); });
            layer.on('tileload', function () { loaded += 1; });
            layer.on('tileerror', function () { failed += 1; reportFailure(); });
            layer.on('load', function () {
                clearTimeout(watchdog);
                if (failed) reportFailure(); else hide();
            });
            arm();
        })({{ this._parent.get_name() }}, {{ this.layer.get_name() }}, {{ this.text|tojson }});
        {% endmacro %}
    """)

    def __init__(self, layer, text: dict):
        super().__init__()
        self._name = 'TileFailureNotice'
        self.layer = layer
        self.text = {
            'stack_id': MAP_NOTICES_ID,
            'tiles_failed': text['tiles_failed'],
            'tiles_partly_failed': text['tiles_partly_failed'],
            'tiles_retrying': text['tiles_retrying'],
            'retry': text['retry'],
            'timeout_ms': TILE_TIMEOUT_MS,
            'box_style': (_NOTICE_STYLE + ' display: flex; align-items: center; '
                          'justify-content: center; flex-wrap: wrap; gap: 10px; '
                          'background: #fdecea; color: #7f1d1d; border: 2px solid #ef4444;'),
            'button_style': ('padding: 6px 14px; border: none; border-radius: 4px; '
                             'background: #b91c1c; color: #fff; font-weight: bold; '
                             'cursor: pointer; font-family: inherit;'),
        }


class PropertyAmenityMapGenerator:
    """
    Generate interactive maps showing properties and nearby amenities
    using OpenStreetMap data.
    """
    
    def __init__(self, radius_km: float = 1.5):
        """
        Initialize the map generator.
        
        Args:
            radius_km: Radius in kilometers to search for amenities (default: 1.5)
        """
        self.radius_km = radius_km
        self.radius_m = radius_km * 1000  # Convert to meters
        
        # Amenity categories and their OSM tags. 'color' is the marker colour
        # (Leaflet.awesome-markers names, also used as a CSS colour in popups);
        # 'cluster_color' fills that category's cluster bubbles (white text on it).
        self.amenity_config = {
            'supermarkets': {
                'osm_type': 'supermarket',
                'tags': {'shop': 'supermarket'},
                'color': 'blue',
                'cluster_color': '#2a81cb',
                'icon': 'shopping-cart',
                'name': 'Supermarkets'
            },
            'convenience_stores': {
                'osm_type': 'convenience',
                'tags': {'shop': 'convenience'},
                'color': 'green',
                'cluster_color': '#2a8a27',
                'icon': 'shopping-basket',
                'name': 'Convenience Stores'
            },
            'restaurants_chinese': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'chinese',
                'color': 'darkred',
                'cluster_color': '#a23336',
                'icon': 'cutlery',
                'name': 'Chinese Restaurants'
            },
            'restaurants_italian': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'italian',
                'color': 'cadetblue',
                'cluster_color': '#436978',
                'icon': 'cutlery',
                'name': 'Italian Restaurants'
            },
            'restaurants_indian': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'indian',
                'color': 'orange',
                'cluster_color': '#c26a00',
                'icon': 'cutlery',
                'name': 'Indian Restaurants'
            },
            'cafes': {
                'osm_type': 'cafe',
                'tags': {'amenity': 'cafe'},
                'color': 'lightblue',
                'cluster_color': '#2b7fb0',
                'icon': 'coffee',
                'name': 'Cafés'
            },
            'parks': {
                'osm_type': 'park',
                'tags': {'leisure': 'park'},
                'color': 'darkgreen',
                'cluster_color': '#3f6b2a',
                'icon': 'tree',
                'name': 'Parks'
            },
            'pharmacies': {
                'osm_type': 'pharmacy',
                'tags': {'amenity': 'pharmacy'},
                'color': 'purple',
                'cluster_color': '#8e3aa6',
                'icon': 'plus-sign',
                'name': 'Pharmacies'
            },
            'banks': {
                'osm_type': 'bank',
                'tags': {'amenity': 'bank'},
                'color': 'darkblue',
                'cluster_color': '#0b4f8a',
                'icon': 'gbp',
                'name': 'Banks'
            },
            'metro_stations': {
                'osm_type': 'station',
                'tags': {'railway': 'station'},
                'color': 'gray',
                'cluster_color': '#555555',
                'icon': 'train',
                'name': 'Metro Stations'
            }
        }
        
    def parse_geo_location(self, geo_location_str: str) -> Optional[Tuple[float, float]]:
        """
        Parse geo_location string into (lat, lon) tuple.
        
        Args:
            geo_location_str: String in format "lat, lon" or dict with lat/lng keys
            
        Returns:
            Tuple of (latitude, longitude) or None if parsing fails
        """
        if not geo_location_str:
            return None
        
        try:
            # Handle string format: "51.5525, -0.1350"
            if isinstance(geo_location_str, str):
                parts = geo_location_str.strip().split(',')
                if len(parts) == 2:
                    lat = float(parts[0].strip())
                    lon = float(parts[1].strip())
                    
                    # Validate coordinates are within UK bounds
                    if 50.0 <= lat <= 59.0 and -8.0 <= lon <= 2.0:
                        return (lat, lon)
            # Handle dict format: {'lat': 51.5525, 'lng': -0.1350}
            elif isinstance(geo_location_str, dict):
                lat = float(geo_location_str.get('lat', 0))
                lon = float(geo_location_str.get('lng') or geo_location_str.get('lon', 0))
                if 50.0 <= lat <= 59.0 and -8.0 <= lon <= 2.0:
                    return (lat, lon)
        except (ValueError, AttributeError, TypeError) as e:
            print(f"    ✗ Failed to parse coordinates; error_type={type(e).__name__}")
            return None
        
        return None
    
    def query_osm_amenities_with_filter(self, lat: float, lon: float, 
                                        amenity_type: str, 
                                        cuisine_filter: str = None) -> List[dict]:
        """
        Query OpenStreetMap for amenities with optional cuisine filter.
        This replicates the original property_amenity_map.py query logic.
        
        Args:
            lat: Latitude of the center point
            lon: Longitude of the center point
            amenity_type: Type of amenity config key
            cuisine_filter: Optional cuisine type to filter (e.g., 'chinese', 'italian')
            
        Returns:
            List of amenity dictionaries
        """
        config = self.amenity_config[amenity_type]

        # Build Overpass QL query using tags from config (node+way+relation)
        selector = ''.join(f'["{key}"="{value}"]' for key, value in config['tags'].items())
        overpass_query = (
            "[out:json][timeout:25];\n"
            "(\n"
            f"  nwr{selector}(around:{self.radius_m},{lat},{lon});\n"
            ");\n"
            "out center;"
        )

        # Routed through the shared, UA-carrying, mirror-rotating client. Provider
        # failures raise OverpassError; callers that want an honest banner should
        # prefer fetch_all_amenities (which propagates it). Here we keep the legacy
        # list contract and return [] on total outage.
        try:
            data = overpass_request(overpass_query, timeout=25)
        except OverpassError as e:
            print(f"    Amenity query failed; error_type={type(e).__name__}")
            return []

        amenities = []
        for element in data.get('elements', []):
            amenity = self._element_to_amenity(element, lat, lon, cuisine_filter)
            if amenity is not None:
                amenities.append(amenity)

        amenities.sort(key=lambda a: a.get('distance_m', 0))
        print(f"    Found {len(amenities)} {config['name']}")
        return amenities

    def _element_to_amenity(self, element: dict, origin_lat: float, origin_lon: float,
                            cuisine_filter: str = None) -> Optional[dict]:
        """Convert one Overpass element into an amenity dict, or None if it should
        be skipped (missing coords, or failing the cuisine filter)."""
        tags = element.get('tags', {})

        if cuisine_filter:
            cuisine = (tags.get('cuisine', '') or '').lower()
            if cuisine_filter not in cuisine:
                return None

        # Coordinates: nodes carry lat/lon directly; ways/relations carry 'center'.
        if element.get('type') == 'node' and 'lat' in element:
            a_lat, a_lon = element['lat'], element['lon']
        elif 'center' in element:
            a_lat, a_lon = element['center']['lat'], element['center']['lon']
        else:
            return None

        amenity = {
            'name': tags.get('name', 'Unnamed'),
            'lat': a_lat,
            'lon': a_lon,
            'distance_m': self._distance_m(origin_lat, origin_lon, a_lat, a_lon),
        }
        if 'cuisine' in tags:
            amenity['cuisine'] = tags['cuisine']
        if 'opening_hours' in tags:
            amenity['opening_hours'] = tags['opening_hours']
        return amenity

    @staticmethod
    def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
        """Great-circle distance between two points in whole metres (Haversine)."""
        R = 6371000
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lng = math.radians(lon2 - lon1)
        a = (math.sin(delta_lat / 2) ** 2
             + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return int(R * c)

    @staticmethod
    def _degradation_banner_html(lang: str = 'en') -> str:
        """Warning shown in the map's notice stack when the amenity provider is down."""
        return (f'<div role="status" style="{_NOTICE_STYLE} background: #fff3cd; '
                f'color: #856404; border: 2px solid #ffc107;">'
                f'&#9888; {_map_text(lang)["amenities_unavailable"]}</div>')

    def _build_combined_query(self, lat: float, lon: float) -> str:
        """Build ONE Overpass query unioning every distinct tag selector across
        all amenity categories. The three restaurant categories share the same
        ``amenity=restaurant`` selector and are split apart later by cuisine, so
        the query stays compact (one union, one round-trip, no per-category
        sleep)."""
        seen = set()
        parts = []
        for config in self.amenity_config.values():
            tag_items = tuple(sorted(config['tags'].items()))
            if tag_items in seen:
                continue
            seen.add(tag_items)
            selector = ''.join(f'["{k}"="{v}"]' for k, v in tag_items)
            parts.append(f'  nwr{selector}(around:{self.radius_m},{lat},{lon});')
        return (
            "[out:json][timeout:25];\n"
            "(\n" + "\n".join(parts) + "\n);\n"
            "out center;"
        )

    def _classify_elements(self, elements: List[dict], lat: float, lon: float
                           ) -> Dict[str, List[dict]]:
        """Assign each returned element to every amenity category whose tag +
        cuisine filter it satisfies (one restaurant can match at most one cuisine
        category, but the loop is general). Returns the {category: [amenity]}
        shape the map renderer expects, each list sorted by distance."""
        result: Dict[str, List[dict]] = {key: [] for key in self.amenity_config}
        for element in elements:
            tags = element.get('tags', {})
            for key, config in self.amenity_config.items():
                if not all(tags.get(k) == v for k, v in config['tags'].items()):
                    continue
                amenity = self._element_to_amenity(
                    element, lat, lon, config.get('cuisine_filter')
                )
                if amenity is not None:
                    result[key].append(amenity)
        for key in result:
            result[key].sort(key=lambda a: a.get('distance_m', 0))
        return result

    def fetch_all_amenities(self, lat: float, lon: float) -> Dict[str, List[dict]]:
        """Fetch every amenity category around (lat, lon) in a SINGLE batched
        Overpass query, cached by rounded coords + radius (7-day TTL).

        Returns {category_key: [amenity, ...]} for all configured categories
        (empty lists are legitimate "none nearby"). Raises OverpassError if every
        mirror is unreachable, so the caller can render an honest degradation
        banner instead of a silently-empty map.
        """
        cache_key = create_cache_key(
            'amenity_map_pois_v1', round(lat, 3), round(lon, 3), int(self.radius_m)
        )
        cached = get_from_cache(cache_key)
        if cached and (time.time() - cached.get('fetched_at', 0)) < POI_CACHE_TTL_SECONDS:
            cached_amenities = cached.get('amenities') or {}
            cached_total = sum(len(v) for v in cached_amenities.values())
            # Only trust a cached HIT that actually carries amenities. A cached
            # all-zero result is almost certainly a poisoned entry from an earlier
            # busy-mirror empty/remark 200 (a real UK address within 1.5km never
            # has zero across all 8 selectors). Ignore it and re-fetch rather than
            # serve a silently-empty map for the rest of the 7-day TTL.
            if cached_total > 0:
                print("  -> [Cache HIT] amenity POIs; coordinates_present=True")
                return cached_amenities

        query = self._build_combined_query(lat, lon)
        # expect_nonempty: this batched union spans 8 selectors around a populated
        # UK address, so an empty 200 from a busy mirror is an outage, not reality.
        # overpass_request rotates past such mirrors and only raises OverpassError
        # if EVERY mirror fails/returns empty.
        data = overpass_request(query, timeout=30, expect_nonempty=True)
        amenities = self._classify_elements(data.get('elements', []), lat, lon)

        total = sum(len(v) for v in amenities.values())
        # A batched union of ALL 8 distinct selectors (supermarkets, convenience,
        # restaurants, cafes, parks, pharmacies, banks, stations) within 1.5km of
        # a real UK address returning ZERO is not a genuine "nothing nearby" -- it
        # is a mirror that answered HTTP 200 with an empty/partial body but no
        # remark. Never cache that (it would poison the cell for 7 days and render
        # a marker-only map with no banner). Raise OverpassError instead so the
        # handler shows an honest degradation banner and the next call retries a
        # fresh mirror. (A truly-empty rural cell is a judgement call, but zero
        # across all 8 categories in 1.5km of a geocoded UK property is
        # effectively impossible in practice.)
        if total == 0:
            raise OverpassError(
                f"Overpass returned 0 amenities across {len(amenities)} categories; "
                "treating as a provider failure and not caching."
            )
        print(f"  -> [Overpass] Batched fetch returned {total} amenities across "
              f"{len(amenities)} categories")
        set_to_cache(cache_key, {'fetched_at': time.time(), 'amenities': amenities})
        return amenities

    def _build_map(self, coords: Tuple[float, float], property_data: dict,
                   amenities_data: Dict[str, List[dict]], amenities_unavailable: bool,
                   lang: str) -> folium.Map:
        """The folium map both public builders render: base tiles with a visible notice
        and a Retry button when they fail, the listing's own marker above everything,
        and one clustered layer per amenity category (only a few switched on)."""
        lat, lon = coords
        text = _map_text(lang)

        m = folium.Map(location=[lat, lon], zoom_start=15, tiles=None)
        tile_layer = folium.TileLayer(
            tiles=MAP_TILE_URL,
            attr=MAP_TILE_ATTRIBUTION,
            name='OpenStreetMap',
            max_zoom=19,
        ).add_to(m)

        # Notices sit in one stack so the amenity banner and the tile notice never cover
        # each other. _TileFailureNotice moves the stack into the map container, so it
        # stays visible in full-screen mode too.
        banner = self._degradation_banner_html(lang) if amenities_unavailable else ''
        m.get_root().html.add_child(folium.Element(
            f'<div id="{MAP_NOTICES_ID}" style="{_NOTICE_STACK_STYLE}">{banner}</div>'))
        # Added right after the tile layer, so its script runs before any amenity layer:
        # the notice still works if a later part of the map script fails.
        _TileFailureNotice(tile_layer, text).add_to(m)

        # The listing's own marker: never clustered, drawn above every amenity marker
        # and cluster bubble, and labelled permanently so it is easy to find.
        prop_price = escape(property_data.get('Price', property_data.get('price', 'N/A')))
        prop_address = escape(property_data.get('Address', property_data.get('address', 'N/A')))
        prop_travel = escape(property_data.get('travel_time_minutes', property_data.get('travel_time', 'N/A')))
        property_popup = f"""
        <div style="font-family: Arial; min-width: 200px;">
            <h4 style="margin-bottom: 10px; color: #2c3e50;">🏠 Property</h4>
            <b>Price:</b> {prop_price}<br>
            <b>Address:</b> {prop_address}<br>
            <b>Travel Time:</b> {prop_travel} min<br>
        </div>
        """
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(property_popup, max_width=300),
            tooltip=folium.Tooltip(text['property_label'], permanent=True, sticky=False,
                                   direction='top', offset=(0, -40)),
            icon=folium.Icon(color='red', icon='home', prefix='fa'),
            z_index_offset=PROPERTY_MARKER_Z_OFFSET,
        ).add_to(m)

        folium.Circle(
            location=[lat, lon],
            radius=self.radius_m,
            color='red',
            fill=False,
            opacity=0.3,
            weight=2,
            tooltip=f'{self.radius_km}km search radius'
        ).add_to(m)

        # One cluster layer per category. A busy street used to put hundreds of
        # overlapping markers on the map (2026-11-05); clusters split apart as the user
        # zooms in, and only DEFAULT_VISIBLE_CATEGORIES start switched on. The others
        # stay one click away in the layer control.
        for amenity_type, config in self.amenity_config.items():
            amenities = amenities_data.get(amenity_type, [])
            if not amenities:
                continue
            cluster = plugins.MarkerCluster(
                name=config['name'],
                show=amenity_type in DEFAULT_VISIBLE_CATEGORIES,
                icon_create_function=_cluster_icon_js(config['cluster_color']),
                show_coverage_on_hover=False,
                disable_clustering_at_zoom=CLUSTER_OFF_AT_ZOOM,
            )
            for amenity in amenities:
                # Every interpolated value is OSM-derived (publicly editable) or
                # client-supplied, so escape it all.
                amenity_name = escape(amenity.get('name', 'Unknown'))
                popup_html = f"""
                <div style="font-family: Arial; min-width: 150px;">
                    <h4 style="margin-bottom: 10px; color: {config['color']};">
                        {amenity_name}
                    </h4>
                    <b>Distance:</b> {escape(amenity.get('distance_m', 'N/A'))}m<br>
                """
                if 'cuisine' in amenity:
                    popup_html += f"<b>Cuisine:</b> {escape(amenity['cuisine'])}<br>"
                if 'opening_hours' in amenity:
                    popup_html += f"<b>Hours:</b> {escape(amenity['opening_hours'])}<br>"
                address = amenity.get('address')
                if isinstance(address, str) and address and not address.startswith('('):
                    popup_html += f"<b>Address:</b> {escape(address)}<br>"
                popup_html += "</div>"

                folium.Marker(
                    location=[amenity['lat'], amenity.get('lon', amenity.get('lng'))],
                    popup=folium.Popup(popup_html, max_width=250),
                    tooltip=amenity_name,
                    icon=folium.Icon(color=config['color'], icon=config['icon'], prefix='fa'),
                ).add_to(cluster)
            cluster.add_to(m)

        folium.LayerControl(position='topright', collapsed=False).add_to(m)

        hide_all_html = """
        <div style="position: fixed; bottom: 10px; right: 10px; background: white;
                    border: 2px solid #3498db; padding: 10px 15px; border-radius: 5px;
                    z-index: 999; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
            <button id="hideAllBtn" style="padding: 8px 12px; background: #e74c3c;
                    color: white; border: none; border-radius: 3px; cursor: pointer;
                    font-weight: bold;">
                Hide All
            </button>
            <button id="showAllBtn" style="padding: 8px 12px; background: #27ae60;
                    color: white; border: none; border-radius: 3px; cursor: pointer;
                    font-weight: bold; margin-left: 5px;">
                Show All
            </button>
        </div>
        """
        m.get_root().html.add_child(folium.Element(hide_all_html))

        js_code = """
        <script>
            document.getElementById('hideAllBtn').addEventListener('click', function() {
                var layers = document.querySelectorAll('input[type="checkbox"]');
                layers.forEach(function(checkbox) {
                    if (checkbox.checked) checkbox.click();
                });
            });
            document.getElementById('showAllBtn').addEventListener('click', function() {
                var layers = document.querySelectorAll('input[type="checkbox"]');
                layers.forEach(function(checkbox) {
                    if (!checkbox.checked) checkbox.click();
                });
            });
        </script>
        """
        m.get_root().html.add_child(folium.Element(js_code))

        plugins.Fullscreen(
            position='topleft',
            title='Fullscreen',
            title_cancel='Exit',
            force_separate_button=True
        ).add_to(m)
        return m

    def create_map_for_property(self,
                                property_data: dict,
                                amenities_data: Dict[str, List[dict]],
                                output_path: str,
                                amenities_unavailable: bool = False,
                                lang: str = 'en') -> bool:
        """
        Create an interactive map for a single property with amenity layers and save
        it as a standalone HTML document.

        Args:
            property_data: Dictionary containing property information
            amenities_data: Dictionary mapping amenity types to lists of amenity data
            output_path: Path where the HTML map will be saved
            amenities_unavailable: show the "amenity data unavailable" banner
            lang: 'en' or 'zh' for the map's notices

        Returns:
            True if successful, False otherwise
        """
        geo_location = property_data.get('geo_location') or property_data.get('coordinates')
        coords = self.parse_geo_location(geo_location)
        if not coords:
            print(f"  ✗ No valid coordinates for property")
            return False

        m = self._build_map(coords, property_data, amenities_data, amenities_unavailable, lang)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        m.save(output_path)
        print("  ✓ Map saved")
        return True

    def generate_map_html(self,
                         property_data: dict,
                         amenities_data: Dict[str, List[dict]],
                         amenities_unavailable: bool = False,
                         lang: str = 'en') -> str:
        """
        Generate map HTML as a string (for embedding or direct serving).

        Args:
            property_data: Dictionary containing property information
            amenities_data: Dictionary mapping amenity types to lists of amenity data
            amenities_unavailable: When True, the POI provider errored (all mirrors
                down) rather than genuinely returning zero amenities; a visible
                banner is shown so the user is never silently given a degraded map.
            lang: the UI language ('en' or 'zh') for the map's notices.

        Returns:
            HTML string of the generated map
        """
        geo_location = property_data.get('geo_location') or property_data.get('coordinates')
        coords = self.parse_geo_location(geo_location)
        if not coords:
            return "<html><body><h1>Error: Invalid coordinates</h1></body></html>"

        m = self._build_map(coords, property_data, amenities_data, amenities_unavailable, lang)
        return m._repr_html_()
