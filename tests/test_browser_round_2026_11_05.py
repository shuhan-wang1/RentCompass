"""Browser test of 2026-11-05: a UCL student, Bloomsbury, September 2027, studio or en-suite,
40 minutes, £300 a week including bills; then the budget tightened to £250 a week.

1. The first listing shown as the best-matching studio (Guilford Street, £1,127) said on its
   own page that it was let only to nurses, NHS staff and medical workers, that its kitchen
   and bathroom were shared, and that electricity was extra. Each condition is now checked
   against the listing's own words, and a conflict is quoted, excludes the listing and
   withdraws any "best match" / "verified" sentence about it.
2. "My budget is actually a strict £250 a week, bills included, please search again" was
   read as a question about bills: the budget stayed £1,300 a month, the panel kept the old
   filter and £1,127 / £1,150 rows still read "Within Budget". A changed filter after shown
   results now also searches again before the answer.
3. Units with one street address ("Grays Inn Road, WC1X" at £1,300 and £1,399) carry their
   listing number wherever the ledger lists them together.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from core.candidate_validation import (answer_lead_without_unverified_claims, hard_constraints,
                                       render_candidate_status, revalidate_view_results,
                                       validate_candidates)
from core.listing_conditions import (bills_evidence, check_listing_conditions, move_in_condition,
                                     tenant_restrictions)

GUILFORD_PAGE = (
    "AVAILABLE EXCLUSIVELY TO NURSES, NHS STAFF AND MEDICAL WORKERS A well-presented, fully "
    "furnished bedsit in a fantastic location in WC1, ideally situated in the heart of London's "
    "medical district, close to numerous hospitals. The accommodation comprises a fresh, clean "
    "and smart double bedroom, with a shared kitchen and bathroom used by one other room. All "
    "utilities included apart from electricity (the heating is electricity). Fully Furnished")
LICENSED_PAGE = (
    "LICENSED FOR NURSES / NHS / MEDICAL WORKERS ONLY. Fresh, clean double bedroom with a "
    "kitchenette and en-suite. Fully Furnished. Bills are not included. Electric for your room "
    "is additional to the rent.")

CRITERIA = {"max_budget": 1300, "room_type": "studio+ensuite", "areas": ["bloomsbury"],
            "move_in_date": "2027-09-01", "property_features": ["bills included"],
            "tenant_status": "student"}


def _guilford(**extra):
    row = {"address": "Guilford Street, London WC1N", "price": "£1127/month", "room_type": "studio",
           "property_type": "Studio", "area": "bloomsbury",
           "url": "https://www.onthemarket.com/details/20630206/",
           "explanation": "Studio to rent in Guilford Street, London WC1N",
           "description": GUILFORD_PAGE, "available_from": "2027-01-07"}
    row.update(extra)
    return row


def _grays(number, price):
    return {"address": "Grays Inn Road, Islington WC1X", "price": f"£{price}/month",
            "room_type": "studio", "property_type": "Studio", "area": "bloomsbury",
            "url": f"https://www.onthemarket.com/details/{number}/", "available_from": "",
            "explanation": "Studio to rent in Grays Inn Road. Amenities: Bills Included, Furnished. "
                           "All Bills Included. Students welcome"}


# ── 1. the listing's own words, condition by condition ───────────────────────────────────

def test_each_condition_of_the_nhs_bedsit_is_a_quoted_conflict_for_a_student():
    rows = {r["condition"]: r for r in check_listing_conditions(_guilford(), hard_constraints(CRITERIA))}
    assert rows["eligibility"]["status"] == "not_met"
    assert rows["eligibility"]["quote"] == "AVAILABLE EXCLUSIVELY TO NURSES, NHS STAFF AND MEDICAL WORKERS"
    assert rows["private_bathroom"]["status"] == "not_met"
    assert "shared kitchen and bathroom" in rows["private_bathroom"]["quote"]
    assert rows["bills"]["status"] == "not_met"
    assert "apart from electricity" in rows["bills"]["quote"]
    assert rows["move_in"]["status"] == "unknown"


def test_a_restriction_is_unconfirmed_when_the_user_never_said_who_they_are():
    constraints = hard_constraints(dict(CRITERIA, tenant_status=None))
    rows = {r["condition"]: r for r in check_listing_conditions(_guilford(), constraints)}
    assert rows["eligibility"]["status"] == "unknown"
    assert "not confirmed" in rows["eligibility"]["reason"]


@pytest.mark.parametrize("tenant,status", [("healthcare", "met"), ("other", "unknown"),
                                           ("postgraduate", "not_met")])
def test_who_the_user_is_decides_an_nhs_only_listing(tenant, status):
    constraints = hard_constraints(dict(CRITERIA, tenant_status=tenant))
    rows = {r["condition"]: r for r in check_listing_conditions(_guilford(), constraints)}
    assert rows["eligibility"]["status"] == status


def test_licensed_for_medical_workers_only_and_bills_not_included():
    assert [group for group, _ in tenant_restrictions(LICENSED_PAGE)] == ["healthcare"]
    assert bills_evidence(LICENSED_PAGE) == ("not_met", "Bills are not included")


@pytest.mark.parametrize("text,state", [
    ("Amenities: Bills Included, Furnished. All Bills Included.", "met"),
    ("All utilities included apart from electricity.", "not_met"),
    ("Rent is £1,200 pcm plus bills.", "not_met"),
    ("A bright studio near the park.", "unknown"),
])
def test_bills_read_from_the_listing(text, state):
    assert bills_evidence(text)[0] == state


def test_an_en_suite_room_with_a_shared_kitchen_still_fits_studio_or_en_suite():
    page = "Large double room with en-suite shower room and a shared kitchen on the first floor."
    rows = check_listing_conditions({"description": page}, hard_constraints({"room_type": "studio+ensuite"}))
    assert not [r for r in rows if r["status"] == "not_met"]


def test_a_studio_only_search_rejects_a_shared_kitchen():
    page = "Studio room with its own shower room; the kitchen is shared with two other rooms."
    rows = check_listing_conditions({"description": page}, hard_constraints({"room_type": "studio"}))
    assert [r["condition"] for r in rows if r["status"] == "not_met"] == ["own_kitchen"]


def test_no_shared_bathroom_is_not_a_conflict():
    page = "No shared bathroom here: you get your own shower room."
    rows = check_listing_conditions({"description": page}, hard_constraints({"room_type": "ensuite"}))
    assert [r["status"] for r in rows if r["condition"] == "private_bathroom"] == ["met"]


def test_a_start_date_months_before_the_move_in_is_not_confirmed():
    today = date(2026, 11, 5)
    assert move_in_condition("2027-01-07", "2027-09-01", today=today)["status"] == "unknown"
    assert move_in_condition("Available now", "2027-09-01", today=today)["status"] == "unknown"
    assert move_in_condition("2027-08-20", "2027-09-01", today=today) is None
    assert move_in_condition("Available now", "2026-11-20", today=today) is None


def test_the_nhs_bedsit_is_excluded_and_the_ledger_quotes_it():
    ledger = validate_candidates([_guilford()], CRITERIA)
    assert [s["status"] for s in ledger["statuses"]] == ["excluded"]
    text = render_candidate_status(ledger)
    assert "Guilford Street, London WC1N (£1127/month): listing is let only to nurses" in text
    assert "Listing says: “AVAILABLE EXCLUSIVELY TO NURSES, NHS STAFF AND MEDICAL WORKERS”" in text
    assert "shared kitchen and bathroom" in text
    zh = render_candidate_status(ledger, language="zh")
    assert "房源只租给护士、NHS 及医护人员，您不符合" in zh
    assert "房源原文说明卫生间是共用的" in zh and "房源原文：“AVAILABLE EXCLUSIVELY" in zh


def test_the_card_row_carries_the_conditions_for_the_panel():
    row = revalidate_view_results([_guilford()], CRITERIA)[0]
    assert row["qualification"]["status"] == "excluded"
    assert any(c["condition"] == "eligibility" and c["status"] == "not_met"
               for c in row["listing_conditions"])
    assert "房源只租给护士、NHS 及医护人员，您不符合" in row["qualification"]["reason_messages"]["zh"]


@pytest.mark.parametrize("lead", [
    "The best match is the Guilford Street studio at £1,127 a month.",
    "Guilford Street is the top pick: a verified, self-contained studio.",
    "最匹配的是 Guilford Street 的 studio，月租 £1,127。",
])
def test_no_best_match_or_verified_claim_survives_for_an_excluded_listing(lead):
    ledger = validate_candidates([_guilford(), _grays(19803200, 1300)], dict(CRITERIA, max_budget=1400))
    assert answer_lead_without_unverified_claims(lead, ledger) == ""


def test_a_sentence_saying_a_listing_is_not_verified_is_kept():
    ledger = validate_candidates([_guilford()], CRITERIA)
    lead = "Typical studios near UCL cost £1,900 to £2,400 a month."
    assert answer_lead_without_unverified_claims(lead, ledger) == lead


# ── 2. a stated budget change reaches the criteria, the cards and a new search ───────────

FIRST = ("我是UCL的学生，在Bloomsbury上课，2027年9月入住，想找studio或者en-suite，通勤不超过40分钟，"
         "预算每周£300包bills")


def _criteria_after(*messages):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    criteria = {}
    for message in messages:
        criteria = _apply_explicit_criteria_updates(dict(criteria), message)
    return criteria


def test_the_first_message_records_bills_and_the_student():
    criteria = _criteria_after(FIRST)
    assert criteria["max_budget"] == pytest.approx(1300)
    assert "bills included" in criteria["property_features"]
    assert criteria["tenant_status"] == "student"


@pytest.mark.parametrize("message", [
    "我的预算其实是严格的每周 £250，包 bills，请重新找",
    "Budget is strictly £250/week including bills. What's available?",
    "我把预算收紧到£250/周，严格不能超",
    "预算降至 £250/周，必须包 bills",
])
def test_a_stated_weekly_budget_replaces_the_old_ceiling(message):
    assert _criteria_after(FIRST, message)["max_budget"] == pytest.approx(250 * 52 / 12)


@pytest.mark.parametrize("message", [
    "Does the Guilford Street flat include bills?",
    "请告诉我这套房的 bills 情况",
])
def test_a_question_about_bills_still_changes_nothing(message):
    from core.constraint_updates import is_criteria_consultation
    assert is_criteria_consultation(message)


@pytest.mark.parametrize("message,expected", [
    ("I'm a UCL student starting in September", "student"),
    ("我在UCL读研", "postgraduate"),
    ("I'm a nurse at UCLH", "healthcare"),
    ("I'm not a student", "other"),
    ("Is it OK for a student?", None),
    ("my sister is a student", None),
])
def test_tenant_status_comes_only_from_the_users_own_words(message, expected):
    from core.constraint_updates import tenant_status_update
    assert tenant_status_update(message) == expected


@pytest.mark.parametrize("message,expected", [
    ("£300 a week including bills", True),
    ("bills included please", True),
    ("Does it include bills?", None),
    ("包bills吗", None),
    ("bills don't need to be included", False),
    ("不用包bills", False),
])
def test_bills_requirement(message, expected):
    from core.constraint_updates import bills_requirement_update
    assert bills_requirement_update(message) is expected


def test_cards_from_the_old_search_are_relabelled_at_the_new_budget():
    old = revalidate_view_results([_guilford(description="", available_from="")], dict(CRITERIA, max_budget=1300))
    assert old[0]["budget_status_code"] == "within"
    new = revalidate_view_results(old, dict(CRITERIA, max_budget=250 * 52 / 12))
    assert new[0]["budget_status_code"] == "over"
    assert new[0]["qualification"]["status"] == "excluded"


def test_the_state_node_notes_which_search_filters_changed():
    from core.agent_loop import _record_criteria_change

    def node(state):
        return {"accumulated_search_criteria": dict(state["accumulated_search_criteria"], max_budget=1083.33)}

    state = {"accumulated_search_criteria": {"max_budget": 1300, "tenant_status": "student"},
             "extracted_context": {"current_message": "budget now £250 a week",
                                   "hard_criteria_changed": ["area"]}}
    update = _record_criteria_change(node)(state)
    assert update["extracted_context"]["hard_criteria_changed"] == ["max_budget"]
    # A turn that changes nothing clears the previous turn's note.
    unchanged = _record_criteria_change(lambda s: {})(
        {"accumulated_search_criteria": {"max_budget": 1300},
         "extracted_context": {"hard_criteria_changed": ["max_budget"]}})
    assert unchanged["extracted_context"]["hard_criteria_changed"] == []


def _research_state(message, *, changed=("max_budget",), shown=True):
    from tests.test_agent_contract_guards import _state
    state = _state(message)
    state["accumulated_search_criteria"] = dict(CRITERIA, max_budget=250 * 52 / 12,
                                                criteria_gate_shown=True)
    state["extracted_context"]["hard_criteria_changed"] = list(changed)
    if shown:
        state["extracted_context"]["last_results"] = [{"address": "Guilford Street, London WC1N",
                                                       "price": "£1127/month"}]
    return state


def _search_provider():
    from tests.test_agent_contract_guards import Provider, Result, Spec
    payload = {"status": "found", "recommendations": [], "over_budget_alternatives": [],
               "search_criteria": {"max_budget": 1083.33, "areas": ["bloomsbury"]}}
    return Provider([Spec("search_properties")], {"search_properties": Result(True, payload)})


def test_a_budget_change_after_shown_results_searches_again_before_answering():
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import Chat, _drive
    provider = _search_provider()
    nodes = build_fc_nodes(provider, agent_llm=Chat(
        AIMessage(content="Your earlier results still apply."),
        AIMessage(content="Nothing in Bloomsbury fits £250 a week.")))
    state = _research_state("我的预算其实是严格的每周 £250，包 bills，请重新找")
    asyncio.run(_drive(nodes, state))
    assert [name for name, _ in provider.calls] == ["search_properties"]
    (_, params), = provider.calls
    assert params["max_budget"] == pytest.approx(250 * 52 / 12)
    assert state["final_response"] == "Nothing in Bloomsbury fits £250 a week."


@pytest.mark.parametrize("changed,shown", [((), True), (("max_budget",), False)])
def test_no_extra_search_without_a_change_or_without_shown_results(changed, shown):
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import Chat, _drive
    provider = _search_provider()
    nodes = build_fc_nodes(provider, agent_llm=Chat(AIMessage(content="Noted.")))
    asyncio.run(_drive(nodes, _research_state("budget £250 a week", changed=changed, shown=shown)))
    assert provider.calls == []


def test_the_search_tool_keeps_rows_whose_bills_are_unknown_and_drops_denied_ones():
    from core.tools.search_properties import _property_feature_evidence
    assert _property_feature_evidence({"Detailed_Amenities": "Bills Included"}, "bills included") == "verified"
    assert _property_feature_evidence({"_full_description": GUILFORD_PAGE}, "bills included") == "absent"
    assert _property_feature_evidence({"Description": "Studio"}, "bills included") == "unverified"


# ── 3. one address, several listings ─────────────────────────────────────────────────────

def test_units_on_one_street_carry_their_listing_numbers():
    ledger = validate_candidates([_grays(19803199, 1399), _grays(19803200, 1300)],
                                 dict(CRITERIA, max_budget=250 * 52 / 12))
    text = render_candidate_status(ledger)
    assert "Grays Inn Road, Islington WC1X (listing 19803199, £1399/month)" in text
    assert "Grays Inn Road, Islington WC1X (listing 19803200, £1300/month)" in text


def test_the_model_view_names_the_listing_and_its_conflicts():
    from core.agent_loop import project_search_payload
    from core.candidate_validation import validate_search_payload
    payload = validate_search_payload({"status": "found", "search_criteria": CRITERIA,
                                       "recommendations": [_guilford(), _grays(19803200, 1300)]})
    view = project_search_payload(payload, 12000)
    excluded = view["excluded_candidates"][0]
    assert excluded["listing_id"] == "20630206"
    assert any(line.startswith("eligibility: not_met") and "NURSES" in line
               for line in excluded["listing_conditions"])


# ── 4. asking about the listing reads the listing's own page ─────────────────────────────

def test_property_details_reads_the_detail_page_and_its_facts(monkeypatch, tmp_path):
    import pandas as pd

    from core.scraping import onthemarket
    from core.tools import get_property_details as details

    url = "https://www.onthemarket.com/details/20630206/"
    cache = onthemarket._DescCache(tmp_path / "desc.sqlite3")
    cache.set(url, GUILFORD_PAGE, "2027-01-07")
    monkeypatch.setattr(onthemarket, "_DESC_CACHE", cache)
    row = {"Address": "Guilford Street, London WC1N", "Price": "£1,127 pcm", "URL": url,
           "Description": "Studio to rent in Guilford Street, London WC1N",
           "Room_Type_Category": "Studio", "Detailed_Amenities": "Apartment, Studio Flat",
           "Available From": "Contact agent"}
    monkeypatch.setattr(details, "load_property_database", lambda: pd.DataFrame([row]))
    monkeypatch.setattr("core.scraping.on_demand.find_cached_listing_by_url",
                        lambda token: dict(row) if token.rstrip("/") == url.rstrip("/") else None)
    result = details.get_property_details_impl(property_url=url)
    assert result["found"] is True
    assert result["property"]["description"].startswith("AVAILABLE EXCLUSIVELY TO NURSES")
    facts = result["listing_text_facts"]
    assert facts["let_only_to"][0]["listing_says"].startswith("AVAILABLE EXCLUSIVELY")
    assert "shared kitchen and bathroom" in facts["shared_bathroom"]
    assert facts["bills_included"] == "no"
    assert result["room_type_analysis"]["self_contained"] is False
    assert result["description_source"] == "listing detail page (cached copy)"
