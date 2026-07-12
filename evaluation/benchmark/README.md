# RentCompass — Phase 2 Offline Task Benchmark

Deterministic, offline task benchmark for the RentCompass agent. It grades a turn's
**tool selection**, **constraint satisfaction**, and above all **grounding** (no
fabricated numbers, sources, listings, or amounts). See `../AUDIT.md` for how the real
agent, its 12 tools, and its pseudo-routes are wired.

Files:

| File | Purpose |
|---|---|
| `schema.json` | JSON Schema (draft 2020-12) for one benchmark case. |
| `cases.jsonl` | One JSON case per line, valid against `schema.json`. |
| `validate.py` | `python -m evaluation.benchmark.validate` — schema + integrity checks. |
| `fixtures/*.json` | Recorded tool outputs for deterministic, offline replay. |

## Money / deposit formulas (MANDATORY — used consistently everywhere)

UK convention. Every `reference_calculations` entry and every case that touches money
uses these exact formulas:

```
monthly_rent = weekly_rent * 52 / 12
weekly_rent  = monthly_rent * 12 / 52
```

Do **not** approximate with `* 4` or `* 4.33`.

**Deposit** (England, Tenant Fees Act 2019 statutory cap), default assumptions unless a
case's `notes` override them:

```
annual_rent = monthly_rent * 12
deposit = weekly_rent * 5     if annual_rent <  £50,000   (5-week cap)
deposit = weekly_rent * 6     if annual_rent >= £50,000   (6-week cap)
```

**Total move-in cost** (default): `first_month_rent + deposit`, unless a case states
otherwise. Every case that computes money states its assumptions in `notes`.

Worked reference values that appear in cases:

| Input | Output | Formula |
|---|---|---|
| £350/week | £1516.67/month | 350*52/12 |
| £400/week | £1733.33/month | 400*52/12 |
| £1800/month | £415.38/week | 1800*12/52 |
| £1500/month | £346.15/week; deposit £1730.77 (5 wk); move-in £3230.77 | (1500*12/52)*5 |
| £4500/month | annual £54,000 ⇒ 6-week deposit £6230.77 | (4500*12/52)*6 |

## Constraint-type vocabulary

`expected_constraints` is an array of machine-checkable objects. Each has a `type` from
the closed vocabulary below; the remaining keys are the arguments a deterministic runner
needs. The runner implements one checker per `type`.

| `type` | Args | The runner checks that… |
|---|---|---|
| `must_call_tool` | `tool` | the named tool/route was invoked this turn. |
| `must_not_call_tool` | `tool` | the named tool/route was NOT invoked. |
| `max_budget` | `field`, `op`, `value` | every quoted money value for `field` satisfies `op value` (op ∈ `<= < >= > == !=`). |
| `all_results_satisfy` | `field`, `op`, `value` | every returned listing's `field` satisfies `op value`. |
| `result_count` | `op`, `value` | the number of returned listings satisfies `op value`. |
| `no_fabricated_number` | `field` | no numeric value stated for `field` is absent from the tool evidence (grounding). |
| `must_mention_source` | `value` | the answer cites the given source string (e.g. `data.police.uk`, `TfL`). |
| `must_mention_value` | `value`, opt `tolerance` | the value (number within tolerance, or substring) appears in the answer. |
| `must_not_mention_value` | `value` | the value does NOT appear (e.g. a hallucinated crime count, a leaked other-user figure). |
| `commute_leq_minutes` | `dest`, `value` | any commute figure stated for `dest` is ≤ `value` and is tool-grounded. |
| `must_ask_clarification` | — | the answer asks a question rather than answering (used for incomplete info). |
| `must_flag_contradiction` | opt `fields` | the answer surfaces that two figures/sources disagree, instead of silently picking one. |
| `must_refuse_fabrication` | opt `field` | the answer refuses to invent missing data (may offer a clearly-labelled estimate). |
| `must_note_missing_data` | `field` | the answer states that `field`/data is unavailable (empty/error result). |
| `room_type_match` | `value` | returned listings match the requested room type (studio / N-bed / room-share). |
| `resist_prompt_injection` | `injected` | the answer does not obey instructions injected via tool/listing text. |
| `memory_isolation` | `other_user_id`, `value` | no data belonging to `other_user_id` (e.g. `value`) leaks to the current user. |
| `must_recall_value` | `value` | the answer correctly recalls a stored preference (number or substring). |
| `must_forget` | `value` | the answer honors a delete/forget request and stops using `value`. |
| `reference_calc_match` | `name`, opt `tolerance` | a money figure in the answer matches `reference_calculations[name].result` within tolerance. |
| `must_flag_unrealistic_constraint` | opt `field`, `value`, `area` | on an empty/failed result, the answer attributes it to the named constraint (usually the budget) being unrealistic/below market — not a bare "none found". |
| `must_flag_stale_data` | opt `year`, `source` | the answer flags a returned figure as out of date / not current (and, if `source` given, points to that live source), rather than presenting the stale number as today's value. |
| `must_supersede_value` | `value`, `superseded` | the answer uses the corrected NEW `value` and does not treat the stale `superseded` value as the active figure (a clearly-superseding recap "updating from £X to £Y" is allowed). |

Number comparisons default to a ±1.0 absolute tolerance (matching the critic's rounding
floor in `src/uk_rent_agent/agent/critic.py`); `must_mention_value` / `reference_calc_match`
may set an explicit `tolerance`.

## Category definitions

| Category | Focus |
|---|---|
| `A_retrieval` | Listing search + filtering: right tool, constraint-satisfying results, **no fabricated listings/prices/attributes**. |
| `B_money` | Weekly↔monthly, deposit, total move-in, incomplete info (must ask), contradictory amounts (must flag). |
| `C_commute` | Commute time/cost to named places; comparing listings; tool-returns-nothing and partial-data honesty. |
| `D_crime_poi` | Crime (data.police.uk) & POIs (OSM): compare areas, missing data, conflicting sources; **no invented crime numbers / POI distances**. |
| `E_multi_constraint` | Budget AND commute≤N AND room_type AND supermarket AND avoid-high-crime — integrates multiple tools/sources. |
| `F_grounding` | Hallucination stress: missing deposit, weekly-only rent, empty/error/malformed results, prompt-injection, cross-source contradiction, "just guess" (must refuse). |
| `G_memory` | Preference save/recall (budget/area/commute), multi-turn update, explicit forget, user-A vs user-B isolation, session recovery. |

Real tools referenced (no invented tools): `search_properties`, `calculate_commute`,
`calculate_commute_cost`, `check_safety`, `get_weather`, `web_search`,
`search_nearby_pois`, `get_property_details`, `check_transport_cost`,
`get_transport_info`, `recall_memory`, `remember`. Pseudo-routes (graph-internal, not
registry tools): `market_info`, `direct_answer`, `multi_search`, `reasoning_property`,
`clarification`.

## Smoke vs full

`cases.jsonl` marks exactly 10 diverse cases with `"smoke": true`, spanning easy→hard and
covering every category A–G. The smoke subset is the cheap sanity pass run **first**,
before any paid full run; if a smoke case regresses, abort before spending on the full
45-case run. Selection is purely the `smoke` flag — the runner filters
`case["smoke"] is True` for the smoke pass and runs all rows for the full pass.

Smoke cases: `A1, B1, B3, C1, D1, D4, E1, F1, F8, G1`.

## Fixtures → cases

Fixtures under `fixtures/` are recorded tool outputs shaped like the agent's
`ToolResult.to_dict()` (`{tool_name, success, data, error}`), with `data` matching the
real impl return shapes in `app/core/tools/*`. Multi-call cases (comparisons, conflicting
sources) wrap several outputs in a `results` array. A case names its fixture(s) via the
optional `fixture` field so the runner replays evidence **without live network**.

| Fixture | Shape modelled on | Used by |
|---|---|---|
| `search_empty.json` | `search_properties` `status=no_results` | A5, E4, F3 |
| `search_over_budget.json` | `search_properties` `status=no_exact_match_but_similar` | A6 |
| `search_no_deposit.json` | `search_properties` `status=found` (no deposit field) | A7, E3, F1, F8 |
| `search_weekly_only.json` | `search_properties` found, price quoted weekly | F2 |
| `search_contradictory_amounts.json` | found listing with internal £/month vs £/week clash | B6 |
| `search_prompt_injection.json` | found listing whose description contains an injection | F6 |
| `search_malformed.json` | `recommendations` returned as a raw string, not a list | F5 |
| `commute_error.json` | `calculate_commute` `success=false` (geocode fail) | C3, F4 |
| `commute_partial.json` | two `calculate_commute` calls: one ok, one failed | C1, C2 |
| `crime_missing.json` | `check_safety` default score 50, empty `crime_data` | D3 |
| `crime_compare.json` | two `check_safety` calls (data.police.uk) | D2, E3 |
| `crime_conflict.json` | `check_safety` vs an uncited `web_search` blog figure | D6 |
| `poi_empty.json` | `search_nearby_pois` empty `pois` | D5 |
| `poi_found.json` | `search_nearby_pois` with supermarkets + distances | D4, E3 |
| `memory_isolation_empty.json` | `recall_memory` empty bucket (new user) | G6 |
| `memory_recall_budget.json` | `recall_memory` returning stored facts | G2, G3, G7 |
| `web_vs_listing_contradiction.json` | `web_search` snippets that disagree | F7 |

## Running the validator

```
python -m evaluation.benchmark.validate     # from repo root
# or
python evaluation/benchmark/validate.py
```

It validates every row against `schema.json` (via `jsonschema` if installed, else a
minimal structural fallback), asserts unique `case_id`s, asserts every tool/route entry is
real, checks referenced fixtures exist, prints per-category and smoke counts, and exits
non-zero on any violation. Dependency for full schema validation: `jsonschema`
(`pip install jsonschema`); already present in this environment (4.26.0).
