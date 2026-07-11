"""
OSM 数据预爬取脚本
- 限制速率（每次请求间隔 1 秒）
- 只获取 500m 内的 POI 数据
- 保存到本地 JSON 文件
"""

import json
import time
import os
import sys
from pathlib import Path

# 添加父目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from typing import Dict, List, Optional
import pandas as pd
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# 配置
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
REQUEST_DELAY = 1.5  # 每次请求间隔（秒）
RADIUS_METERS = 500  # 搜索半径
OUTPUT_FILE = "data/osm_poi_cache.json"

# POI 类型定义
POI_CATEGORIES = {
    "supermarket": {
        "query": '["shop"="supermarket"]',
        "icon": "🛒"
    },
    "convenience": {
        "query": '["shop"="convenience"]',
        "icon": "🏪"
    },
    "restaurant": {
        "query": '["amenity"="restaurant"]',
        "icon": "🍽️"
    },
    "cafe": {
        "query": '["amenity"="cafe"]',
        "icon": "☕"
    },
    "pharmacy": {
        "query": '["amenity"="pharmacy"]',
        "icon": "💊"
    },
    "hospital": {
        "query": '["amenity"="hospital"]',
        "icon": "🏥"
    },
    "clinic": {
        "query": '["amenity"="clinic"]',
        "icon": "🩺"
    },
    "gym": {
        "query": '["leisure"="fitness_centre"]',
        "icon": "🏋️"
    },
    "park": {
        "query": '["leisure"="park"]',
        "icon": "🌳"
    },
    "bus_stop": {
        "query": '["highway"="bus_stop"]',
        "icon": "🚌"
    },
    "subway": {
        "query": '["station"="subway"]',
        "icon": "🚇"
    },
    "bank": {
        "query": '["amenity"="bank"]',
        "icon": "🏦"
    },
    "atm": {
        "query": '["amenity"="atm"]',
        "icon": "💳"
    },
    "post_office": {
        "query": '["amenity"="post_office"]',
        "icon": "📮"
    },
    "library": {
        "query": '["amenity"="library"]',
        "icon": "📚"
    }
}


class OSMDataFetcher:
    def __init__(self, output_file: str = OUTPUT_FILE):
        self.output_file = output_file
        self.cache: Dict = {}
        self.geolocator = Nominatim(user_agent="uk_rent_recommender_v1")
        self._load_cache()
    
    def _load_cache(self):
        """加载已有的缓存"""
        if os.path.exists(self.output_file):
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    self.cache = json.load(f)
                print(f"✅ 已加载 {len(self.cache)} 个地址的缓存数据")
            except Exception as e:
                print(f"⚠️ 加载缓存失败: {e}")
                self.cache = {}
    
    def _save_cache(self):
        """保存缓存到文件"""
        os.makedirs(os.path.dirname(self.output_file) or '.', exist_ok=True)
        with open(self.output_file, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)
        print(f"💾 已保存缓存到 {self.output_file}")
    
    def geocode_address(self, address: str) -> Optional[tuple]:
        """将地址转换为经纬度"""
        try:
            location = self.geolocator.geocode(address, timeout=10)
            if location:
                return (location.latitude, location.longitude)
        except GeocoderTimedOut:
            print(f"⏱️ 地理编码超时: {address}")
        except Exception as e:
            print(f"❌ 地理编码失败: {address} - {e}")
        return None
    
    def fetch_pois_for_location(self, lat: float, lon: float, category: str) -> List[Dict]:
        """获取指定位置周边的 POI"""
        query_filter = POI_CATEGORIES[category]["query"]
        
        query = f"""
        [out:json][timeout:25];
        (
            node{query_filter}(around:{RADIUS_METERS},{lat},{lon});
            way{query_filter}(around:{RADIUS_METERS},{lat},{lon});
        );
        out center body;
        """
        
        try:
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            if response.status_code == 200:
                data = response.json()
                pois = []
                for element in data.get("elements", []):
                    tags = element.get("tags", {})
                    poi = {
                        "name": tags.get("name", "Unnamed"),
                        "type": category,
                        "lat": element.get("lat") or element.get("center", {}).get("lat"),
                        "lon": element.get("lon") or element.get("center", {}).get("lon"),
                        "icon": POI_CATEGORIES[category]["icon"],
                        "details": {
                            "cuisine": tags.get("cuisine"),
                            "brand": tags.get("brand"),
                            "opening_hours": tags.get("opening_hours"),
                            "phone": tags.get("phone"),
                            "website": tags.get("website"),
                        }
                    }
                    # 清理 None 值
                    poi["details"] = {k: v for k, v in poi["details"].items() if v}
                    pois.append(poi)
                return pois
            else:
                print(f"❌ API 错误 {response.status_code}: {category}")
        except Exception as e:
            print(f"❌ 请求失败 ({category}): {e}")
        
        return []
    
    def fetch_all_pois_for_address(self, address: str, force_refresh: bool = False) -> Dict:
        """获取地址周边的所有 POI"""
        # 创建缓存键
        cache_key = address.lower().strip()
        
        # 检查缓存
        if not force_refresh and cache_key in self.cache:
            print(f"📋 使用缓存: {address[:50]}...")
            return self.cache[cache_key]
        
        # 地理编码
        coords = self.geocode_address(address)
        if not coords:
            print(f"❌ 无法获取坐标: {address}")
            return {"error": "geocoding_failed", "address": address}
        
        lat, lon = coords
        print(f"📍 坐标: {lat:.6f}, {lon:.6f}")
        
        # 获取各类 POI
        all_pois = {
            "address": address,
            "lat": lat,
            "lon": lon,
            "radius_m": RADIUS_METERS,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pois": {}
        }
        
        for category in POI_CATEGORIES:
            print(f"  🔍 获取 {category}...")
            pois = self.fetch_pois_for_location(lat, lon, category)
            all_pois["pois"][category] = pois
            print(f"     找到 {len(pois)} 个 {category}")
            time.sleep(REQUEST_DELAY)  # 限速
        
        # 保存到缓存
        self.cache[cache_key] = all_pois
        self._save_cache()
        
        return all_pois
    
    def fetch_for_property_list(self, csv_path: str, address_column: str = "Address"):
        """批量获取房源列表中所有地址的 POI 数据"""
        try:
            df = pd.read_csv(csv_path)
            addresses = df[address_column].dropna().unique().tolist()
            
            print(f"\n{'='*60}")
            print(f"📊 共 {len(addresses)} 个唯一地址")
            print(f"📋 已缓存 {len(self.cache)} 个地址")
            print(f"{'='*60}\n")
            
            # 过滤已缓存的地址
            to_fetch = [addr for addr in addresses if addr.lower().strip() not in self.cache]
            print(f"⏳ 需要获取 {len(to_fetch)} 个新地址\n")
            
            for i, address in enumerate(to_fetch):
                print(f"\n[{i+1}/{len(to_fetch)}] 处理: {address[:60]}...")
                self.fetch_all_pois_for_address(address)
                
                # 每 5 个地址保存一次
                if (i + 1) % 5 == 0:
                    self._save_cache()
                    print(f"💾 中间保存完成 ({i+1}/{len(to_fetch)})")
            
            # 最终保存
            self._save_cache()
            print(f"\n✅ 完成！共处理 {len(to_fetch)} 个新地址")
            
        except Exception as e:
            print(f"❌ 批量处理失败: {e}")
            self._save_cache()  # 保存已获取的数据


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="OSM POI 数据预爬取工具")
    parser.add_argument("--csv", type=str, help="房源 CSV 文件路径")
    parser.add_argument("--address", type=str, help="单个地址查询")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE, help="输出文件路径")
    
    args = parser.parse_args()
    
    fetcher = OSMDataFetcher(output_file=args.output)
    
    if args.address:
        # 单个地址查询
        print(f"\n🔍 查询地址: {args.address}")
        result = fetcher.fetch_all_pois_for_address(args.address)
        print(f"\n📊 结果:")
        for category, pois in result.get("pois", {}).items():
            if pois:
                print(f"  {POI_CATEGORIES[category]['icon']} {category}: {len(pois)} 个")
    
    elif args.csv:
        # 批量处理
        fetcher.fetch_for_property_list(args.csv)
    
    else:
        # 默认处理 data 目录下的房源文件
        default_csv = "data/fake_property_listings.csv"
        if os.path.exists(default_csv):
            fetcher.fetch_for_property_list(default_csv)
        else:
            print("用法:")
            print("  python prefetch_osm_data.py --csv data/properties.csv")
            print("  python prefetch_osm_data.py --address 'Vega, 6 Miles Street, Vauxhall, London SW8 1RZ'")


if __name__ == "__main__":
    main()
