"""r6d: the critic's price pool derives only the rent arithmetic that occurs.

The old pool accepted any evidence price x 1-36, so an invented figure passed as a multiple of
any small evidence amount ("£2,400" = 200 x 12, "£300" = a £15 fee x 20). It now derives the
weekly/monthly conversions, a year, the 5- or 6-week deposit, the 1-week holding deposit, N
months of rent where the answer states N, sums / differences / the mean of two evidence
figures, a difference over a year or the stated months, a written-out x / ÷ step by a count the
answer states, and for a fare or daily cap x 2 a day and x the stated commuting days.

Measured on every recorded draft and final answer of rounds r06-r20 plus the test corpora (1,413
texts): 1 newly flagged text (a draft cut off mid-figure, "Poplar: £17"), 8 correct ones no
longer flagged, 0 correct figures newly flagged.
"""
import pytest

from uk_rent_agent.agent.critic import _derivations, unsupported_reply_prices


@pytest.mark.parametrize("reply,evidence", [
    ("The rent is £2,400 a month.", {"budget_status": "Over budget by £200"}),       # 200 x 12
    ("Expect to pay £300 up front.", {"admin_fee": "£15", "price": "£1,800/month"}),  # 15 x 20
    ("That would come to £9,000 here.", {"price": "£1,500/month"}),                 # no "6 months"
    ("The deposit is £5,000.", {"price": "£1,500/month"}),                           # not 5/6 weeks
])
def test_an_invented_multiple_is_flagged(reply, evidence):
    assert unsupported_reply_prices(reply, [evidence, "find me a flat"])


@pytest.mark.parametrize("reply", [
    "At £1,500 a month that is £18,000 a year.",                                     # x 12
    "That works out at about £346 a week.",                                          # x 12/52
    "The deposit is capped at 5 weeks' rent, £1,730.77.",                            # 5 weeks
    "A holding deposit of one week's rent is £346.15.",                              # 1 week
    "Over 6 months that is £9,000 in rent.",                                         # 6 stated
    "Against the £1,350 listing it costs £150 more a month, £1,800 over 12 months.",  # gap x 12
])
def test_the_rent_arithmetic_that_occurs_is_kept(reply):
    assert not unsupported_reply_prices(reply, [{"price": "£1,500/month", "other": {"price": "£1,350/month"}},
                                                "find me a flat"]), reply


def test_fare_arithmetic_over_the_stated_days():
    evidence = [{"fare": "£1.75", "fare_peak": "£4.95"}, "how much would I spend a month?"]
    reply = ("The cheapest route at £1.75 each way is £3.50 a day. Using the 22 commuting days per "
             "month, that is 22 x £9.90 = £217.80 on the fastest route, or 22 x £3.50 = £77.00.")
    assert not unsupported_reply_prices(reply, evidence)
    assert unsupported_reply_prices(reply.replace("£217.80", "£250.80"), evidence)


def test_a_year_is_derived_only_where_the_answer_speaks_of_one():
    assert 18200.0 in _derivations(350.0, "weekly")
    assert 2400.0 in _derivations(200.0, "monthly")
    assert 2400.0 not in _derivations(200.0, "unknown", year=False)
