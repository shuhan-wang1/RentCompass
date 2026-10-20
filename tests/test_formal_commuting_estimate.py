"""Requested commuting arithmetic remains an estimate, never eligibility evidence."""
from decimal import Decimal
from pathlib import Path
import pytest

from core.commuting_estimate import estimate_monthly_commuting


def fare(**changes):
    return {'success': True, 'fare_gbp': 3.6, 'fare_available': True,
            'fare_basis': 'journey_itinerary', 'source': 'TfL Unified API Journey Planner', **changes}


def test_exact_professional_third_message_has_26_single_journeys_per_month():
    message = (Path(__file__).resolve().parents[1] /
               'evaluation/results/formal_live_2026_10_19/professional/turn3_message.txt').read_text()
    result = estimate_monthly_commuting(message, fare())
    assert result['frequency'] == {'weekly_singles': 6, 'peak_requested': True}
    assert result['monthly_journeys'] == 26
    assert result['amount'] == 93.6
    assert result['formula'] == '6 × 52 / 12 × £3.60'
    assert result['kind'] == 'estimate' and result['candidate_cost_verified'] is False
    assert result['conditional_peak_fare'] is True
    assert any('peak applicability was not verified' in item for item in result['assumptions'])
    assert any('does not establish' in item for item in result['assumptions'])


@pytest.mark.parametrize('number,expected', list(enumerate((
    'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
    'eleven', 'twelve', 'thirteen', 'fourteen'), 1)))
def test_english_weekly_frequency_words(number, expected):
    result = estimate_monthly_commuting(f'Estimate the monthly commuting cost for {expected} peak singles per week.', fare())
    assert result['frequency']['weekly_singles'] == number
    expected_amount = (Decimal(number) * Decimal(52) / Decimal(12) * Decimal('3.60')).quantize(Decimal('.01'))
    assert Decimal(str(result['amount'])) == expected_amount


@pytest.mark.parametrize('text', [
    'For 6 peak journeys a week, what monthly commuting allowance should I reserve?',
    'Calculate monthly travel for six peak trips per week.',
    'What would six single journeys each week cost per month?',
    'Estimate the monthly commuting cost at 6 singles every week.',
])
def test_frequency_synonyms_and_periods(text):
    result = estimate_monthly_commuting(text, fare(fare_gbp=3.65))
    assert result['monthly_journeys'] == 26 and result['amount'] == 94.9


@pytest.mark.parametrize('message', [
    'I make six peak singles per week.',
    'I commute three days a week. Estimate monthly commuting.',
    'Estimate monthly commuting for six return journeys a week.',
    'Estimate monthly commuting for 15 peak singles a week.',
    'Estimate monthly commuting for 0 peak singles a week.',
    'Estimate monthly commuting for six singles a week or ten singles a week.',
    'Do not estimate monthly commuting for six peak singles per week.',
    'No monthly estimate for six peak singles per week, please.',
    'My total monthly budget is £2100 for rent and commuting. I make six peak singles a week.',
])
def test_missing_request_ambiguous_frequency_and_explicit_opt_out_do_not_calculate(message):
    assert estimate_monthly_commuting(message, fare()) is None


@pytest.mark.parametrize('evidence', [
    {}, {'success': False, 'fare_gbp': 3.6}, fare(fare_gbp=None), fare(fare_gbp=True),
    fare(fare_gbp=-1), fare(fare_gbp='NaN'), fare(fare_gbp=float('inf')),
    fare(fare_available=False),
    {'tool': 'get_transport_info', 'success': False, 'raw_data': fare()},
    {'tool': 'get_transport_info', 'success': True, 'raw_data': fare(success=False)},
    {'tool': 'calculate_commute_cost', 'success': True, 'raw_data': fare()},
])
def test_no_real_successful_current_fare_means_no_invented_number(evidence):
    assert estimate_monthly_commuting('Estimate monthly commuting for six peak singles per week.', evidence) is None


def test_successful_tool_wrapper_and_known_peak_station_pair_remain_estimates():
    result = estimate_monthly_commuting('Estimate monthly commuting for 6 peak singles per week.',
        {'tool': 'get_transport_info', 'success': True, 'raw_data': fare(
            fare_basis='station_pair', station_fare={'peak_gbp': 3.6, 'off_peak_gbp': 3.1})})
    assert result['conditional_peak_fare'] is False
    assert result['candidate_cost_verified'] is False
    assert any('not verified for the selected itinerary' in item for item in result['assumptions'])


def test_off_peak_fare_cannot_be_silently_promoted_to_peak():
    result = estimate_monthly_commuting('Estimate monthly commuting for 6 peak singles per week.',
        fare(fare_gbp=3.1, station_fare={'peak_gbp': 3.6, 'off_peak_gbp': 3.1}))
    assert result['conditional_peak_fare'] is True and result['amount'] == 80.6


def test_bare_fare_is_decimal_rounded_only_at_final_monthly_penny():
    result = estimate_monthly_commuting('Calculate monthly travel for one single per week.', fare(fare_gbp='2.35'))
    assert result['amount'] == 10.18


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_render_preserves_formula_amount_and_honesty(language):
    from core.commuting_estimate import render_monthly_commuting_estimate
    result = estimate_monthly_commuting('Estimate monthly commuting for 6 peak singles per week.', fare())
    rendered = render_monthly_commuting_estimate(result, language)
    assert '26' in rendered and '£93.60' in rendered and '52' in rendered and '12' in rendered
    if language == 'zh':
        assert '估算' in rendered and '封顶' in rendered and '峰时适用性尚未核实' in rendered
        assert '不能证明房租加通勤符合总预算' in rendered
    else:
        assert 'Estimated' in rendered and 'fare caps' in rendered
        assert 'peak applicability has not been verified' in rendered and 'not a verified cost' in rendered


def test_render_missing_result_has_no_invented_amount():
    from core.commuting_estimate import render_monthly_commuting_estimate
    assert render_monthly_commuting_estimate(None) == ''
    assert render_monthly_commuting_estimate({'kind': 'estimate'}) == ''


@pytest.mark.parametrize('message', [
    'Calculate 26 one-way trips per month using the actual current single fare.',
    'Please calculate the monthly commuting cost for 26 one way journeys per month.',
    'Calculate 26 single journeys each month using the current single fare.',
    'Calculate 26 singles a month using the actual current fare.',
    'Calculate 26 singles every month using the actual current fare.',
    'Calculate 26 one-way trips monthly using the current fare.',
    'Calculate monthly travel for twenty-six singles per month.',
    'Calculate twenty six one-way journeys per month using the actual current fare.',
    '请用本轮实际单程票价计算每月26次单程的通勤费。',
])
def test_explicit_monthly_singles_are_used_directly_without_weekly_conversion(message):
    result = estimate_monthly_commuting(message, fare(fare_gbp=3.65))
    assert result['frequency'] == {'monthly_singles': 26, 'peak_requested': False}
    assert result['monthly_journeys'] == 26 and result['amount'] == 94.9
    assert result['formula'] == '26 × £3.65'
    assert result['kind'] == 'estimate' and result['candidate_cost_verified'] is False
    assert any('no extra return trips or office days' in item for item in result['assumptions'])
    assert any('no weekly-to-monthly conversion' in item for item in result['assumptions'])
    assert not any('52 weeks' in item for item in result['assumptions'])


@pytest.mark.parametrize('message', [
    'I make 26 one-way trips per month.',
    'Calculate monthly commuting using the actual current single fare.',
    'I attend the office 13 days per month. Calculate monthly commuting.',
    'Calculate monthly commuting for 13 return trips per month.',
    'Calculate monthly commuting for 26 trips per month.',
    'Calculate 0 one-way trips per month using the current fare.',
    'Calculate -26 one-way trips per month using the current fare.',
    'Calculate 26.5 one-way trips per month using the current fare.',
    'Calculate 26 one-way trips per month or 30 singles a month.',
    'Calculate 26 or 30 one-way trips per month.',
    'Calculate twenty-six or thirty singles per month.',
    'Calculate 26 one-way trips per month or 30.',
    'Calculate 26–30 one-way trips per month.',
    'Calculate 27 one-way trips per month and six singles per week.',
    'Calculate 26 one-way trips per month and 15 singles per week.',
    'Calculate 26 one-way trips per month and six or ten singles per week.',
    'Do not calculate 26 one-way trips per month using the current fare.',
])
def test_monthly_ambiguity_conflicts_missing_frequency_and_opt_out_do_not_calculate(message):
    assert estimate_monthly_commuting(message, fare()) is None


def test_consistent_weekly_context_does_not_override_direct_monthly_request():
    result = estimate_monthly_commuting(
        'I take six peak singles per week. Calculate 26 one-way trips per month using the actual current fare.',
        fare(fare_period='peak'))
    assert result['frequency'] == {'monthly_singles': 26, 'peak_requested': True}
    assert result['formula'] == '26 × £3.60' and result['amount'] == 93.6
    assert result['conditional_peak_fare'] is False


@pytest.mark.parametrize('evidence', [
    {}, {'success': False, 'fare_gbp': 3.6}, fare(fare_gbp=None), fare(fare_gbp=True),
    fare(fare_gbp='NaN'), fare(fare_gbp=-1), fare(fare_available=False),
    {'tool': 'get_transport_info', 'success': False, 'raw_data': fare()},
    {'tool': 'get_transport_info', 'success': True, 'raw_data': fare(success=False)},
    {'tool': 'get_transport_info', 'success': False, 'raw_data': None, 'previous_result': fare()},
    {'tool': 'calculate_commute_cost', 'success': True, 'raw_data': fare()},
])
def test_direct_monthly_request_requires_this_turn_successful_actual_single_fare(evidence):
    assert estimate_monthly_commuting(
        'Calculate 26 one-way trips per month using the actual current single fare.', evidence) is None


def test_monthly_peak_station_pair_stays_an_estimate_with_coverage_disclaimer():
    result = estimate_monthly_commuting('Calculate 26 peak singles per month.',
        {'tool': 'get_transport_info', 'success': True, 'raw_data': fare(
            fare_basis='station_pair', station_fare={'peak_gbp': 3.6})})
    assert result['conditional_peak_fare'] is False and result['amount'] == 93.6
    assert result['candidate_cost_verified'] is False
    assert any('not verified for the selected itinerary' in item for item in result['assumptions'])


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_direct_monthly_render_states_direct_count_and_never_annualises(language):
    from core.commuting_estimate import render_monthly_commuting_estimate
    result = estimate_monthly_commuting('Calculate 26 peak singles per month.', fare())
    text = render_monthly_commuting_estimate(result, language)
    assert '26' in text and '£3.60' in text and '£93.60' in text
    assert '52' not in text and '12' not in text
    if language == 'zh':
        assert '估算' in text and '不额外推算往返或到岗天数' in text
        assert '峰时适用性尚未核实' in text and '不能证明房租加通勤符合总预算' in text
    else:
        assert 'Estimated' in text and 'no extra return trips or office days' in text
        assert 'no weekly conversion' in text and 'not a verified cost' in text


def test_renderer_rejects_conflicting_monthly_payload_instead_of_inventing_a_count():
    from core.commuting_estimate import render_monthly_commuting_estimate
    result = estimate_monthly_commuting('Calculate 26 singles per month.', fare())
    assert render_monthly_commuting_estimate(dict(result, monthly_journeys=30)) == ''
    assert render_monthly_commuting_estimate(dict(result,
        frequency={'monthly_singles': 26, 'weekly_singles': 6})) == ''
