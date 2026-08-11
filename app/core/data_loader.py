# data_loader.py (Local CSV Version)

import pandas as pd
import re
import ast # Used to safely parse the string representation of the image list
import os

from uk_rent_agent.config import Config
from uk_rent_agent.data.parsing import parse_price  # noqa: F401 (re-exported: app.py & search_properties import parse_price from here)
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

def _current_repository() -> PropertyRepository:
    global _repository
    current = Config.from_env()
    if current != _repository._config:
        _repository = PropertyRepository(current)
    return _repository


def get_property_snapshot(force_refresh: bool = False):
    """Return rows together with their source, age and observation timestamp."""
    return _current_repository().load(force_refresh=force_refresh)


def load_properties(force_refresh: bool = False) -> list[dict]:
    """Smart property loader — the entry point the app should use.

    Selected via the PROPERTY_SOURCE env var:
      - 'csv'     : always the bundled fake CSV (old demo behaviour).
      - 'scraper' : real scraped data, honouring the hybrid TTL cache; scrapes
                    on startup when the cache is missing/stale (can be slow).
      - 'auto'    : (default) serve a scraped snapshot if one exists, including
                    an explicitly-labelled stale snapshot; otherwise return no
                    rows. Build/refresh it out-of-band with:
                    python scripts/build_scraped_dataset.py

    Demo rows are never an implicit fallback. They require PROPERTY_SOURCE=csv.
    """
    return get_property_snapshot(force_refresh=force_refresh).properties


def get_property_source() -> str:
    """Return the source label for the same repository snapshot used by search."""
    return _current_repository().load().source
