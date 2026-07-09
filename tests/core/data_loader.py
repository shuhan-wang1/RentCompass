# data_loader.py (Local CSV Version)

import pandas as pd
import re
import ast # Used to safely parse the string representation of the image list
import os

# --- This is the new function to load data from your fake CSV ---
def load_mock_properties_from_csv(filename: str = None) -> list[dict]:
    """
    Loads property listings from a local CSV file for testing and demo purposes.
    If filename is not provided, will look in the data/ directory.
    """
    # If no filename provided, use default path
    if filename is None:
        # Get the directory of this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(os.path.dirname(current_dir), 'data', 'fake_property_listings.csv')
    
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
        return []
    except Exception as e:
        print(f"/!\\ ERROR: Failed to read mock data file: {e} /!\\")
        return []

def parse_price(price_str: str) -> float | None:
    if not isinstance(price_str, str): return None
    if 'poa' in price_str.lower(): return None
    try:
        price = re.sub(r'[£,pcm]', '', price_str).strip()
        return float(price)
    except (ValueError, TypeError):
        return None

def extract_postcode(address: str) -> str | None:
    if not isinstance(address, str): return None
    postcode_regex = r'([A-Z]{1,2}[0-9][A-Z0-9]?\s?[0-9][A-Z]{2})'
    match = re.search(postcode_regex, address, re.IGNORECASE)
    if match:
        postcode = match.group(1).upper().replace(" ", "")
        if len(postcode) > 3:
            return f"{postcode[:-3]} {postcode[-3:]}"
        return postcode
    return None

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

def filter_by_budget(properties: list[dict], max_price: float) -> list[dict]:
    return [p for p in properties if p.get('parsed_price', float('inf')) <= max_price]