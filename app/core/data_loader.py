# data_loader.py (Local CSV Version)

import pandas as pd
import re
import ast # Used to safely parse the string representation of the image list
import os

from uk_rent_agent.config import Config
from uk_rent_agent.data.parsing import extract_postcode, filter_by_budget, parse_price
from uk_rent_agent.data.repository import PropertyRepository

_repository = PropertyRepository(Config.from_env())

# --- This is the new function to load data from your fake CSV ---
def load_mock_properties_from_csv(filename: str = None) -> list[dict]:
    """
    Loads property listings from a local CSV file for testing and demo purposes.
    If filename is not provided, will look in the data/ directory.
    """
    # If no filename provided, use default path
    if filename is None:
        return PropertyRepository._read(_repository, _repository.fake_path)
    
    try:
        df = pd.read_csv(filename)
        # Convert the string representation of a list into an actual list
        df['Images'] = df['Images'].apply(lambda x: ast.literal_eval(x) if isinstance(x, str) and x.startswith('[') else [])
        properties = df.to_dict('records')
        print(f"--- Loaded {len(properties)} properties from local file: {filename} ---")
        return properties
    except FileNotFoundError:
        print(f"/!\\ ERROR: Mock data file not found at '{filename}'. Please create it. /!\\")
        return []
    except Exception as e:
        print(f"/!\\ ERROR: Failed to read mock data file: {e} /!\\")
        return []

def load_properties(force_refresh: bool = False) -> list[dict]:
    """Smart property loader — the entry point the app should use.

    Selected via the PROPERTY_SOURCE env var:
      - 'csv'     : always the bundled fake CSV (old demo behaviour).
      - 'scraper' : real scraped data, honouring the hybrid TTL cache; scrapes
                    on startup when the cache is missing/stale (can be slow).
      - 'auto'    : (default) serve the scraped cache if it has been built,
                    otherwise the fake CSV. Never blocks startup on a live scrape
                    unless SCRAPE_ON_STARTUP is truthy. Build/refresh the cache
                    out-of-band with:  python scripts/build_scraped_dataset.py

    Always falls back to the fake CSV if the scraping layer is unavailable, so
    the app can never start with zero properties.
    """
    global _repository
    current = Config.from_env()
    if current != _repository._config:
        _repository = PropertyRepository(current)
    return _repository.load(force_refresh=force_refresh).properties


def get_property_source() -> str:
    """Return the source label for the same repository snapshot used by search."""
    return _repository.load().source

# --- This function is now modified to call the local loader instead of the scraper ---
def get_live_properties(location_id: str, radius: float, min_price: int, max_price: int, limit: int | None = None) -> list[dict]:
    """
    MODIFIED: This function no longer scrapes live data.
    It loads properties from a local CSV file, making it legit and clean for demos.
    The function signature is kept the same to ensure compatibility with the rest of the app.
    """
    print("\n--- In Demo Mode: Loading properties from local CSV ---")

    # Call the new function to get data from the CSV
    all_properties = load_mock_properties_from_csv()

    if not all_properties:
        return []

    # Process properties (this part remains the same)
    processed_properties = []
    for prop in all_properties:
        prop['parsed_price'] = parse_price(prop.get('Price'))
        prop['postcode'] = extract_postcode(prop.get('Address'))
        if prop['parsed_price'] is not None:
             processed_properties.append(prop)

    # Apply the limit if one was provided
    if limit:
        return processed_properties[:limit]

    return processed_properties
