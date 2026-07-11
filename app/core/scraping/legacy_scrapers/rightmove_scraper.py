# rightmove_scraper.py

import requests
import csv
import json
import time
import random
import re
from bs4 import BeautifulSoup

# 在函数签名中添加 limit 参数
def scrape_rightmove_api(session, location_identifier, radius, min_price, max_price, min_bedrooms, max_bedrooms, limit=None):
    all_properties = []
    page_index = 0
    while True:
        if limit is not None and len(all_properties) >= limit:
            print(f"    - Scraper limit of {limit} reached. Stopping API requests.")
            break

        api_url = "https://www.rightmove.co.uk/api/_search"
        params = {
            'locationIdentifier': location_identifier, 'minBedrooms': min_bedrooms,
            'maxBedrooms': max_bedrooms, 'minPrice': min_price, 'maxPrice': max_price,
            'radius': radius, 'channel': 'RENT', 'index': page_index, 'viewType': 'LIST',
            'sortType': '6', 'numberOfPropertiesPerPage': 24,
        }
        try:
            print(f"    - Requesting API page {page_index // 24 + 1}...")
            response = session.get(api_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            properties_on_page = data.get('properties', [])
            if not properties_on_page:
                break
            for prop in properties_on_page:
                if limit is not None and len(all_properties) >= limit:
                    break
                if 'house share' in prop.get('propertyTypeFullDescription', '').lower() or 'retirement' in prop.get('propertyTypeFullDescription', '').lower():
                    continue
                
                # --- NEW: Extract images ---
                images = []
                property_images = prop.get('propertyImages', {})
                if property_images:
                    # Get main image
                    main_image = property_images.get('mainImageSrc', '')
                    if main_image:
                        images.append(main_image)
                    
                    # Get additional images (usually up to 5-10)
                    image_list = property_images.get('images', [])
                    for img in image_list[:10]:  # Limit to 10 images
                        img_url = img.get('srcUrl', '')
                        if img_url and img_url not in images:
                            images.append(img_url)
                
                all_properties.append({
                    'Price': prop.get('price', {}).get('displayPrices', [{}])[0].get('displayPrice', 'N/A'),
                    'Address': prop.get('displayAddress', 'N/A').replace('\n', ' '),
                    'Description': prop.get('propertyTypeFullDescription', 'N/A'),
                    'URL': 'https://www.rightmove.co.uk' + prop.get('propertyUrl', ''),
                    'Available From': 'To Be Checked',
                    'Images': images  # NEW: Image URLs array
                })
            
            if limit is not None and len(all_properties) >= limit:
                break

            page_index += 24
            time.sleep(random.uniform(0.5, 1.5))
        except requests.exceptions.RequestException as e:
            print(f"    - API request error: {e}")
            break
        except json.JSONDecodeError:
            print("    - API response JSON decode error.")
            break
    
    return all_properties

def enrich_properties_with_movein_date(session, properties_list):
    if not properties_list:
        return []
    enriched_properties = []
    total = len(properties_list)
    print(f"    - 开始为 {total} 个房源提取入住日期...")
    for i, prop in enumerate(properties_list):
        print(f"    - ({i+1}/{total}) 获取: {prop['URL']}", end='')
        move_in_date = "未找到"
        try:
            response = session.get(prop['URL'], timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            date_element = soup.find('dt', string=re.compile(r"Let available date", re.I))
            if date_element and date_element.find_next_sibling('dd'):
                move_in_date = date_element.find_next_sibling('dd').get_text(strip=True)
            prop['Available From'] = move_in_date
            print(f" -> {move_in_date}")
        except Exception as e:
            print(f" -> 查询失败: {e}")
            prop['Available From'] = '查询失败'
        enriched_properties.append(prop)
        time.sleep(random.uniform(2.0, 5.0))
    return enriched_properties

def save_to_csv(properties, filename, mode='w', include_header=True):
    if not properties: return
    headers = properties[0].keys()
    try:
        with open(filename, mode, newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            if include_header: writer.writeheader()
            writer.writerows(properties)
    except IOError as e:
        print(f"写入CSV文件 '{filename}' 时出错: {e}")

# 在函数签名中添加 limit 参数
def find_properties(location_identifier, radius, min_price, max_price, min_bedrooms=0, max_bedrooms=1, limit=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }
    session = requests.Session()
    session.headers.update(headers)
    
    # 步骤 1: 调用API获取基础信息，并传入 limit
    scraped_properties = scrape_rightmove_api(session, location_identifier, radius, min_price, max_price, min_bedrooms, max_bedrooms, limit=limit)
    if not scraped_properties:
        return []
    
    # 步骤 2: 访问每个URL获取入住日期 (处理的已经是限制后的列表)
    final_properties = enrich_properties_with_movein_date(session, scraped_properties)
    
    return final_properties

if __name__ == '__main__':
    print("--- 正在以独立模式运行 rightmove_scraper.py 示例 ---")
    found_properties = find_properties(
        location_identifier='STATION^8414', radius=0.5,
        min_price=1800, max_price=2500,
        limit=5 # 示例：直接运行时也只抓取5个
    )
    if found_properties:
        print(f"\n查找完成，共找到 {len(found_properties)} 个房源。")
        save_to_csv(found_properties, 'single_search_results.csv')
        print(f"结果已保存到 single_search_results.csv")
    else:
        print("\n在此次示例查找中未找到任何房源。")
    print("--- 示例运行结束 ---")