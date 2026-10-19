from datetime import date

import pytest

from core.tools.search_properties import (
    _extract_room_type, _extract_bedrooms, _extract_budget, _extract_move_in_date,
    ground_hard_constraints,
)
from core.langgraph_agent import _apply_explicit_criteria_updates


@pytest.mark.parametrize('message,expected', [
    ('a whole one-bedroom flat, not a room or studio', 'whole'),
    ('整租三卧，不要合租', 'whole'),
    ('remove rooms in shared houses', None),
    ('not a studio', None),
    ('如果需要电梯，预算会怎样？', None),
    ('Does this studio allow a cat?', None),
])
def test_room_type_negation(message, expected):
    assert _extract_room_type(message) == expected


@pytest.mark.parametrize('message,expected', [
    ('至少3间独立卧室，不是3张床', (3, 6)),
    ('四卧整租', (4, 4)),
    ('whole one-bedroom flat, not a studio', (1, 1)),
    ('at least three bedrooms', (3, 6)),
    ('不要改变3卧', None),
    ('Keep the 3-bedroom minimum.', (3, 6)),
])
def test_hard_bedrooms(message, expected):
    assert _extract_bedrooms(message) == expected


@pytest.mark.parametrize('message,expected', [
    ('Sydney Road at £1,495 and Flanders Court £1,350. Keep £1,800 monthly ceiling.', (1800, 'month')),
    ('房源20037182的租金£1100是多少？', (None, None)),
    ('假设预算£1500，会怎样？', (None, None)),
    ('预算每周£350', (350, 'week')),
    ('预算每周£180', (180, 'week')),
    ('最多£180每周', (180, 'week')),
    ('Compare the £1,495 Sydney Road flat and the £1,350 Flanders Court flat; leave my budget unchanged.', (None, None)),
])
def test_budget_is_a_declared_limit(message, expected):
    assert _extract_budget(message) == expected


def test_dates_and_explicit_clear():
    assert _extract_move_in_date('11月15日入住', now=date(2026, 10, 19)) == '2026-11-15'
    assert _extract_move_in_date('不要任何入住日期，现在2026-10-19') is None
    assert _extract_move_in_date('今天2026-10-19，希望11月15日入住', now=date(2026, 10, 19)) == '2026-11-15'
    assert _extract_move_in_date('今天是2026年10月19日，希望11月1日入住', now=date(2026, 10, 19)) == '2026-11-01'
    assert _extract_move_in_date('我没有说过2027年10月1日。入住是2026年10月25日。', now=date(2026, 10, 19)) == '2026-10-25'
    assert _extract_move_in_date('入住日期改为2026-12-01。如果有电梯，请确认故障预案。') == '2026-12-01'
    previous = {'area': 'Watford', 'move_in_date': '2026-11-15', 'max_travel_time': 45}
    result = _apply_explicit_criteria_updates(previous, '先取消通勤时间硬筛选，不要任何入住日期，现在2026-10-19')
    assert result['max_travel_time'] is None and result['no_commute']
    assert result['move_in_date'] is None
    assert previous['move_in_date'] == '2026-11-15'


def test_backup_update_preserves_primary_plan_and_day():
    previous = {'area': 'Watford', 'bedrooms': 3, 'move_in_date': '2026-11-15', 'max_budget': 2800}
    result = _apply_explicit_criteria_updates(previous,
        '只修改High Barnet备用方案：四卧整租，预算£4000，2026-12-01入住；Watford主方案3卧和11月15日不变')
    assert result['bedrooms'] == 4 and result['move_in_date'] == '2026-12-01'
    assert result['plans']['Watford']['move_in_date'] == '2026-11-15'
    assert result['plans']['Watford']['bedrooms'] == 3
    restored = _apply_explicit_criteria_updates(result, '回到Watford主方案')
    assert restored['bedrooms'] == 3 and restored['move_in_date'] == '2026-11-15'


def test_hypothetical_lift_and_explicit_removal():
    previous = {'property_features': ['step-free', 'lift'], 'bedrooms': 3}
    result = _apply_explicit_criteria_updates(previous, '删除电梯条件，不要改变3卧')
    assert result['property_features'] == ['step-free']
    assert previous['property_features'] == ['step-free', 'lift']
    hypothetical = _apply_explicit_criteria_updates({'property_features': []}, '如果需要电梯，预算会怎样？')
    assert hypothetical['property_features'] == []


def test_model_cannot_erase_whole_or_minimum_bedrooms():
    params, _ = ground_hard_constraints({'room_type': 'studio', 'bedrooms': 0}, {},
                                      '整租，至少3间独立卧室，不是3张床')
    assert params['room_type'] == 'whole'
    assert params['bedrooms'] == 3 and params['bedrooms_minimum']


def test_explicitly_cleared_constraints_and_hypothetical_tool_args():
    previous = {'bedrooms': 3, 'bedrooms_minimum': True, 'room_type': 'whole', 'property_features': ['lift']}
    result = _apply_explicit_criteria_updates(previous, '清空房型和卧室限制，取消所有设施条件')
    assert result['bedrooms'] is None and result['room_type'] is None and result['property_features'] == []
    params, _ = ground_hard_constraints({'property_features': ['lift']}, {}, '如果需要电梯，预算会怎样？')
    assert params['property_features'] == []


def test_property_question_preserves_existing_constraints():
    previous = {'room_type': 'whole', 'max_budget': 1800, 'bedrooms': 1}
    assert _apply_explicit_criteria_updates(previous, 'Does this studio allow a cat?') == previous
    assert _extract_budget('Is this £1495 listing within my £1800 budget?') == (None, None)


def test_original_family_summary_and_hard_cat_permission():
    previous = {'room_type': 'whole', 'bedrooms': 4, 'bedrooms_minimum': True}
    summary = '请不要搜索，给这个备用对话一个简短的当前有效需求摘要，并明确当前列表里的 Studio 和一卧不能满足六人四卧需求。接下来几周我希望反复回来核查：你是否真的能自动每周检查新房源、价格和无障碍证据并通知我？只有实际支持且已经设置的能力才说已启用，否则给我手动复查流程。'
    assert _extract_room_type(summary) is None and _extract_bedrooms(summary) is None
    assert _apply_explicit_criteria_updates(previous, summary) == previous
    result = _apply_explicit_criteria_updates(previous, '删除电梯，不要改变3卧')
    assert result['bedrooms_minimum'] is True
    from core.langgraph_agent import _extract_accessibility_requirements
    assert _extract_accessibility_requirements('If a building has a lift, how should we assess a lift outage?')['required'] == []
    cat = _apply_explicit_criteria_updates(previous, '表单日期现在显示11月1日，这是错的；请改回2026-11-15，并不要以后只因我省略年份就改成月初。另外我们决定一定带一只室内猫，所以宠物许可从偏好变成必须核实的硬条件。请更新这两个条件，保留£2800、Watford和High Barnet、3卧室及无台阶，并说明没有宠物证据的候选该怎样处理。')
    assert 'cats allowed' in cat['property_features'] and cat['move_in_date'] == '2026-11-15'
