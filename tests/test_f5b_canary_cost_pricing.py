"""The shipped price table must produce a number, and the right one.

Three separate defects met here:

* the only price table in the repo was a placeholder (`unverified: true`, null
  rates for the model production actually runs), so `canary_cost` returned
  `total_cost: null` on every real window it had ever been pointed at;
* one unpriced model erased the cost of every priced call beside it;
* and the arithmetic double-charged cached tokens — latent only because no
  verified table had ever shipped to expose it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_V2 = _ROOT / "scripts" / "pricing" / "deepseek_prices_v2.json"


def _load():
    spec = importlib.util.spec_from_file_location(
        "canary_cost_f5b", _ROOT / "scripts" / "canary_cost.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["canary_cost_f5b"] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load()
PRICES_V2 = json.loads(_V2.read_text(encoding="utf-8"))


def _usage(model="deepseek-v4-flash", *, calls=1, input_tokens=1000,
           output_tokens=500, cache_read_tokens=200):
    metrics = {"calls": calls, "input_tokens": input_tokens,
               "output_tokens": output_tokens,
               "cache_read_tokens": cache_read_tokens}
    return {**metrics, "models": {model: dict(metrics)}}


def _record(usage, arch="fc_loop"):
    return {"event": "canary.turn", "agent_arch": arch,
            "llm_usage_status": "complete", "llm_usage": usage}


# ---- the shipped table ----------------------------------------------------- #

def test_shipped_v2_table_is_verified_and_carries_provenance():
    assert PRICES_V2["unverified"] is False
    assert PRICES_V2["unit"] == "per_1m_tokens"
    assert PRICES_V2["currency"] == "USD"
    assert PRICES_V2["source_url"].startswith("https://api-docs.deepseek.com/")
    assert PRICES_V2["retrieved_at"]
    flash = PRICES_V2["models"]["deepseek-v4-flash"]
    assert flash["verified"] is True
    assert flash["source_url"] and flash["retrieved_at"]
    # The shipped rate is the PEAK rate: a cost gate may overstate, never understate.
    assert flash["input"] == flash["peak"]["input"] > flash["off_peak"]["input"]
    assert flash["cache_read"] < flash["input"] < flash["output"]


def test_v1_placeholder_table_is_left_untouched():
    """A published price table is never edited in place; historical runs must repro."""
    v1 = json.loads((_ROOT / "scripts" / "pricing" / "deepseek_prices_v1.json")
                    .read_text(encoding="utf-8"))
    assert v1["price_table_version"] == 1
    assert v1["unverified"] is True
    assert v1["models"]["deepseek-v4-flash"]["input"] is None


def test_the_production_model_now_yields_a_non_null_total_cost():
    report = cc.build_cost_report([_record(_usage())], PRICES_V2)

    assert report["ok"] is True
    assert report["decision"] == "PROCEED"
    assert report["total_cost"] is not None
    assert report["total_cost"] > 0


# ---- the arithmetic -------------------------------------------------------- #

def test_cache_reads_are_a_breakdown_of_input_not_an_extra_bucket():
    """1000 input of which 200 are cache hits: 800 charged at miss, 200 at hit."""
    flash = PRICES_V2["models"]["deepseek-v4-flash"]
    expected = (800 * flash["input"] + 500 * flash["output"]
                + 200 * flash["cache_read"]) / 1_000_000.0
    double_counted = (1000 * flash["input"] + 500 * flash["output"]
                      + 200 * flash["cache_read"]) / 1_000_000.0

    result = cc.compute_cost(cc.sum_usage([_record(_usage())]), PRICES_V2)

    assert result["total_cost"] == pytest.approx(round(expected, 6))
    assert result["total_cost"] < double_counted
    assert result["by_model"][0]["cache_miss_tokens"] == 800


def test_cache_read_greater_than_input_is_flagged_and_never_charged_negative():
    usage = _usage(input_tokens=100, cache_read_tokens=400)

    result = cc.compute_cost(cc.sum_usage([_record(usage)]), PRICES_V2)

    assert result["priced_cost"] >= 0
    assert any("internally inconsistent" in issue for issue in result["issues"])
    assert result["ok"] is False


# ---- partial pricing ------------------------------------------------------- #

def test_an_unpriced_model_no_longer_erases_the_priced_ones():
    records = [_record(_usage()),
               _record(_usage(model="deepseek-chat"), arch="legacy")]

    result = cc.compute_cost(cc.sum_usage(records), PRICES_V2)

    # Fail-closed gate is unchanged...
    assert result["ok"] is False
    assert result["decision"] == "HOLD"
    assert result["total_cost"] is None
    # ...but the arithmetic that COULD be done is stated, with its incompleteness.
    assert result["priced_cost"] > 0
    assert result["unpriced_models"] == ["deepseek-chat"]
    assert result["unpriced_usage"]["calls"] == 1
    assert result["unpriced_call_share"] == pytest.approx(0.5)


def test_a_per_model_verified_false_marker_beats_a_present_rate():
    prices = json.loads(json.dumps(PRICES_V2))
    prices["models"]["deepseek-v4-flash"]["verified"] = False

    result = cc.compute_cost(cc.sum_usage([_record(_usage())]), prices)

    assert result["unpriced_models"] == ["deepseek-v4-flash"]
    assert result["total_cost"] is None
    assert result["priced_cost"] == 0.0
