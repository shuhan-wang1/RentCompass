"""
Tool: Calculate Commute Cost
综合计算通勤成本的工具 - 整合通勤时间 + 交通费用 + 月度总成本

功能:
1. 计算从房源到目的地的通勤时间 (使用 TfL Journey Planner)
2. 判断路线是否需要公共交通
3. 根据起点和终点所在的 Zone 计算交通费用 (统一 TfL 2026 票价表)
4. 返回综合信息: 通勤时间、月度交通成本、建议购买哪种票

数据来源:
- 通勤时间: TfL Journey Planner（无路线时仅提供有标注的估算）
- 交通费用: TfL 官方 2026 票价表（含生效日期和来源链接）
- Zone 判断: Postcodes.io / OpenStreetMap 地理编码后的简化邮编规则
"""

from core.tool_system import Tool
from typing import Optional
import re
from uk_rent_agent.data.tfl_fares import get_zonal_fare
from core.tools.check_transport_cost import _commuting_frequency, _payg_commute_estimates


def _check_route_uses_transit(route_data: dict, requested_mode: str) -> bool:
    """
    Check if Google Maps route uses any public transport

    Args:
        route_data: Route data from Google Maps Directions API
        requested_mode: The mode requested by user ("transit", "walking", etc.)

    Returns:
        bool: True if route includes transit (bus, subway, train), False otherwise

    Logic:
    - If user requested "transit" mode, we assume they NEED public transport
    - Check if any step actually uses TRANSIT mode
    - If user requested transit but no TRANSIT steps found, it means:
      * Distance is very short (Google suggests walking)
      * OR route is not feasible by transit
    """
    if not route_data or 'legs' not in route_data:
        return False

    has_transit_step = False

    for leg in route_data.get('legs', []):
        for step in leg.get('steps', []):
            travel_mode = step.get('travel_mode', '').upper()
            # Check if this step uses public transit
            if travel_mode == 'TRANSIT':
                has_transit_step = True
                break
            # Also check transit_details if available
            if 'transit_details' in step:
                has_transit_step = True
                break

        if has_transit_step:
            break

    # If user requested transit mode, we should charge them even if Google suggests walking
    # because they likely need to use transit during busy times or bad weather
    if requested_mode == "transit":
        # If there ARE transit steps, definitely return True
        if has_transit_step:
            return True
        # If user requested transit but no transit steps found,
        # it means the distance is very short - check if it's under 20 minutes
        # If under 20 min walking, assume they can walk; otherwise assume they need transit
        duration_seconds = route_data.get('legs', [{}])[0].get('duration', {}).get('value', 0)
        duration_minutes = duration_seconds / 60

        # If walking time > 15 minutes, assume they will use transit during peak/bad weather
        if duration_minutes > 15:
            print("   ⚠️ User requested transit; walking threshold exceeded")
            return True
        else:
            print("   ℹ️ Walking threshold not exceeded; transit not needed")
            return False

    # For other modes (walking, bicycling, driving), only return True if there are actual transit steps
    return has_transit_step

def _get_zone_from_address(address: str) -> Optional[int]:
    """
    从地址判断所在的 Transport Zone
    使用 Google Maps Geocoding API 获取地址的详细信息

    优先级:
    1. 如果地址中直接提到 postcode，使用 postcode 判断
    2. 否则使用 Google Maps API 获取 postcode，然后判断

    Returns:
        int: Zone 编号 (1-6)，如果无法判断则返回 None
    """
    try:
        # A named Zone 1 station is its own evidence, before any postcode or geocoder
        # (R1-09: "Euston Station, London NW1 2RT" and geocoded King's Cross read as Zone 2).
        station_zone = _zone_from_station_name(address)
        if station_zone is not None:
            return station_zone
        # 先尝试从地址中提取 postcode (英国邮编格式: XX## #XX)
        postcode_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
        match = re.search(postcode_pattern, address.upper())

        if match:
            postcode = match.group(1)
            print("   📮 从地址提取到 postcode postcode_present=True")
            return _get_zone_from_postcode(postcode)

        # 如果无法从地址提取，使用免费地理编码（Postcodes.io/Nominatim）获取 postcode
        from core.maps_service import geocode_address

        geocode_result = geocode_address(address)
        if not geocode_result:
            print(f"   ❌ 无法 geocode 地址 address_chars={len(address)}")
            return None

        postcode = geocode_result.get('postcode')
        if postcode:
            print("   📮 从地理编码获取 postcode postcode_present=True")
            return _get_zone_from_postcode(postcode)

        print(f"   ⚠️ 无法从 geocode 结果提取 postcode")
        return None

    except Exception as e:
        print(f"   ❌ Zone 判断出错 exception_type={type(e).__name__}")
        return None


# Fare zone by London postcode DISTRICT, from the zone of the district's own stations.
#
# The previous rule derived the zone from the district NUMBER ("E 9-12 -> zone 4"), which
# is not how either system works: postcode districts are numbered alphabetically by post
# town, not by distance. 11 of 18 tested districts came out 1-3 zones wrong — E14 (Canary
# Wharf, zone 2) read as zone 5, E17 (Walthamstow, zone 3) as zone 6 — and a Stratford ->
# Waterloo commute was priced as a Zone 1-5 Travelcard, £293.40 instead of £201.60
# (2026-10-19 B-03).
#
# District granularity is still an approximation (the tool labels it
# ``zone_basis: postcode_district_estimate``): the table gives the zone of the district's
# PRINCIPAL station(s), and where that station straddles a boundary (Stratford 2/3, Hendon
# Central 3/4) the higher zone, so the recommended Travelcard covers it. A minor station in
# an outer corner of a district can still sit a zone further out (Stamford Hill in N16,
# Wood Street in E17). A district not listed here returns None and stays unpriced — the
# honest outcome the tool already reports ("Unable to determine zones"), rather than a
# number invented from arithmetic.
#
# N1, NW1 and W2 hold Zone 1 termini (King's Cross/St Pancras in N1C/N1 9, Euston and
# Marylebone/Baker Street in NW1, Paddington in W2) beside Zone 2 stations. A district that
# straddles Zones 1/2 answers 1: a Zone 1-N pass also covers Zone 2, while the Zone 2 pass
# these districts used to produce does not cover the Zone 1 terminus — King's Cross ->
# Canary Wharf was recommended a "Zone 2 Monthly Travelcard" (R1-09).
_DISTRICT_ZONES = {
    **{d: 1 for d in ("W1", "SW1", "SE1", "SW3", "SW7", "N1", "NW1", "W2")},
    **{d: 2 for d in (
        "N4", "N5", "N7", "N16",
        "NW3", "NW5", "NW6", "NW8",
        "E1", "E2", "E3", "E5", "E8", "E9", "E14",
        "SE4", "SE5", "SE8", "SE11", "SE14", "SE15", "SE16", "SE17", "SE22", "SE24",
        "SW2", "SW4", "SW5", "SW6", "SW8", "SW9", "SW10", "SW11",
        "W6", "W8", "W9", "W10", "W11", "W12", "W14")},
    **{d: 3 for d in (
        "N2", "N6", "N8", "N15", "N17", "N19", "N22",
        "NW2", "NW10", "NW11",
        "E7", "E10", "E13", "E15", "E16", "E17", "E20",
        "SE3", "SE6", "SE10", "SE13", "SE23", "SE26", "SE27",
        "SW12", "SW13", "SW14", "SW15", "SW16", "SW17", "SW18", "SW19",
        "W3", "W4", "W5")},
    **{d: 4 for d in ("N3", "N11", "N12", "N14", "NW4", "NW7", "NW9", "E6", "E11", "E12", "SW20",
                      "W7")},
}


# Named Zone 1 stations (fare zone 1, or a Zone 1/2 boundary station, where 1 is the zone
# whose pass covers it). Matched only as a STATION — the whole place ("King's Cross",
# "Paddington, London") or a name followed by "station" — never as a street name:
# "Victoria Road, Ilford" and "Euston Road" are not stations.
_ZONE_ONE_STATIONS = (
    "king's cross st pancras", "king's cross", "st pancras", "st pancras international",
    "euston", "euston square", "paddington", "marylebone", "baker street",
    "liverpool street", "waterloo", "waterloo east", "victoria", "london bridge",
    "charing cross", "fenchurch street", "cannon street", "blackfriars", "city thameslink",
    "moorgate", "bank", "monument", "oxford circus", "tottenham court road", "holborn",
    "angel", "old street", "farringdon", "barbican", "bond street", "green park",
    "piccadilly circus", "leicester square", "covent garden", "westminster", "embankment",
    "temple", "aldgate", "aldgate east", "tower hill", "tower gateway", "borough",
    "lambeth north", "russell square", "goodge street", "warren street",
    "great portland street", "regent's park", "edgware road", "lancaster gate",
    "marble arch", "hyde park corner", "knightsbridge", "south kensington", "sloane square",
    "st james's park", "chancery lane", "st paul's", "mansion house",
    "elephant and castle", "elephant & castle", "vauxhall", "pimlico", "earl's court",
    "gloucester road", "high street kensington", "notting hill gate", "queensway",
    "bayswater", "southwark",
)
_STATION_SUFFIX = r"(?:\s+(?:(?:underground|tube|rail|railway|mainline|overground|national rail|dlr)\s+)?(?:station|stn))?"


def _zone_from_station_name(address: str) -> Optional[int]:
    """1 when ``address`` names a Zone 1 station as a station, else None."""
    text = " ".join(re.sub(r"[\u2019']", "'", str(address or "")).lower().split())
    if not text:
        return None
    text = re.sub(r"\b[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}\b", " ", text)          # postcode
    text = re.sub(r"\bst\.\s*", "st ", text)
    text = re.sub(r"^(?:london\s+)|[,\s]+(?:london|uk|united kingdom|england)\b", " ", text)
    text = " ".join(text.replace(",", " ").split()).strip(" .")
    for name in sorted(_ZONE_ONE_STATIONS, key=len, reverse=True):
        stem = re.escape(name).replace("'", "'?")
        if re.fullmatch(stem + _STATION_SUFFIX, text) or re.search(
                r"(?<![a-z])" + stem + r"\s+(?:(?:underground|tube|rail|railway|mainline|overground|"
                r"national rail|dlr)\s+)?(?:station|stn)\b", text):
            return 1
    return None


def _get_zone_from_postcode(postcode: str) -> Optional[int]:
    """Fare zone for a London postcode, or None when it cannot be established.

    Only the outward district is read ("N7 0EG" -> "N7"; without a space the 3-character
    inward code is dropped: "N70EG" -> "N7"). EC and WC districts are zone 1 throughout;
    every other district is looked up in ``_DISTRICT_ZONES``, and an unlisted one (outer
    London, Watford, anywhere outside TfL zones) is None, never a guess.
    """
    pc = str(postcode or "").upper().strip()
    if not pc:
        return None
    district = pc.split(' ')[0] if ' ' in pc else (pc[:-3] if len(pc) > 3 else pc)
    if re.fullmatch(r'(?:EC|WC)\d[A-Z]?', district):
        return 1
    match = re.fullmatch(r'([A-Z]{1,2}\d{1,2})([A-Z]?)', district)
    if not match:
        return None
    # A sub-district letter ("W1F", "SW1A", "SE1P") belongs to its district.
    return _DISTRICT_ZONES.get(match.group(1))


def _commute_time_text(measured_minutes: Optional[int], details: dict) -> str:
    """The commute-time SENTENCE for the summary block, with its basis inside the string.

    A sibling ``basis`` key is what ``route_source`` was: computed, then never read. The summary
    is the part of this payload the model quotes verbatim, so "2 minutes" has to stop being a
    renderable string at all when nothing measured it.
    """
    if measured_minutes is not None:
        return f"{measured_minutes} minutes (measured: TfL journey plan)"
    low = details.get('estimate_low_minutes')
    high = details.get('estimate_high_minutes')
    est = details.get('estimated_duration_minutes')
    if est is not None and low is not None and high is not None:
        return (f"estimated {low}-{high} minutes (straight-line estimate, NOT a journey plan; "
                f"point figure {est})")
    return ("not established — TfL returned no journey for this pair and the straight-line "
            "figure is not trustworthy at this distance, so no commute time is given")


def _monthly_hours_clause(measured_minutes: Optional[int], details: dict,
                          commuting_days_per_month: float = 22) -> str:
    """The "~N hours/month commuting" clause, or an honest refusal in its place.

    ``minutes x 2 x 22`` inherits every property of the minutes it is built from. Derived from a
    measured journey it is a fact; derived from a straight-line guess it is the guess with a 44x
    lever on it, which is how a 2-minute estimate became "1.5 hours a month" as a stated total.
    """
    if measured_minutes is not None:
        return (f" + {measured_minutes * 2} min/day × {commuting_days_per_month:g} workdays = "
                f"~{measured_minutes * 2 * commuting_days_per_month / 60:.1f} hours/month")
    low = details.get('estimate_low_minutes')
    high = details.get('estimate_high_minutes')
    if low is not None and high is not None:
        return (f". Commuting HOURS are not stated as a fact: no journey plan exists for this "
                f"pair, so the time is an estimated {low}-{high} min each way "
                f"(~{low * 2 * commuting_days_per_month / 60:.1f}-"
                f"{high * 2 * commuting_days_per_month / 60:.1f} hours/month if that estimate holds)")
    return (". Commuting HOURS are not stated: no commute time could be established for this "
            "pair, and the fare above does not depend on one")


def calculate_commute_cost_impl(
    from_address: str,
    to_address: str,
    travel_type: str = "student",
    mode: str = "transit",
    office_days_per_week: Optional[float] = None,
) -> dict:
    """
    计算综合通勤成本（时间 + 费用）

    NOTE: this is a PLAIN SYNC function on purpose. calculate_travel_time and
    _get_zone_from_address perform SYNCHRONOUS network I/O (TfL Journey Planner + free
    geocoding). Registering it as sync means Tool.execute offloads it to an executor thread
    (tool_system.py :279-284), keeping the asyncio event loop responsive so the fc-loop's
    per-tool timeout / batch budget can fire (the confirmed four-concurrent-calls-to-52s bug).

    Args:
        from_address: 起点地址 (房源地址)
        to_address: 终点地址 (学校/工作地点)
        travel_type: 乘客类型 ('student' 或 'adult')
        mode: 通勤方式 ('transit', 'driving', 'walking', 'bicycling')

    Returns:
        dict: 包含通勤时间、Zone 信息、月度交通费用等
    """
    try:
        try:
            frequency = _commuting_frequency(office_days_per_week)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'office_days_per_week must be between 0 and 7.',
                    'retryable': False}
        print(
            "\n   🚇 计算综合通勤成本 "
            f"from_chars={len(from_address)} to_chars={len(to_address)} "
            f"student_request={'student' in travel_type.lower()} "
            f"mode_supported={mode in {'transit', 'driving', 'walking', 'bicycling'}}"
        )

        # Step 1: 通勤时间 (TfL Journey Planner，免费；非伦敦自动回退直线估算)
        #
        # THE GAP THAT WAS HERE (closed 2026-07-27). This used to call calculate_travel_time,
        # which returns a BARE int and silently falls back to the straight-line estimator, and
        # then put that int into `commute.duration_minutes` — the field that means "a journey
        # planner measured this" — and derived duration_category / is_acceptable / a monthly-
        # hours figure from it. For a 0.47 km pair calculate_commute said "estimated 11 minutes
        # (9-14), straight-line basis" while this tool stated "2 minutes" as fact, in the same
        # turn, about the same pair. The stated reason for leaving it was that
        # calculate_travel_time is the CACHED entry point; maps_service.calculate_travel_basis
        # is now that cached entry point AND basis-aware, so there is nothing left to trade.
        from core.maps_service import calculate_travel_basis
        from core.commute_basis import (
            best_estimate_minutes, is_measured, withdraw_uncalibrated_mode,
        )

        details = calculate_travel_basis(from_address, to_address, mode)
        if not details:
            return {
                'success': False,
                'error': ('Could not calculate a route (an address could not be geocoded '
                          'or no route was found).')
            }
        # Defence in depth. maps_service now threads `mode` into describe_estimate, so this is
        # a no-op on that path; it still catches a payload produced by anything that does not.
        details = withdraw_uncalibrated_mode(details, mode)

        measured_minutes = details.get('duration_minutes')
        if not is_measured(details.get('source')):
            measured_minutes = None          # a guess never occupies the measured field
        estimated_minutes = details.get('estimated_duration_minutes')
        straight_line_km = details.get('straight_line_km')

        # A figure for INTERNAL thresholding only — the fare heuristic below and nothing else.
        # It is never returned and never rendered, so it is allowed to exist where a published
        # figure would be refused; see maps_service.calculate_travel_time for the same split.
        threshold_minutes = (measured_minutes if measured_minutes is not None
                             else best_estimate_minutes(straight_line_km, mode))

        if measured_minutes is not None:
            print("   ✅ Measured TfL journey route found")
        elif estimated_minutes is not None:
            print("   ⚠️ No journey plan; publishable straight-line estimate available")
        else:
            print(f"   ⚠️ No journey plan and no publishable estimate for this pair")

        # Step 2: 是否需要公共交通票价
        # TfL/估算没有逐步路线信息，用启发式：transit 模式且时长 > 15 分钟视为需要公共交通。
        # 该启发式只决定"是否查票价"，不对外公布分钟数，因此可以使用 threshold_minutes。
        uses_transit = (mode == "transit" and threshold_minutes is not None
                        and threshold_minutes > 15)
        print(f"   🚌 Route uses public transport (heuristic): {uses_transit}")

        transport_cost_info = None

        # Only calculate transport cost if route actually uses public transit
        if uses_transit:
            # 获取起点和终点的 Zone
            from_zone = _get_zone_from_address(from_address)
            to_zone = _get_zone_from_address(to_address)

            if from_zone and to_zone:
                # 确定 Zone 范围
                min_zone = min(from_zone, to_zone)
                max_zone = max(from_zone, to_zone)
                user_type = "student" if "student" in travel_type.lower() else "adult"
                try:
                    prices = get_zonal_fare(min_zone, max_zone, user_type)
                except (TypeError, ValueError) as exc:
                    transport_cost_info = {
                        "error": "Fare lookup failed. Please check tfl.gov.uk/fares."
                    }
                else:
                    zone_label = (
                        f"Zone {min_zone}" if min_zone == max_zone
                        else f"Zone {min_zone}-{max_zone}"
                    )
                    print("   📍 Looking up zonal fares")
                    payg = _payg_commute_estimates(prices['daily_cap'], office_days_per_week)
                    payg_monthly_estimate = payg['payg_monthly_estimate']
                    monthly_travelcard = prices['monthly']
                    recommended_cost = min(payg_monthly_estimate, monthly_travelcard)
                    recommended_product = (
                        "Monthly Travelcard"
                        if monthly_travelcard <= payg_monthly_estimate
                        else ("PAYG daily caps (22-day estimate)" if office_days_per_week is None
                              else "PAYG daily caps (hybrid-work monthly average)")
                    )
                    is_student = user_type == "student"

                    transport_cost_info = {
                        **payg,
                        "from_zone": from_zone,
                        "to_zone": to_zone,
                        "recommended_pass": f"{zone_label} {recommended_product}",
                        "user_type": (
                            "18+ Student Oyster" if is_student else "Adult Oyster/contactless"
                        ),
                        # Backwards-compatible field, now explicitly the cheaper
                        # of the two published options rather than a PAYG-only guess.
                        "monthly_cost": recommended_cost,
                        "monthly_cost_basis": recommended_product,
                        "payg_monthly_estimate_22_days": round(prices['daily_cap'] * 22, 2),
                        "monthly_travelcard": monthly_travelcard,
                        "weekly_travelcard": prices['weekly'],
                        "daily_cap": prices['daily_cap'],
                        "daily_off_peak_cap": prices['daily_off_peak_cap'],
                        "daily_off_peak_cap_note": prices['daily_off_peak_cap_note'],
                        "currency": "GBP",
                        "effective_date": prices['effective_date'],
                        "fare_edition": prices['edition'],
                        "source": prices['source'],
                        "source_url": prices['source_url'],
                        'zone_basis': 'postcode_district_estimate',
                        'coverage_verified': False,
                        'coverage_note': ('Postcode-derived zones are approximate, not verified '
                                          'station or itinerary coverage. Out-of-zone services '
                                          'and supplements require separate fare evidence.'),
                        "note": (
                            f"Compared the standard PAYG daily cap (£{prices['daily_cap']:.2f}) "
                            f"× {payg['commuting_days_per_month']:g} average commuting days/month "
                            f"(£{payg_monthly_estimate:.2f}) with the "
                            f"{zone_label} monthly Travelcard (£{monthly_travelcard:.2f}). "
                            "18+ Student Oyster discounts apply to Travelcards, not standard PAYG."
                        ),
                    }
            else:
                transport_cost_info = {
                    "error": "Unable to determine zones for one or both addresses. Cannot calculate transport cost.",
                    "from_zone": from_zone,
                    "to_zone": to_zone,
                    **frequency,
                    'coverage_verified': False,
                    'zone_basis': 'not_established',
                    'coverage_note': 'No zonal price is assigned when either address has an unknown zone.'
                }

        # Step 3: 组装完整结果
        #
        # duration_minutes / duration_category / is_acceptable are claims only a MEASURED
        # journey supports. On the estimate branch they are None and the figure travels in
        # estimated_duration_minutes with its range, its model and its basis — the same
        # contract calculate_commute returns, so the two tools now agree field for field as
        # well as number for number.
        commute_block = {
            'duration_minutes': measured_minutes,
            'duration_category': (
                None if measured_minutes is None
                else 'Short (< 20 min)' if measured_minutes < 20
                else 'Medium (20-45 min)' if measured_minutes <= 45
                else 'Long (> 45 min)'
            ),
            'is_acceptable': None if measured_minutes is None else measured_minutes <= 45,
            'basis': details.get('basis'),
            'basis_note': details.get('basis_note'),
        }
        if measured_minutes is None:
            commute_block.update({
                'estimated_duration_minutes': estimated_minutes,
                'estimate_low_minutes': details.get('estimate_low_minutes'),
                'estimate_high_minutes': details.get('estimate_high_minutes'),
                'estimate_model': details.get('estimate_model'),
                'straight_line_km': straight_line_km,
                'caveat': details.get('caveat'),
            })

        result = {
            **frequency,
            'success': True,
            'from_address': from_address,
            'to_address': to_address,
            'mode': mode,
            'uses_public_transport': uses_transit,
            'commute': commute_block,
            'transport_cost_available': bool(transport_cost_info and
                                             'monthly_cost' in transport_cost_info),
            'partial': bool(transport_cost_info and transport_cost_info.get('error')),
        }
        if measured_minutes is None:
            # The instruction goes WITH the payload, exactly as in calculate_commute: a basis
            # field the prompt never mentions is what route_source was.
            if estimated_minutes is None:
                km_clause = (
                    f" The one fact available is the straight-line distance, "
                    f"{straight_line_km} km — you may state that as a distance, but it is not a "
                    f"travel time." if isinstance(straight_line_km, (int, float)) else "")
                result['recommendation'] = (
                    "No commute TIME is available for this pair — the fare figures below stand "
                    "on their own. Say that plainly; do NOT state a number of minutes, and do "
                    "not infer one from the distance." + km_clause)
            else:
                result['recommendation'] = (
                    f"There is no journey plan for this pair. If you mention a time at all, give "
                    f"it as an estimated {details.get('estimate_low_minutes')}-"
                    f"{details.get('estimate_high_minutes')} minute range and say it is estimated "
                    f"from the straight-line distance, not measured. Never state "
                    f"'{estimated_minutes} minutes' as the commute."
                    + (f" Basis to disclose: {details.get('basis_note')}"
                       if details.get('basis_note') else ""))

        # The summary strings are what the model actually quotes, so the basis has to be IN
        # them and the monthly-HOURS figure — duration x 2 x 22 — has to inherit the refusal:
        # it is the same unbacked minute count multiplied by 44, and rendering it as a fact
        # was the loudest form of the defect.
        commute_time_txt = _commute_time_text(measured_minutes, details)
        hours_clause = _monthly_hours_clause(measured_minutes, details,
                                             frequency['commuting_days_per_month'])

        # 添加交通费用信息（如果路线使用公共交通且计算成功）
        if transport_cost_info:
            result['transport_cost'] = transport_cost_info

            # 如果成功计算了月度费用，添加到结果摘要
            if 'monthly_cost' in transport_cost_info:
                result['summary'] = {
                    'commute_time': commute_time_txt,
                    'monthly_transport_cost': f"£{transport_cost_info['monthly_cost']:.2f}",
                    'recommended_pass': transport_cost_info.get('recommended_pass', 'N/A'),
                    'uses_public_transport': 'Yes' if uses_transit else 'No',
                    'total_commuting_cost_per_month': (
                        f"£{transport_cost_info['monthly_cost']:.2f} transport"
                        f"{hours_clause}"),
                }
        elif not uses_transit:
            # 如果不使用公共交通，明确说明无交通成本
            result['summary'] = {
                'commute_time': commute_time_txt,
                'monthly_transport_cost': '£0.00',
                'uses_public_transport': 'No',
                'note': 'Route does not require public transport (walking/cycling only)'
            }

        return result

    except Exception as exc:
        print(f"   ❌ 通勤成本计算出错 exception_type={type(exc).__name__}")
        return {
            "success": False,
            "error": "Commute cost calculation failed",
            "retryable": True,
        }


# 创建工具实例
calculate_commute_cost_tool = Tool(
    name="calculate_commute_cost",

    description="""Calculate commute time plus a monthly zonal fare comparison from a listing to a destination. Pass office_days_per_week for hybrid work: weekly PAYG caps × office days, average monthly × 52/12. Omitted attendance preserves 22 commuting days/month. Caps are upper bounds, not actual single/return fares. Postcode-zone estimates do not verify route coverage; unknown/out-of-zone locations such as Watford Junction must remain unpriced rather than defaulting to zones 1-2 or 6.
READ THE BASIS FIELD — same contract as calculate_commute: `commute.duration_minutes` is populated ONLY when TfL returned a real journey plan (basis=tfl_journey_plan) and is the only figure you may state as a commute time. When TfL has no journey the block instead carries `estimated_duration_minutes` plus `estimate_low_minutes`/`estimate_high_minutes` (basis=straight_line_estimate) and `duration_category`/`is_acceptable` are null — quote that as an estimated RANGE and say it is derived from the straight-line distance, or, when it is null too, say no commute time is available. The FARE figures are unaffected either way and may always be stated. Never present an estimate as a measured journey time, and never state a monthly commuting-HOURS total derived from one.
计算房源到目的地的通勤时间与月度交通费用（票价来源 TfL）。""",

    func=calculate_commute_cost_impl,

    parameters={
        'type': 'object',
        'properties': {
            'from_address': {
                'type': 'string',
                'description': '起点地址（房源完整地址，包括门牌号、街道名、城市、邮编）'
            },
            'to_address': {
                'type': 'string',
                'description': '终点地址（学校/工作地点完整地址）'
            },
            'travel_type': {
                'type': 'string',
                'enum': ['student', 'adult'],
                'description': '乘客类型。学生使用 18+ Student Oyster Card 可享受 Travelcard 30% 折扣',
                'default': 'student'
            },
            'mode': {
                'type': 'string',
                'enum': ['transit', 'driving', 'walking', 'bicycling'],
                'description': '通勤方式。注意：只有 transit (公共交通) 才会计算交通费用',
                'default': 'transit'
            },
            'office_days_per_week': {
                'type': 'number', 'minimum': 0, 'maximum': 7,
                'description': 'Return commuting days per week; use the latest user attendance (e.g. 2 or 3). Optional; omitted preserves the legacy 22 commuting days/month.'
            },
        },
        'required': ['from_address', 'to_address']
    },

    max_retries=2
)
