import csv
from datetime import datetime
from dateutil.parser import parse, ParserError

def load_properties_from_csv(filename='rightmove_listings.csv'):
    """从CSV文件中加载房源列表。"""
    try:
        with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            properties = [row for row in reader]
            print(f"成功从 {filename} 加载了 {len(properties)} 个房源。")
            return properties
    except FileNotFoundError:
        print(f"错误: 未找到文件 '{filename}'。请确保它与此脚本在同一目录下。")
        return []
    except Exception as e:
        print(f"读取CSV文件时发生错误: {e}")
        return []

def filter_properties_by_date(properties, target_date_str):
    """根据入住日期过滤房源。"""
    if not properties:
        return []

    target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
    print(f"筛选目标：保留入住日期在 {target_date.strftime('%Y年%m月%d日')} 或之后的所有房源。")
    
    filtered_properties = []
    
    for prop in properties:
        date_str = prop.get('Available From', '').strip()

        if not date_str:
            print(f"警告: 房源 '{prop['Address']}' 无入住日期，将予以保留。")
            filtered_properties.append(prop)
            continue

        # --- 这里是修改的部分 ---
        # 处理 'Now', 'Ask agent' 等非具体日期的特殊情况
        non_specific_dates = ['now', '现在', 'immediate', 'ask agent']
        if any(term in date_str.lower() for term in non_specific_dates):
            print(f"过滤掉: '{prop['Address']}' (原因: 入住日期为 '{date_str}')")
            continue
        # --- 修改结束 ---

        try:
            available_date = parse(date_str, dayfirst=True, fuzzy=False)
            
            # 如果解析出的日期没有年份，默认使用当前年份 (2025)
            if available_date.year == 1900: 
                 available_date = available_date.replace(year=datetime.now().year)

            if available_date >= target_date:
                print(f"保留: '{prop['Address']}' (入住日期: {available_date.strftime('%Y-%m-%d')})")
                filtered_properties.append(prop)
            else:
                print(f"过滤掉: '{prop['Address']}' (入住日期: {available_date.strftime('%Y-%m-%d')})")

        except ParserError:
            print(f"警告: 无法解析日期 '{date_str}' (房源: '{prop['Address']}'), 将予以保留以供手动检查。")
            filtered_properties.append(prop)
            
    return filtered_properties

def save_to_csv(properties, filename='filtered_rightmove_listings.csv'):
    """将过滤后的房源列表保存到新的CSV文件。"""
    if not properties:
        print("没有可保存的已过滤房源。")
        return
        
    # 确保所有字典的键都包含在表头中
    headers = set()
    for p in properties:
        headers.update(p.keys())
    
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=sorted(list(headers)))
            writer.writeheader()
            writer.writerows(properties)
        print(f"\n成功将 {len(properties)} 个过滤后的房源保存到 {filename}")
    except IOError as e:
        print(f"写入新的CSV文件时出错: {e}")

if __name__ == '__main__':
    # --- 参数定义 ---
    INPUT_FILENAME = 'combined_search_results.csv'
    OUTPUT_FILENAME = 'filtered_rightmove_listings.csv'
    TARGET_DATE = '2025-08-20' 
    
    # --- 执行程序 ---
    all_properties = load_properties_from_csv(INPUT_FILENAME)
    
    if all_properties:
        final_list = filter_properties_by_date(all_properties, TARGET_DATE)
        save_to_csv(final_list, OUTPUT_FILENAME)

    print("\n程序执行完毕。")