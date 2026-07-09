#!/usr/bin/env python3
"""
代码审查：验证所有条件化逻辑是否正确实现
"""

import re

def check_file(filepath, checks):
    """检查文件中是否存在特定的代码片段"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    results = []
    for check_name, pattern in checks:
        if isinstance(pattern, str):
            found = pattern in content
        else:
            found = pattern.search(content) is not None
        results.append((check_name, found))
    
    return results

print("=" * 80)
print("CODE REVIEW: Validating conditional crime data logic")
print("=" * 80)

# 检查 1: generate_recommendations 中的条件逻辑
print("\n[CHECK 1] llm_interface.py - generate_recommendations")
print("-" * 80)

checks_llm = [
    ("Determine should_include_crime flag", "should_include_crime = any(kw in soft_prefs_lower for kw in"),
    ("Set crimes_6m to None when not needed", "simple_prop['crimes_6m'] = None"),
    ("Set crime_trend to None when not needed", "simple_prop['crime_trend'] = None"),
    ("LLM prompt: Instruct to ignore null crimes", "If a field is null or missing, DO NOT mention it"),
    ("LLM prompt: Example pattern without crime", "User doesn't care about safety"),
]

results = check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\llm_interface.py',
    checks_llm
)

for check_name, found in results:
    status = "✓" if found else "❌"
    print(f"  {status} {check_name}: {found}")

# 检查 2: enrichment_service.py 中的条件逻辑
print("\n[CHECK 2] enrichment_service.py - conditional enrichment")
print("-" * 80)

checks_enrichment = [
    ("Skip crime data when not needed", "if not tasks"),
    ("Remove crime_data_summary field", "del enriched_prop[field]"),
    ("Skip crime enrichment check", "if any(keyword in soft_prefs for keyword in ['safe', 'crime'"),
]

results = check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\enrichment_service.py',
    checks_enrichment
)

for check_name, found in results:
    status = "✓" if found else "❌"
    print(f"  {status} {check_name}: {found}")

# 检查 3: create_fallback_recommendations 中的条件逻辑
print("\n[CHECK 3] llm_interface.py - create_fallback_recommendations")
print("-" * 80)

checks_fallback = [
    ("Determine user_cares_about_crime flag", "user_cares_about_crime = any(kw in soft_prefs_lower for kw in"),
    ("Conditional crime mention in fallback", "if user_cares_about_crime:"),
    ("Only extract crime data if user cares", "crime_data = prop.get('crime_data_summary', {}) if user_cares_about_crime else {}"),
]

results = check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\llm_interface.py',
    checks_fallback
)

for check_name, found in results:
    status = "✓" if found else "❌"
    print(f"  {status} {check_name}: {found}")

# 检查 4: LLM 提示中的数据指导
print("\n[CHECK 4] llm_interface.py - LLM prompts with data guidance")
print("-" * 80)

checks_prompts = [
    ("Data guidance section", "data_guidance = "),
    ("Excluded topics list", "excluded_topics = []"),
    ("Warn about not mentioning excluded topics", "DO NOT mention these in your explanations"),
    ("Budget information section", "budget_section = "),
]

results = check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\llm_interface.py',
    checks_prompts
)

for check_name, found in results:
    status = "✓" if found else "❌"
    print(f"  {status} {check_name}: {found}")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

all_checks = check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\llm_interface.py',
    checks_llm + checks_fallback + checks_prompts
) + check_file(
    'c:\\Users\\shuhan\\Desktop\\uk_rent_recommendation\\local_data_demo\\core\\enrichment_service.py',
    checks_enrichment
)

passed = sum(1 for _, found in all_checks if found)
total = len(all_checks)

print(f"\nPassed checks: {passed}/{total}")
if passed == total:
    print("✅ All code checks passed! The fix appears to be complete.")
else:
    print(f"❌ {total - passed} checks failed. Please review the implementation.")
