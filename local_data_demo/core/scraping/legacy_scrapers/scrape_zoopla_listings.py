# scrape_zoopla_listings.py

import requests
import json
import time
import random
import re
from bs4 import BeautifulSoup

def _extract_date_from_detail_page(soup):
    """
    A dedicated function to find the availability date using a multi-layered and robust strategy.
    """
    date_text = None
    
    # Strategy 1: Find the "tags/pills" section (most common from user's example)
    # The date is in a list with a class like '_1wz55u80'
    tags_list = soup.find('ul', class_=re.compile(r'^_1wz55u80'))
    if tags_list:
        for tag in tags_list.find_all('li'):
            # --- 【关键修改】: 使用更灵活、不区分大小写的查找 ---
            tag_text = tag.get_text(strip=True)
            if "available" in tag_text.lower():
                # 使用正则表达式安全地移除前缀
                date_text = re.sub(r'Available from', '', tag_text, flags=re.I).strip()
                print(f" -> Found Date (Tag): {date_text}")
                return date_text
    # --- 【修改结束】 ---

    # Strategy 2: Parse the __NEXT_DATA__ JSON block (most reliable backup)
    data_script = soup.find('script', {'id': '__NEXT_DATA__'})
    if data_script:
        data = json.loads(data_script.string)
        letting_details = data.get('props',{}).get('pageProps',{}).get('details',{}).get('lettings')
        if letting_details and letting_details.get('lettingDate'):
            date_text = letting_details['lettingDate']
            print(f" -> Found Date (JSON): {date_text}")
            return date_text

    # Strategy 3: Find the "Key features" list
    features_heading = soup.find(['h2', 'h3'], string=re.compile(r"Key features", re.I))
    if features_heading:
        features_list = features_heading.find_next_sibling('ul')
        if features_list:
            for li in features_list.find_all('li'):
                li_text = li.get_text().lower()
                if "available" in li_text:
                    full_text = li.get_text(strip=True)
                    date_text = re.sub(r'available from', '', full_text, flags=re.I).strip()
                    date_text = re.sub(r'available', '', date_text, flags=re.I).strip()
                    print(f" -> Found Date (Features List): {date_text.capitalize()}")
                    return date_text.capitalize()

    # Strategy 4: Search the full description text
    description_div = soup.find('div', {'data-testid': 'listing-description-content'})
    if description_div:
        # 这个正则表达式会匹配 "available" 之后的几乎所有文本，直到遇到换行符或句子结尾
        match = re.search(r"available (from |on |after |now|immediately|[\w\s,./]+?)(?:\.|\n)", description_div.get_text(), re.I)
        if match:
            date_text = match.group(1).strip()
            print(f" -> Found Date (Description): {date_text}")
            return date_text

    return None

def find_properties_zoopla(location_slug, radius, min_price, max_price, min_bedrooms=0, max_bedrooms=1, exclude_retirement_homes=True, exclude_shared_accommodation=True):
    """
    【V8 - 真正健壮的日期提取】
    """
    session = requests.Session()
    flaresolverr_url = "http://localhost:8191/v1"
    headers = {'Content-Type': 'application/json'}
    session_id = f'zoopla_session_{random.randint(1000, 9999)}'

    print(f"    - Initializing direct communication with FlareSolverr (session: {session_id})...")

    try:
        init_payload = {'cmd': 'sessions.create', 'session': session_id}
        response = session.post(flaresolverr_url, headers=headers, json=init_payload, timeout=20)
        response.raise_for_status()
        if response.json().get('status') != 'ok':
            raise Exception("Failed to create a FlareSolverr session.")
    except requests.exceptions.RequestException as e:
        print(f"    - Connection to FlareSolverr failed. Error: {e}")
        return []

    try:
        # --- 阶段一 ---
        base_url = f"https://www.zoopla.co.uk/to-rent/property/{location_slug}/"
        params = {
            "beds_min": str(min_bedrooms), "beds_max": str(max_bedrooms),
            "price_frequency": "per_month", "price_min": str(min_price),
            "price_max": str(max_price), "radius": str(radius),
            "results_sort": "newest_listings", "search_source": "to-rent",
            "is_retirement_home": str(not exclude_retirement_homes).lower(),
            "is_shared_accommodation": str(not exclude_shared_accommodation).lower()
        }
        
        from requests.models import PreparedRequest
        p = PreparedRequest()
        p.prepare(url=base_url, params=params)
        target_url = p.url

        print(f"    - Sending request to FlareSolverr for Zoopla search page...")
        payload = {'cmd': 'request.get', 'url': target_url, 'session': session_id, 'maxTimeout': 60000}
        response = session.post(flaresolverr_url, headers=headers, json=payload, timeout=70)
        response.raise_for_status()
        
        flare_data = response.json()
        if flare_data.get('status') != 'ok':
            raise Exception(f"FlareSolverr returned an error: {flare_data.get('message', 'Unknown error')}")
            
        html = flare_data['solution']['response']
        soup = BeautifulSoup(html, 'html.parser')
        
        schema_script = soup.find('script', {'id': 'lsrp-schema'})
        if not schema_script:
            print("    - Zoopla: CRITICAL - Could not find 'lsrp-schema' script tag.")
            return []

        schema_data = json.loads(schema_script.string)
        graph_data = schema_data.get('@graph', [])
        item_list = []
        if len(graph_data) > 2:
            item_list = graph_data[2].get('mainEntity', {}).get('itemListElement', [])

        if not item_list:
            print("    - Zoopla: Property list ('itemListElement') is empty.")
            return []

        print(f"    - Success! Found {len(item_list)} property URLs to check.")
        
        properties_from_search = []
        for entry in item_list:
            item = entry.get('item', {})
            offers = item.get('offers', {})
            properties_from_search.append({
                'Price': f"£{offers.get('price', 'N/A')} pcm",
                'Address': item.get('name', 'N/A'),
                'Description': item.get('description', ''),
                'URL': item.get('url', ''),
                'Available From': 'To Be Checked'
            })
        
        # --- 阶段二 ---
        final_properties = []
        total = len(properties_from_search)
        if total > 0:
            print(f"\n    - Zoopla: Phase 2 - Enriching details for {total} properties...")
            for i, prop in enumerate(properties_from_search):
                if not prop.get('URL'):
                    final_properties.append(prop)
                    continue

                print(f"    - ({i+1}/{total}) Checking: {prop['URL']}", end='')
                
                try:
                    time.sleep(random.uniform(2.0, 4.0))
                    detail_payload = {'cmd': 'request.get', 'url': prop['URL'], 'session': session_id, 'maxTimeout': 40000}
                    detail_response = session.post(flaresolverr_url, headers=headers, json=detail_payload, timeout=50)
                    detail_flare_data = detail_response.json()
                    
                    if detail_flare_data['status'] != 'ok':
                        raise Exception(f"FlareSolverr error on detail page")
                    
                    detail_html = detail_flare_data['solution']['response']
                    detail_soup = BeautifulSoup(detail_html, 'lxml')
                    
                    found_date = _extract_date_from_detail_page(detail_soup)
                    
                    if found_date:
                        prop['Available From'] = found_date
                    else:
                        prop['Available From'] = 'Not Found on Page'
                        print(f" -> Date not found")

                except Exception as e_detail:
                    print(f" -> Detail page query failed: {e_detail}")
                    prop['Available From'] = 'Query Failed'
                
                final_properties.append(prop)
            
        return final_properties

    finally:
        print("    - Cleaning up FlareSolverr session...")
        destroy_payload = {'cmd': 'sessions.destroy', 'session': session_id}
        try:
            session.post(flaresolverr_url, headers=headers, json=destroy_payload, timeout=20)
            print(f"    - FlareSolverr session '{session_id}' destroyed.")
        except Exception as e_destroy:
            print(f"    - Failed to destroy FlareSolverr session: {e_destroy}")