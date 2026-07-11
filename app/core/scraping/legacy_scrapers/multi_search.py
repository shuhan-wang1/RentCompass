# multi_search.py

from rightmove_scraper import find_properties as find_on_rightmove
from scrape_zoopla_listings import find_properties_zoopla as find_on_zoopla
from rightmove_scraper import save_to_csv # 我们可以复用这个保存函数
import time
import os

# ==============================================================================
# --- 在这里定义您的所有搜索任务 ---
# ==============================================================================
# 更新后的搜索任务列表  
# ==============================================================================
# --- 在这里定义您的所有搜索任务 ---
# --- 请用这个更新后的、包含正确键名的版本替换您文件中的旧列表 ---
# ==============================================================================
SEARCH_TASKS = [
    {
        'search_name': '罗素广场附近',
        'rightmove_id': 'STATION^7877',      # 正确的 Russell Square 代码
        'zoopla_slug': 'station/tube/russell-square',
        'radius': 3,    
    },
    {
        'search_name': 'Watford Junction周边',
        'rightmove_id': 'STATION^9686',     # 正确的 Watford Junction 代码
        'zoopla_slug': 'station/rail/watford-junction',
        'radius': 2,
    },
    {
        'search_name': 'Wembley Park周边',
        'rightmove_id': 'STATION^9782',      # 正确的 Wembley Park 代码
        'zoopla_slug': 'wembley-park',
        'radius': 2,
    }
]

# --- 定义通用的价格范围 ---
MIN_PRICE = 700
MAX_PRICE = 1100

# --- 定义统一的输出文件名 ---
OUTPUT_FILENAME = 'combined_search_results.csv'
# ==============================================================================


if __name__ == '__main__':
    all_found_properties = []
    total_tasks = len(SEARCH_TASKS)

    print(f"--- 开始执行 {total_tasks} 个跨平台搜索任务 ---")

    # 在开始前删除旧的输出文件
    if os.path.exists(OUTPUT_FILENAME):
        os.remove(OUTPUT_FILENAME)

    for i, task in enumerate(SEARCH_TASKS):
        print(f"\n{'='*50}\n>>> 任务 {i+1}/{total_tasks}: 正在搜索 '{task['search_name']}'\n{'='*50}")

        # --- 在 Rightmove 上搜索 ---
        print(f"\n--- 正在 Rightmove 上搜索... ---")
        print(f"    参数: location={task['rightmove_id']}, radius={task['radius']} miles, price=£{MIN_PRICE}-£{MAX_PRICE}")
        rm_properties = find_on_rightmove(
            location_identifier=task['rightmove_id'],
            radius=task['radius'],
            min_price=MIN_PRICE, max_price=MAX_PRICE,
            min_bedrooms=0, max_bedrooms=1
        )
        if rm_properties:
            for prop in rm_properties:
                prop['Platform'] = 'Rightmove'
            all_found_properties.extend(rm_properties)
            print(f"--- Rightmove 搜索完成，找到 {len(rm_properties)} 个房源。 ---")
        else:
            print(f"--- Rightmove 搜索完成，未找到任何房源。 ---")
        
        print("\n--- 任务内平台切换，等待5秒 ---")
        time.sleep(5)

        # --- 在 Zoopla 上搜索 ---
        print(f"\n--- 正在 Zoopla 上搜索... ---")
        print(f"    参数: location={task['zoopla_slug']}, radius={task['radius']} miles, price=£{MIN_PRICE}-£{MAX_PRICE}")
        zp_properties = find_on_zoopla(
            location_slug=task['zoopla_slug'],
            radius=task['radius'],
            min_price=MIN_PRICE, max_price=MAX_PRICE,
            min_bedrooms=0, max_bedrooms=1
        )
        if zp_properties:
            for prop in zp_properties:
                prop['Platform'] = 'Zoopla'
            all_found_properties.extend(zp_properties)
            print(f"--- Zoopla 搜索完成，找到 {len(zp_properties)} 个房源。 ---")
        else:
            print(f"--- Zoopla 搜索完成，未找到任何房源。 ---")

        # 在两个大任务之间增加一个更长的延时
        if i < total_tasks - 1:
            print(f"\n{'='*50}\n--- 大任务间隔，等待15秒 ---\n{'='*50}")
            time.sleep(15)

    print(f"\n{'='*50}\n--- 所有搜索任务已完成 ---\n{'='*50}")

    if all_found_properties:
        print(f"总计找到 {len(all_found_properties)} 个房源。")
        
        # 将所有结果一次性保存到CSV文件
        save_to_csv(all_found_properties, OUTPUT_FILENAME)
        print(f"所有结果已汇总保存到: {OUTPUT_FILENAME}")
    else:
        print("所有任务均未找到任何房源。")