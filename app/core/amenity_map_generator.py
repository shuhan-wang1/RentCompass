"""
Property Amenity Map Generator
Integrates OpenStreetMap visualization for property recommendations
"""

import folium
from folium import plugins
import time
from typing import Dict, List, Tuple, Optional
import math
from pathlib import Path

from .maps_service import overpass_request, OverpassError
from .cache_service import get_from_cache, set_to_cache, create_cache_key

# Amenities change slowly, so POI results are cached for a week keyed by rounded
# coordinates + radius. This keeps repeat map generations (and live tests) off
# the shared Overpass servers.
POI_CACHE_TTL_SECONDS = 7 * 24 * 3600


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
        
        # Define amenity categories and their OSM tags
        # Matches the original property_amenity_map.py configuration
        self.amenity_config = {
            'supermarkets': {
                'osm_type': 'supermarket',
                'tags': {'shop': 'supermarket'},
                'color': 'blue',
                'icon': 'shopping-cart',
                'name': 'Supermarkets'
            },
            'convenience_stores': {
                'osm_type': 'convenience',
                'tags': {'shop': 'convenience'},
                'color': 'green',
                'icon': 'shopping-basket',
                'name': 'Convenience Stores'
            },
            'restaurants_chinese': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'chinese',
                'color': 'darkred',
                'icon': 'cutlery',
                'name': 'Chinese Restaurants'
            },
            'restaurants_italian': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'italian',
                'color': 'red',
                'icon': 'cutlery',
                'name': 'Italian Restaurants'
            },
            'restaurants_indian': {
                'osm_type': 'restaurant',
                'tags': {'amenity': 'restaurant'},
                'cuisine_filter': 'indian',
                'color': 'orange',
                'icon': 'cutlery',
                'name': 'Indian Restaurants'
            },
            'cafes': {
                'osm_type': 'cafe',
                'tags': {'amenity': 'cafe'},
                'color': 'lightblue',
                'icon': 'coffee',
                'name': 'Cafés'
            },
            'parks': {
                'osm_type': 'park',
                'tags': {'leisure': 'park'},
                'color': 'darkgreen',
                'icon': 'tree',
                'name': 'Parks'
            },
            'pharmacies': {
                'osm_type': 'pharmacy',
                'tags': {'amenity': 'pharmacy'},
                'color': 'purple',
                'icon': 'plus-sign',
                'name': 'Pharmacies'
            },
            'banks': {
                'osm_type': 'bank',
                'tags': {'amenity': 'bank'},
                'color': 'darkblue',
                'icon': 'gbp',
                'name': 'Banks'
            },
            'metro_stations': {
                'osm_type': 'station',
                'tags': {'railway': 'station'},
                'color': 'gray',
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
            print(f"    ✗ Failed to parse coordinates: {geo_location_str} - {e}")
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
            print(f"    Error querying {config['name']}: {e}")
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
    def _degradation_banner_html() -> str:
        """Fixed-position warning banner shown when the amenity provider is down."""
        return """
        <div style="position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
                    background: #fff3cd; color: #856404; border: 2px solid #ffc107;
                    padding: 10px 18px; border-radius: 6px; z-index: 10000;
                    font-family: Arial, sans-serif; font-weight: bold; text-align: center;
                    max-width: 90%; box-shadow: 0 2px 6px rgba(0,0,0,0.25);">
            &#9888; Nearby amenity data is temporarily unavailable &mdash; showing property location only.
        </div>
        """

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
                print(f"  -> [Cache HIT] amenity POIs for {round(lat, 3)}, {round(lon, 3)}")
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
                f"Overpass returned 0 amenities across all {len(amenities)} "
                f"categories for ({lat:.5f}, {lon:.5f}); treating as a provider "
                f"failure and not caching."
            )
        print(f"  -> [Overpass] Batched fetch returned {total} amenities across "
              f"{len(amenities)} categories")
        set_to_cache(cache_key, {'fetched_at': time.time(), 'amenities': amenities})
        return amenities

    def create_map_for_property(self,
                                property_data: dict,
                                amenities_data: Dict[str, List[dict]],
                                output_path: str,
                                amenities_unavailable: bool = False) -> bool:
        """
        Create an interactive map for a single property with amenity layers.
        
        Args:
            property_data: Dictionary containing property information
            amenities_data: Dictionary mapping amenity types to lists of amenity data
            output_path: Path where the HTML map will be saved
            
        Returns:
            True if successful, False otherwise
        """
        # Get coordinates
        geo_location = property_data.get('geo_location') or property_data.get('coordinates')
        coords = self.parse_geo_location(geo_location)
        
        if not coords:
            print(f"  ✗ No valid coordinates for property")
            return False
        
        lat, lon = coords
        
        # Create base map centered on the property
        m = folium.Map(
            location=[lat, lon],
            zoom_start=15,
            tiles='OpenStreetMap'
        )
        
        # Add the property marker (always visible)
        property_popup = f"""
        <div style="font-family: Arial; min-width: 200px;">
            <h4 style="margin-bottom: 10px; color: #2c3e50;">🏠 Property</h4>
            <b>Price:</b> {property_data.get('Price', property_data.get('price', 'N/A'))}<br>
            <b>Address:</b> {property_data.get('Address', property_data.get('address', 'N/A'))}<br>
            <b>Travel Time:</b> {property_data.get('travel_time_minutes', property_data.get('travel_time', 'N/A'))} min<br>
        </div>
        """
        
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(property_popup, max_width=300),
            tooltip=f"Property: {property_data.get('Address', property_data.get('address', 'Unknown'))[:40]}...",
            icon=folium.Icon(color='red', icon='home', prefix='fa')
        ).add_to(m)
        
        # Add search radius circle
        folium.Circle(
            location=[lat, lon],
            radius=self.radius_m,
            color='red',
            fill=False,
            opacity=0.3,
            weight=2,
            tooltip=f'{self.radius_km}km search radius'
        ).add_to(m)

        if amenities_unavailable:
            m.get_root().html.add_child(folium.Element(self._degradation_banner_html()))

        # Add amenities for each category
        print(f"  Adding amenity layers to map...")
        
        for amenity_type, config in self.amenity_config.items():
            amenities = amenities_data.get(amenity_type, [])
            
            if amenities:
                # Create a feature group for this amenity type (for layer control)
                feature_group = folium.FeatureGroup(name=config['name'], show=True)
                
                for amenity in amenities:
                    # Build popup HTML
                    popup_html = f"""
                    <div style="font-family: Arial; min-width: 150px;">
                        <h4 style="margin-bottom: 10px; color: {config['color']};">
                            {amenity.get('name', 'Unknown')}
                        </h4>
                    """
                    
                    if 'distance_m' in amenity:
                        popup_html += f"<b>Distance:</b> {amenity.get('distance_m')}m<br>"
                    if 'cuisine' in amenity:
                        popup_html += f"<b>Cuisine:</b> {amenity['cuisine']}<br>"
                    if 'opening_hours' in amenity:
                        popup_html += f"<b>Hours:</b> {amenity['opening_hours']}<br>"
                    if 'address' in amenity and not amenity['address'].startswith('('):
                        popup_html += f"<b>Address:</b> {amenity['address']}<br>"
                    
                    popup_html += "</div>"
                    
                    folium.Marker(
                        location=[amenity['lat'], amenity.get('lon', amenity.get('lng'))],
                        popup=folium.Popup(popup_html, max_width=250),
                        tooltip=amenity.get('name', 'Unknown'),
                        icon=folium.Icon(
                            color=config['color'],
                            icon=config['icon'],
                            prefix='fa'
                        )
                    ).add_to(feature_group)
                
                feature_group.add_to(m)
                print(f"    ✓ Added {len(amenities)} {config['name']}")
        
        # Add layer control to toggle amenity types
        folium.LayerControl(
            position='topright',
            collapsed=False
        ).add_to(m)
        
        # Add custom HTML button to hide all layers
        hide_all_html = """
        <div style="position: fixed; bottom: 10px; right: 10px; background: white; 
                    border: 2px solid #3498db; padding: 10px 15px; border-radius: 5px; 
                    z-index: 999; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
            <button id="hideAllBtn" style="padding: 8px 12px; background: #e74c3c; 
                    color: white; border: none; border-radius: 3px; cursor: pointer; 
                    font-weight: bold;">
                Hide All Layers
            </button>
            <button id="showAllBtn" style="padding: 8px 12px; background: #27ae60; 
                    color: white; border: none; border-radius: 3px; cursor: pointer; 
                    font-weight: bold; margin-left: 5px;">
                Show All Layers
            </button>
        </div>
        """
        m.get_root().html.add_child(folium.Element(hide_all_html))
        
        # Add JavaScript to handle button clicks
        js_code = """
        <script>
            document.getElementById('hideAllBtn').addEventListener('click', function() {
                var layers = document.querySelectorAll('input[type="checkbox"]');
                layers.forEach(function(checkbox) {
                    if (checkbox.checked) {
                        checkbox.click();
                    }
                });
            });
            
            document.getElementById('showAllBtn').addEventListener('click', function() {
                var layers = document.querySelectorAll('input[type="checkbox"]');
                layers.forEach(function(checkbox) {
                    if (!checkbox.checked) {
                        checkbox.click();
                    }
                });
            });
        </script>
        """
        m.get_root().html.add_child(folium.Element(js_code))
        
        # Add fullscreen button
        plugins.Fullscreen(
            position='topleft',
            title='Fullscreen',
            title_cancel='Exit Fullscreen',
            force_separate_button=True
        ).add_to(m)
        
        # Save the map
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        m.save(output_path)
        print(f"  ✓ Map saved to: {output_path}")
        
        return True
    
    def generate_map_html(self,
                         property_data: dict,
                         amenities_data: Dict[str, List[dict]],
                         amenities_unavailable: bool = False) -> str:
        """
        Generate map HTML as a string (for embedding or direct serving).

        Args:
            property_data: Dictionary containing property information
            amenities_data: Dictionary mapping amenity types to lists of amenity data
            amenities_unavailable: When True, the POI provider errored (all mirrors
                down) rather than genuinely returning zero amenities; a visible
                banner is shown so the user is never silently given a degraded map.

        Returns:
            HTML string of the generated map
        """
        # Get coordinates
        geo_location = property_data.get('geo_location') or property_data.get('coordinates')
        coords = self.parse_geo_location(geo_location)
        
        if not coords:
            return "<html><body><h1>Error: Invalid coordinates</h1></body></html>"
        
        lat, lon = coords
        
        # Create base map
        m = folium.Map(
            location=[lat, lon],
            zoom_start=15,
            tiles='OpenStreetMap'
        )
        
        # Add the property marker
        property_popup = f"""
        <div style="font-family: Arial; min-width: 200px;">
            <h4 style="margin-bottom: 10px; color: #2c3e50;">🏠 Property</h4>
            <b>Price:</b> {property_data.get('Price', property_data.get('price', 'N/A'))}<br>
            <b>Address:</b> {property_data.get('Address', property_data.get('address', 'N/A'))}<br>
            <b>Travel Time:</b> {property_data.get('travel_time_minutes', property_data.get('travel_time', 'N/A'))} min<br>
        </div>
        """
        
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(property_popup, max_width=300),
            tooltip=f"Property",
            icon=folium.Icon(color='red', icon='home', prefix='fa')
        ).add_to(m)
        
        # Add search radius circle
        folium.Circle(
            location=[lat, lon],
            radius=self.radius_m,
            color='red',
            fill=False,
            opacity=0.3,
            weight=2,
            tooltip=f'{self.radius_km}km search radius'
        ).add_to(m)

        # Honest degradation: if the POI provider errored, tell the user plainly
        # instead of showing a marker-only map that looks like "nothing nearby".
        if amenities_unavailable:
            m.get_root().html.add_child(folium.Element(self._degradation_banner_html()))

        # Add amenities
        for amenity_type, config in self.amenity_config.items():
            amenities = amenities_data.get(amenity_type, [])
            
            if amenities:
                feature_group = folium.FeatureGroup(name=config['name'], show=True)
                
                for amenity in amenities:
                    popup_html = f"""
                    <div style="font-family: Arial; min-width: 150px;">
                        <h4 style="margin-bottom: 10px; color: {config['color']};">
                            {amenity.get('name', 'Unknown')}
                        </h4>
                        <b>Distance:</b> {amenity.get('distance_m', 'N/A')}m<br>
                    """
                    
                    if 'cuisine' in amenity:
                        popup_html += f"<b>Cuisine:</b> {amenity['cuisine']}<br>"
                    
                    popup_html += "</div>"
                    
                    folium.Marker(
                        location=[amenity['lat'], amenity.get('lon', amenity.get('lng'))],
                        popup=folium.Popup(popup_html, max_width=250),
                        tooltip=amenity.get('name', 'Unknown'),
                        icon=folium.Icon(
                            color=config['color'],
                            icon=config['icon'],
                            prefix='fa'
                        )
                    ).add_to(feature_group)
                
                feature_group.add_to(m)
        
        # Add layer control
        folium.LayerControl(position='topright', collapsed=False).add_to(m)
        
        # Add control buttons
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
        
        # Add fullscreen
        plugins.Fullscreen(
            position='topleft',
            title='Fullscreen',
            title_cancel='Exit',
            force_separate_button=True
        ).add_to(m)
        
        # Return HTML as string
        return m._repr_html_()
