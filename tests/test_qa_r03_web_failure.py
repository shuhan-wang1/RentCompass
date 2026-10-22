"""R3-04: an honest web-failure draft survives the no-evidence money contract.

r04 rid 92c2254f: both web_search calls failed ("SearXNG search service is unavailable").
The draft said so, then gave the general rules and quoted £50 and £50,000 with no evidence.
``enforce_no_evidence_numeric_contract`` replaced the WHOLE answer with "Sorry, I don't have
reliable data…". Now the unsupported figures go, the failure statement stays, and the
answer names the lookup that failed.
"""
from __future__ import annotations

import json
import pathlib

from uk_rent_agent.agent.critic import enforce_no_evidence_numeric_contract, no_reliable_data_message

CASE = json.loads((pathlib.Path(__file__).parent / "data" /
                   "qa_r03_web_failure_draft.json").read_text(encoding="utf-8"))


def _evidence():
    return [a["raw_data"] for a in CASE["tool_artifacts"]]


def test_failure_statement_survives_and_unsupported_figures_go():
    out = enforce_no_evidence_numeric_contract(
        CASE["draft"], retrieved_evidence=_evidence(), user_message=CASE["message"],
        reply_language="en", failed_lookups=["web search"])
    assert out != no_reliable_data_message("en")
    assert out.startswith("I tried to search the web twice, but the search service is currently unavailable")
    assert "£50" not in out
    assert "Tenant Fees Act 2019" in out and "Deposit rules" in out


def test_failed_lookup_is_named_when_the_answer_does_not_say_so():
    draft = ("Typical letting rules: the tenancy deposit is capped at five weeks' rent. Holding "
             "deposits are capped at one week's rent and must be refunded within 15 days unless "
             "a permitted reason applies. Admin fees are banned. A variation fee is capped at £50.")
    out = enforce_no_evidence_numeric_contract(
        draft, retrieved_evidence=_evidence(), user_message="What are the rules?",
        reply_language="en", failed_lookups=["web search"])
    assert "£50" not in out
    assert "The web search lookup failed this turn" in out


def test_answer_that_is_only_figures_still_gets_the_template_and_the_failure():
    out = enforce_no_evidence_numeric_contract(
        "Typical local rent is £1,234/month.", retrieved_evidence={"success": True, "results": []},
        user_message="What is the usual rent here?", reply_language="en",
        failed_lookups=["web search"])
    assert out.startswith(no_reliable_data_message("en"))
    assert "web search lookup failed" in out and "£1,234" not in out


def test_without_failed_lookups_the_short_answer_contract_is_unchanged():
    assert enforce_no_evidence_numeric_contract(
        "Typical local rent is £1,234/month.", retrieved_evidence={"success": True, "results": []},
        user_message="What is the usual rent here?", reply_language="en") == no_reliable_data_message("en")
