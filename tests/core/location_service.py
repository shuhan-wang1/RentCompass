# location_service.py

import requests
import json
from urllib.parse import urlparse, parse_qs
from .cache_service import get_from_cache, set_to_cache, create_cache_key

def get_rightmove_location_identifier(search_term: str) -> str | None:
    """
    FINAL, MOST ROBUST VERSION: This function simulates a user search on the
    Rightmove website and extracts the locationIdentifier from the final,
    redirected URL.

    This is the most reliable method as it mimics the core user workflow and
    does not depend on any hidden or unauthorized APIs.

    Args:
        search_term: The location name to search for (e.g., "University of Manchester").

    Returns:
        The full locationIdentifier string (e.g., "REGION^874") or None.
    """
    if not search_term:
        return None

    cache_key = create_cache_key('get_rightmove_location_identifier_v4', search_term)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Location ID for '{search_term}': {cached_result}")
        return cached_result

    print(f"  -> [Web Simulate] Simulating user search to find ID for: '{search_term}'")
    
    # This is the URL used when a user performs a search on the main site.
    # We are intentionally not URL-encoding the search term here, as `requests` will handle it.
    base_search_url = "https://www.rightmove.co.uk/property-to-rent/find.html"
    
    params = {
        'searchType': 'RENT',
        'locationIdentifier': '', # Must be present but can be empty
        'insId': 1,
        'radius': 0.0,
        '_includeLetAgreed': 'on',
        'dontShow': '',
        'keywords': search_term # The crucial part: we put our search term here
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/5.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/5.37.36'
    }

    try:
        # We make the request and, importantly, `allow_redirects=True` (which is the default)
        # The `response.url` will hold the FINAL URL after all redirects.
        response = requests.get(base_search_url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        
        final_url = response.url
        print(f"    - Redirected to final URL: {final_url}")
        
        # Now, we parse this final URL to extract the 'locationIdentifier' query parameter
        parsed_url = urlparse(final_url)
        query_params = parse_qs(parsed_url.query)
        
        location_identifier = query_params.get('locationIdentifier', [None])[0]
        
        if location_identifier and location_identifier != "REGION^87490": # Ignore the default London-wide region
            print(f"    - Success! Extracted ID: {location_identifier}")
            set_to_cache(cache_key, location_identifier)
            return location_identifier
        else:
            print(f"    - Could not extract a specific locationIdentifier from the final URL.")
            return None

    except requests.exceptions.RequestException as e:
        print(f"    - ERROR: Web simulation request failed: {e}")
        return None