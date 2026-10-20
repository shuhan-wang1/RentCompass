"""The criteria and cards of a completed turn travel as one versioned snapshot."""
from copy import deepcopy


def view_state(snapshot: dict | None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    state = {
        'operation': 'replace',
        'revision': int(snapshot.get('context_revision') or 0),
        'criteria': deepcopy(snapshot.get('accumulated_search_criteria') or {}),
        'results': deepcopy(snapshot.get('view_results', snapshot.get('last_results')) or []),
    }

    retrieval = snapshot.get('search_retrieval')
    if retrieval is None:
        retrieval = (snapshot.get('extracted_context') or {}).get('search_retrieval')
    if isinstance(retrieval, dict):
        state['retrieval'] = deepcopy(retrieval)
    return state
