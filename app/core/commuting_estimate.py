"""Pure, explicitly requested commuting arithmetic from this turn's fare evidence.

This result is an estimate for a declared trip frequency, not candidate-linked
transport evidence. It must not establish listing eligibility or affordability.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

_NUMBERS = {word: value for value, word in enumerate((
    'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
    'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen'), 1)}
_NUMBER = r'(?:[1-9]\d{0,5}|' + '|'.join(_NUMBERS) + ')'
_FREQUENCY = re.compile(
    rf'\b({_NUMBER})\s+(?P<peak>peak\s+)?(?:single(?:\s+(?:journeys?|trips?))?s?|journeys?|trips?)'
    r'\s+(?:a|per|each|every)\s+week\b', re.I)
_MONTHLY_NUMBERS = dict(_NUMBERS, fifteen=15, sixteen=16, seventeen=17,
                        eighteen=18, nineteen=19, twenty=20, thirty=30)
for _unit, _value in list(_NUMBERS.items())[:9]:
    for _separator in ('-', ' '):
        _MONTHLY_NUMBERS[f'twenty{_separator}{_unit}'] = 20 + _value
_MONTHLY_NUMBERS.update({'thirty-one': 31, 'thirty one': 31})
_MONTHLY_NUMBER = (r'(?:[1-9]\d{0,5}|' + '|'.join(
    re.escape(word) for word in sorted(_MONTHLY_NUMBERS, key=len, reverse=True)) + ')')
_MONTHLY_FREQUENCY = re.compile(
    rf'(?<![\w.,+\-\u2010-\u2015\u2212])\b({_MONTHLY_NUMBER})\s+(?P<peak>peak\s+)?'
    r'(?:single(?:\s+(?:journeys?|trips?))?s?|one[-\u2010-\u2015 ]way\s+(?:journeys?|trips?))'
    r'\s+(?:(?:a|per|each|every)\s+month|monthly)\b', re.I)
_MONTHLY_FREQUENCY_ZH = re.compile(
    r'每月\s*([1-9]\d{0,5})\s*(?:次|个)?\s*(?P<peak>高峰(?:期)?\s*)?单程(?:出行|旅程|行程)?')


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _monthly_estimate_requested(message: str) -> bool:
    for clause in re.split(r'[.!?。！？;；\n]', message or ''):
        if re.search(r'(?:don.t|do not|never)\s+(?:estimate|calculate|reserve)|no monthly (?:estimate|calculation)|不要(?:估算|计算|预留)', clause, re.I):
            continue
        monthly = re.search(r'\bmonthly\b|\b(?:per|a|each|every)\s+month\b|每月|月均', clause, re.I)
        request = re.search(r'\b(?:estimate|calculate|work out|what|how much|reserve)\b|估算|计算|预留|多少', clause, re.I)
        commuting = re.search(r'commut|journeys?|trips?|singles?|fare|travel|交通|通勤|单程', clause, re.I)
        if monthly and request and commuting:
            return True
    return False


def estimate_monthly_commuting(message: str, transport_result: dict) -> dict | None:
    """Use an explicit monthly singles total, or average declared weekly singles.

    ``transport_result`` is the successful raw get_transport_info payload from the
    current turn, or its tool-result wrapper. The caller owns the current-turn
    boundary; old observations, model prose and stored fares are not inputs.
    ``None`` means no requested, evidence-backed calculation is available.
    """
    if not _monthly_estimate_requested(message) or not isinstance(transport_result, dict):
        return None
    weekly_matches = list(_FREQUENCY.finditer(message or ''))
    monthly_matches = (list(_MONTHLY_FREQUENCY.finditer(message or '')) +
                       list(_MONTHLY_FREQUENCY_ZH.finditer(message or '')))
    if not weekly_matches and not monthly_matches:
        return None
    for match in monthly_matches:
        # A shared period/unit in "26 or 30 singles per month" must not
        # silently turn the second alternative into the chosen frequency.
        before, after = message[:match.start()], message[match.end():]
        if (re.search(rf'\b{_MONTHLY_NUMBER}\s+(?:or|to)\s*$', before, re.I) or
                re.match(rf'\s*(?:or|to)\s+{_MONTHLY_NUMBER}\b', after, re.I) or
                re.match(r'\s*(?:或|至|到)\s*\d', after)):
            return None
    weekly_values = {int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1).lower()]
                     for m in weekly_matches}
    monthly_values = {int(m.group(1)) if m.group(1).isdigit() else _MONTHLY_NUMBERS[m.group(1).lower()]
                      for m in monthly_matches}
    if len(weekly_values) > 1 or len(monthly_values) > 1:
        return None
    weekly_singles = next(iter(weekly_values), None)
    monthly_singles = next(iter(monthly_values), None)
    if monthly_singles is None and weekly_singles is not None and weekly_singles > 14:
        return None  # Preserve the existing weekly interface's supported range.
    if weekly_singles is not None and monthly_singles is not None and (
            Decimal(weekly_singles) * Decimal(52) / Decimal(12) != Decimal(monthly_singles)):
        return None
    peak_requested = any(bool(m.group('peak')) for m in weekly_matches + monthly_matches)
    wrapper = transport_result
    if wrapper.get('tool') not in (None, 'get_transport_info') or wrapper.get('success') is not True:
        return None
    raw = wrapper.get('raw_data', wrapper)
    if not isinstance(raw, dict) or raw.get('success') is not True or raw.get('fare_available') is False:
        return None
    fare = _decimal(raw.get('fare_gbp'))
    if fare is None:
        return None
    monthly_journeys = (Decimal(monthly_singles) if monthly_singles is not None else
                        Decimal(weekly_singles) * Decimal(52) / Decimal(12))
    amount = (monthly_journeys * fare).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    station_fare = raw.get('station_fare') or {}
    peak_value = _decimal(station_fare.get('peak_gbp')) if isinstance(station_fare, dict) else None
    peak_verified = bool(raw.get('fare_is_peak') is True or raw.get('fare_period') == 'peak'
                         or (peak_value is not None and peak_value == fare))
    conditional_peak = peak_requested and not peak_verified
    assumptions = ([
        'Each stated monthly journey counts as one single; no extra return trips or office days are inferred.',
        'The declared monthly trip count is used directly; no weekly-to-monthly conversion is applied.',
    ] if monthly_singles is not None else [
        'Each stated weekly journey counts as one single; no extra return trips are inferred.',
        'The declared weekly frequency continues for 52 weeks; divide by 12 for an average month.',
    ]) + [
        'The observed single fare applies to every counted journey; discounts, caps and future fare changes are excluded.',
        'This estimate is not verified travel cost for a particular available flat and does not establish its combined-budget eligibility.',
    ]
    if conditional_peak:
        assumptions.append('Conditional: the returned single fare is assumed to apply at peak times; peak applicability was not verified.')
    if raw.get('fare_basis') == 'station_pair':
        assumptions.append('The fare is for the station pair; it is not verified for the selected itinerary or a home-to-work route.')
    return {
        'kind': 'estimate',
        'frequency': ({'monthly_singles': monthly_singles, 'peak_requested': peak_requested}
                      if monthly_singles is not None else
                      {'weekly_singles': weekly_singles, 'peak_requested': peak_requested}),
        'monthly_journeys': float(monthly_journeys),
        'amount': float(amount),
        'currency': 'GBP',
        'single_fare_gbp': float(fare),
        'formula': (f'{monthly_singles} × £{fare:.2f}' if monthly_singles is not None else
                    f'{weekly_singles} × 52 / 12 × £{fare:.2f}'),
        'assumptions': assumptions,
        'conditional_peak_fare': conditional_peak,
        'candidate_cost_verified': False,
        'source': raw.get('source'),
    }


def render_monthly_commuting_estimate(estimate: dict | None, language: str = 'en') -> str:
    """Human-readable estimate; never supply missing arithmetic or verified status."""
    if not isinstance(estimate, dict) or estimate.get('kind') != 'estimate':
        return ''
    frequency = estimate.get('frequency') or {}
    weekly = frequency.get('weekly_singles')
    monthly_singles = frequency.get('monthly_singles')
    monthly = _decimal(estimate.get('monthly_journeys'))
    amount = _decimal(estimate.get('amount'))
    single = _decimal(estimate.get('single_fare_gbp'))
    direct = monthly_singles is not None
    count = monthly_singles if direct else weekly
    if (not isinstance(count, int) or isinstance(count, bool) or count <= 0 or
            monthly is None or amount is None or single is None or
            (direct and (weekly is not None or monthly != Decimal(monthly_singles)))):
        return ''
    journeys = f'{monthly:.2f}'.rstrip('0').rstrip('.')
    if language == 'zh':
        text = ((f'月通勤费估算：每月 {monthly_singles} 个单程 × £{single:.2f} = £{amount:.2f}/月。'
                 '直接采用明确的每月单程总数，不额外推算往返或到岗天数，也不进行周月换算。'
                 '假设该单程票价适用于每次出行；未计优惠、票价封顶或未来调价。') if direct else
                (f'月均通勤费估算：每周 {weekly} 个单程 × 52 ÷ 12 = 每月约 {journeys} 个单程；'
                f'{journeys} × £{single:.2f} = £{amount:.2f}/月。'
                '按全年52周均摊，假设该单程票价适用于每次出行；未计优惠、票价封顶或未来调价。')) + (
                '这不是任何房源已经核实的通勤成本，也不能证明房租加通勤符合总预算。')
        if estimate.get('conditional_peak_fare'):
            text += '此估算以该票价适用于高峰时段为条件；峰时适用性尚未核实。'
        if any('station pair' in str(item) for item in estimate.get('assumptions') or []):
            text += '该票价仅针对站点组合，未核实所选行程或房源到工作地的路线。'
        return text
    text = ((f'Estimated monthly commuting: {monthly_singles} single journeys/month × '
             f'£{single:.2f} = £{amount:.2f}/month. '
             'The declared monthly trip count is used directly; no extra return trips or office days '
             'are inferred and no weekly conversion is applied. '
             'The observed fare is assumed for every trip; discounts, fare caps and future changes are excluded. ')
            if direct else
            (f'Estimated monthly commuting: {weekly} singles/week × 52 ÷ 12 = '
            f'{journeys} single journeys/month; {journeys} × £{single:.2f} = £{amount:.2f}/month. '
            'This averages 52 weeks across 12 months and assumes the observed fare for every trip; '
            'discounts, fare caps and future changes are excluded. ')) + (
            'It is not a verified cost for any flat and does not establish the combined rent-and-commuting budget.')
    if estimate.get('conditional_peak_fare'):
        text += ' Conditional on this fare applying at peak times: peak applicability has not been verified.'
    if any('station pair' in str(item) for item in estimate.get('assumptions') or []):
        text += ' The station-pair fare is not verified for the selected itinerary or a home-to-work route.'
    return text
