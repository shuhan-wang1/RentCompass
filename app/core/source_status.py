"""User-facing source failures, kept distinct from an empty candidate sample."""
from __future__ import annotations

FAILED_SOURCE_STATES = frozenset({
    'blocked', 'rate_limited', 'unavailable', 'schema_error', 'robots_disallowed',
    'cooldown', 'timeout', 'error',
})


def source_failure_summary(results_by_area: dict, *, language: str = 'en') -> dict:
    failures = {}
    for area, result in (results_by_area or {}).items():
        meta = (result or {}).get('meta') or {}
        status = meta.get('source_status')
        if status not in FAILED_SOURCE_STATES:
            continue
        error = meta.get('source_error') or {}
        safe = {key: error[key] for key in ('status_code', 'reason', 'retry_after_seconds')
                if key in error and isinstance(error[key], (str, int, float, bool, type(None)))}
        failures[area] = {'status': status, **safe}
    if not failures:
        return {}
    # Other areas returned complete results: the failure is THAT area's, not a source outage.
    # "The OnTheMarket listing source is temporarily unavailable" led an answer listing four
    # verified homes from the other areas, because one requested area (Liverpool, from
    # "Liverpool Street") was not covered (R2-07).
    served = [area for area, result in (results_by_area or {}).items()
              if area not in failures
              and ((result or {}).get('meta') or {}).get('source_status') not in FAILED_SOURCE_STATES
              and ((result or {}).get('meta') or {}).get('retrieval_complete') is not False]
    if served:
        failed_names = ', '.join(str(area) for area in failures)
        served_names = ', '.join(str(area) for area in served)
        message = (f'未能获取 {failed_names} 的房源（该区域不在覆盖范围内或来源暂不可用）；'
                   f'以下结果来自 {served_names}。' if language == 'zh' else
                   f'Listings for {failed_names} could not be retrieved (the area is not covered '
                   f'or its source was unavailable); the results below cover {served_names}.')
        return {'source_status': 'partial', 'source_errors': failures, 'source_message': message,
                'retrieval_complete': False}
    statuses = {item['status'] for item in failures.values()}
    status = ('blocked' if statuses & {'blocked', 'robots_disallowed', 'cooldown'} else
              'rate_limited' if 'rate_limited' in statuses else
              'schema_error' if 'schema_error' in statuses else 'unavailable')
    codes = sorted({str(item['status_code']) for item in failures.values()
                    if item.get('status_code') is not None})
    code_note = (' (HTTP ' + ', '.join(codes) + ')') if codes else ''
    if language == 'zh':
        reason = ('访问受限' if status == 'blocked' else '请求受到限流' if status == 'rate_limited'
                  else '页面数据结构发生变化' if status == 'schema_error' else '暂时不可用')
        message = (f'OnTheMarket 房源来源{reason}{code_note}，本次无法核实最新房源。'
                   '您的找房条件已保留；这不表示当前预算或区域没有房源。')
    else:
        reason = ('has restricted access' if status == 'blocked' else 'is rate limiting requests'
                  if status == 'rate_limited' else 'returned an unsupported page format'
                  if status == 'schema_error' else 'is temporarily unavailable')
        message = (f'The OnTheMarket listing source {reason}{code_note}, so current listings '
                   'could not be verified. Your housing criteria are preserved; this does '
                   'not show that your budget or area has no properties.')
    return {'source_status': status, 'source_errors': failures, 'source_message': message,
            'retrieval_complete': False}


RETRIEVAL_FIELDS = (
    'source_status', 'source_errors', 'source_message', 'retrieval_complete',
    'retrieval_outcome', 'partial', 'partial_note', 'incomplete_areas', 'area_status',
    'data_source', 'possibly_outdated',
)


def retrieval_metadata(data: dict) -> dict:
    """Only source/coverage metadata travels to the persisted results panel."""
    return {key: data[key] for key in RETRIEVAL_FIELDS if key in data}
