"""Pure read projection for snapshots written before a proven parser correction.

Callers provide only the authenticated conversation's snapshot and user history.
Historical messages and durable snapshots are left intact for auditability.
"""
from copy import deepcopy


def recover_snapshot(snapshot: dict | None, messages: list[dict]) -> dict | None:
    if not isinstance(snapshot, dict):
        return snapshot
    from core.criteria_recovery import repair_criteria_sources, preferences_after_criteria_repair
    from core.candidate_validation import revalidate_view_results
    result = deepcopy(snapshot)
    criteria = repair_criteria_sources(result.get('accumulated_search_criteria') or {}, messages)
    result['accumulated_search_criteria'] = criteria
    result['user_preferences'] = preferences_after_criteria_repair(
        result.get('user_preferences') or {}, criteria)
    rows = result.get('view_results', result.get('last_results')) or []
    result['view_results'] = revalidate_view_results(rows, criteria)
    return result
