"""One complete criterion snapshot drives validation, persistence and the panel."""
from copy import deepcopy


def synchronize_monthly_budget_alias(criteria: dict) -> dict:
    """Mirror a determined canonical monthly rent, including a protected clear.

    A conflicting historical alias must not widen a hard ceiling. User-message
    ownership is checked against the deterministic rent parser before replacing
    that alias; form and recovered canonical values already carry their authority.
    Weekly values are never relabelled as monthly values here.
    """
    if (not isinstance(criteria, dict) or 'max_budget' not in criteria
            or 'max_budget_monthly' not in criteria):
        return criteria
    value = criteria['max_budget']
    sources = criteria.get('constraint_sources') or {}
    source = sources.get('max_budget') if isinstance(sources, dict) else None
    owns = isinstance(source, dict) and 'value' in source and source['value'] == value
    if value is None:
        confirmed = owns
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return criteria
        period = str(criteria.get('budget_period') or '').lower()
        form_default = owns and source.get('source') in ('form', 'user_form', 'manual') and not period
        if period not in ('month', 'monthly', 'pcm') and not form_default:
            return criteria
        confirmed = owns and source.get('source') in (
            'form', 'user_form', 'manual', 'saved_preference', 'criteria_recovery')
        if owns and source.get('source') == 'user_message' and source.get('text'):
            from core.tools.search_properties import _extract_budget
            amount, declared_period = _extract_budget(source['text'])
            if declared_period == 'week' and amount is not None:
                from core.tenancy_reference import monthly_from_weekly
                amount = monthly_from_weekly(amount)
            confirmed = amount == value
        if 'max_budget_monthly' not in criteria:
            confirmed = confirmed or not source
    if not confirmed or ('max_budget_monthly' in criteria and criteria['max_budget_monthly'] == value):
        return criteria
    result = deepcopy(criteria)
    result['max_budget_monthly'] = value
    return result


def reconcile_search_criteria(accumulated: dict, searched: dict) -> dict:
    """Fill unconfirmed missing slots from the executed tool, honoring explicit clears.

    Empty defaults are not user instructions to remove a previously enforced constraint.
    The historical search echo's `destination` may mean the area, so only its explicit
    `commute_destination` is accepted as a commute target.
    """
    result = deepcopy(accumulated or {})
    searched = searched or {}
    sources = result.get('constraint_sources') or {}
    aliases = {
        'area': ('area',), 'areas': ('areas',),
        'commute_destination': ('commute_destination',),
        'max_budget': ('max_budget_monthly','max_budget'),
        'max_travel_time': ('max_commute_time','max_travel_time'),
        'bedrooms': ('bedrooms',), 'bedrooms_minimum': ('bedrooms_minimum',),
        'room_type': ('room_type',), 'move_in_date': ('move_in_date',),
        'monthly_total_budget': ('monthly_total_budget',),
        'office_days_per_week': ('office_days_per_week',), 'max_changes': ('max_changes',),
        'property_features': ('property_features',), 'soft_preferences': ('soft_preferences',),
        'budget_period': ('budget_period',), 'no_commute': ('no_commute',),
    }
    # Normalize the accumulated alias before considering the tool's view.
    if (result.get('commute_destination') is None and result.get('destination')
            and 'commute_destination' not in sources):
        result['commute_destination'] = result['destination']
    for field, keys in aliases.items():
        if field in sources:
            continue  # includes explicit None/[]/False/0 supplied by the user
        current = result.get(field)
        if current is not None and current != '' and current != []:
            continue
        for key in keys:
            value = searched.get(key)
            if value is not None and value != '' and value != []:
                result[field] = deepcopy(value)
                break
    areas = result.get('areas') or ([result['area']] if result.get('area') else [])
    result['areas'] = list(areas)
    result['area'] = areas[0] if areas else None
    result['destination'] = result.get('commute_destination')
    return synchronize_monthly_budget_alias(result)


def apply_form_criteria(accumulated: dict, form: dict) -> dict:
    """The visible form replaces its slots and preserves unexposed hard constraints."""
    result = deepcopy(accumulated or {})
    result.update(deepcopy(form))
    sources = dict(result.get('constraint_sources') or {})
    for field, value in form.items():
        sources[field] = {'source':'form', 'value':deepcopy(value)}
    result['constraint_sources'] = sources
    result['destination'] = result.get('commute_destination')
    # Validation also accepts these historical aliases. They must follow the form's
    # replacement (including an explicit clear), rather than retain an older ceiling.
    if 'max_budget' in form:
        if form['max_budget'] is None:
            result['max_budget_monthly'] = None
        elif str(result.get('budget_period') or '').lower() in ('week', 'weekly', 'pw'):
            result.pop('max_budget_monthly', None)
        else:
            result['max_budget_monthly'] = form['max_budget']
    if 'max_travel_time' in form:
        result['max_commute_time'] = form['max_travel_time']
    return result
