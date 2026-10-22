"""The criteria and cards of a completed turn travel as one versioned snapshot."""
from copy import deepcopy


def _without_source_text(value, inside: bool = False):
    """``value`` with every text under a ``constraint_sources`` mapping dropped."""
    if isinstance(value, dict):
        return {key: _without_source_text(item, inside or key == 'constraint_sources')
                for key, item in value.items() if not (inside and key == 'text')}
    if isinstance(value, list):
        return [_without_source_text(item, inside) for item in value]
    return deepcopy(value)


def browser_criteria(criteria) -> dict:
    """The criteria as the browser may see them (r6-C1).

    The snapshot keeps each source's full user text for the server; the browser gets a
    bounded excerpt with sensitive clauses removed. If the projection cannot run, the
    source texts are dropped rather than shipped raw."""
    return browser_response(criteria if isinstance(criteria, dict) else {})


def browser_response(payload):
    """A response body with every ``constraint_sources`` text projected (see above)."""
    try:
        from core.criteria_projection import browser_payload
        return browser_payload(payload)
    except Exception:
        return _without_source_text(payload)


def view_state(snapshot: dict | None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    state = {
        'operation': 'replace',
        'revision': int(snapshot.get('context_revision') or 0),
        'criteria': browser_criteria(snapshot.get('accumulated_search_criteria') or {}),
        'results': deepcopy(snapshot.get('view_results', snapshot.get('last_results')) or []),
    }

    retrieval = snapshot.get('search_retrieval')
    if retrieval is None:
        retrieval = (snapshot.get('extracted_context') or {}).get('search_retrieval')
    if isinstance(retrieval, dict):
        state['retrieval'] = deepcopy(retrieval)
    return state
