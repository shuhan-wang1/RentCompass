# held-out v2 — AUTHOR AUDIT (all 110 cases)

> **This is an author audit, not a human review.** Every row below was checked by the model that authored the set. No person has labelled any case. Nothing in this file may be reported as human review, human evaluation or inter-rater agreement.

- cases audited: **110**
- PASS: **110**   REPLACE: **0**
- source: `evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl`

Four columns, per the preflight's manual half:

1. **Hard constraints explicit, verifiable and non-contradictory** — backed by re-normalising each constraint's verbatim `user_text` span (H6), by the frozen evidence containing both a satisfying and a violating record (the violation trap), and by the per-slot contradiction check (H4).
2. **Correct completion written** — a non-empty `correct_completion` that names the no-result / unknown branch where the case has one.
3. **Every judgeable claim has an evidence source** — `allowed_evidence_sources` non-empty; a calculation case carries `reference_calculations`; a case that declares tools has a frozen fixture.
4. **Not a duplicate of the 98 tuning cases** — `novelty_note` present; zero verbatim query overlap measured against `evaluation/benchmark/cases.jsonl`.

| # | case_id | stratum | 1 constraints | 2 completion | 3 evidence | 4 novelty | verdict |
|---|---|---|---|---|---|---|---|
| 1 | `HO2-001` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 2 | `HO2-002` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 3 | `HO2-003` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 4 | `HO2-004` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 5 | `HO2-005` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap move_in_date:unknown | yes | 2 source(s), fixture | yes | **PASS** |
| 6 | `HO2-006` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 7 | `HO2-007` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 8 | `HO2-008` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 9 | `HO2-009` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 10 | `HO2-010` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap move_in_date:unknown | yes | 2 source(s), fixture | yes | **PASS** |
| 11 | `HO2-011` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 12 | `HO2-012` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 13 | `HO2-013` | retrieval_hard | bedroom_count:trap room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 14 | `HO2-014` | retrieval_hard | room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 15 | `HO2-015` | retrieval_hard | room_type:trap area:trap budget:trap move_in_date:unknown | yes | 2 source(s), fixture | yes | **PASS** |
| 16 | `HO2-016` | retrieval_hard | room_type:trap area:trap budget:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 17 | `HO2-017` | retrieval_hard | room_type:trap area:trap commute:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 18 | `HO2-018` | retrieval_hard | room_type:trap area:trap commute:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 19 | `HO2-019` | retrieval_hard | room_type:trap area:trap commute:trap move_in_date:unknown | yes | 3 source(s), fixture | yes | **PASS** |
| 20 | `HO2-020` | retrieval_hard | room_type:trap area:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 21 | `HO2-021` | retrieval_hard | room_type:trap area:trap | yes | 2 source(s), fixture | yes | **PASS** |
| 22 | `HO2-022` | retrieval_hard | room_type:trap area:trap move_in_date:unknown | yes | 2 source(s), fixture | yes | **PASS** |
| 23 | `HO2-023` | retrieval_hard | area:trap commute:trap move_in_date:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 24 | `HO2-024` | retrieval_hard | area:trap commute:trap move_in_date:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 25 | `HO2-025` | retrieval_hard | area:trap commute:trap move_in_date:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 26 | `HO2-026` | retrieval_hard | area:trap commute:trap move_in_date:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 27 | `HO2-027` | retrieval_hard | area:trap commute:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 28 | `HO2-028` | retrieval_hard | area:trap commute:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 29 | `HO2-029` | retrieval_hard | area:trap commute:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 30 | `HO2-030` | retrieval_hard | area:trap commute:trap move_in_date:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 31 | `HO2-031` | retrieval_hard | area:trap commute:trap move_in_date:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 32 | `HO2-032` | retrieval_hard | area:trap commute:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 33 | `HO2-033` | retrieval_hard | area:trap commute:trap move_in_date:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 34 | `HO2-034` | retrieval_hard | area:trap commute:trap move_in_date:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 35 | `HO2-035` | retrieval_hard | area:trap commute:trap move_in_date:trap property_feature:trap | yes | 3 source(s), fixture | yes | **PASS** |
| 36 | `HO2-036` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 37 | `HO2-037` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 38 | `HO2-038` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 39 | `HO2-039` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 40 | `HO2-040` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 41 | `HO2-041` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 42 | `HO2-042` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 43 | `HO2-043` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 44 | `HO2-044` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 45 | `HO2-045` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 46 | `HO2-046` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 47 | `HO2-047` | retrieval_soft | area:trivial | yes | 2 source(s), fixture | yes | **PASS** |
| 48 | `HO2-048` | retrieval_soft | budget:no_result area:no_result bedroom_count:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 49 | `HO2-049` | retrieval_soft | budget:no_result area:no_result property_feature:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 50 | `HO2-050` | retrieval_soft | budget:no_result area:no_result bedroom_count:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 51 | `HO2-051` | retrieval_soft | budget:no_result area:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 52 | `HO2-052` | retrieval_soft | budget:no_result area:no_result bedroom_count:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 53 | `HO2-053` | retrieval_soft | budget:no_result area:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 54 | `HO2-054` | retrieval_soft | budget:no_result area:no_result bedroom_count:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 55 | `HO2-055` | retrieval_soft | budget:no_result area:no_result room_type:no_result | yes | 2 source(s), fixture | yes | **PASS** |
| 56 | `HO2-056` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 57 | `HO2-057` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 58 | `HO2-058` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 59 | `HO2-059` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 60 | `HO2-060` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 61 | `HO2-061` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 62 | `HO2-062` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 63 | `HO2-063` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 64 | `HO2-064` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 65 | `HO2-065` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 66 | `HO2-066` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 67 | `HO2-067` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 68 | `HO2-068` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 69 | `HO2-069` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 70 | `HO2-070` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 71 | `HO2-071` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 72 | `HO2-072` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 73 | `HO2-073` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 74 | `HO2-074` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 75 | `HO2-075` | calculation | n/a (no hard constraint) | yes | 2 source(s), ref-calc | yes | **PASS** |
| 76 | `HO2-076` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 77 | `HO2-077` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 78 | `HO2-078` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 79 | `HO2-079` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 80 | `HO2-080` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 81 | `HO2-081` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 82 | `HO2-082` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 83 | `HO2-083` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 84 | `HO2-084` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 85 | `HO2-085` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 86 | `HO2-086` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 87 | `HO2-087` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 88 | `HO2-088` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 89 | `HO2-089` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 90 | `HO2-090` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 91 | `HO2-091` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 92 | `HO2-092` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 93 | `HO2-093` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 94 | `HO2-094` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 95 | `HO2-095` | memory | n/a (no hard constraint) | yes | 2 source(s), fixture | yes | **PASS** |
| 96 | `HO2-096` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 97 | `HO2-097` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 98 | `HO2-098` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 99 | `HO2-099` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 100 | `HO2-100` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 101 | `HO2-101` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 102 | `HO2-102` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 103 | `HO2-103` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 104 | `HO2-104` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 105 | `HO2-105` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 106 | `HO2-106` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 107 | `HO2-107` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 108 | `HO2-108` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 109 | `HO2-109` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |
| 110 | `HO2-110` | clarify | n/a (no hard constraint) | yes | 1 source(s) | yes | **PASS** |

---

## Per-case evidence

### HO2-001  (retrieval_hard)

- request: `Can you find me a 2-bed flat in Walthamstow under £1,700 a month. Please leave out anything that does not meet every one of those.`
- frozen listings: 5   fixture: `ho2_001_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '2-bed', re-normalises to `{'op': '==', 'value': 2}`; satisfying ['fernbrook row', 'halstow row', 'quillon row', 'ashlin row']; violating ['wraysbury row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['fernbrook row', 'halstow row', 'wraysbury row', 'ashlin row']; violating ['quillon row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Walthamstow', re-normalises to `{'granularity': 'borough', 'value': 'Walthamstow'}`; satisfying ['fernbrook row', 'halstow row', 'wraysbury row', 'quillon row']; violating ['ashlin row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,700 a month', re-normalises to `{'value': 1700.0}`; satisfying ['fernbrook row', 'wraysbury row']; violating ['halstow row', 'quillon row', 'ashlin row']

### HO2-002  (retrieval_hard)

- request: `I need a 1-bed flat in Leyton under £1,450 a month. Do not include options that break any of those conditions.`
- frozen listings: 5   fixture: `ho2_002_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '1-bed', re-normalises to `{'op': '==', 'value': 1}`; satisfying ['marchcroft row', 'denbury row', 'pentworth row', 'ravensmere row']; violating ['osterlyn row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['marchcroft row', 'denbury row', 'osterlyn row', 'ravensmere row']; violating ['pentworth row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Leyton', re-normalises to `{'granularity': 'borough', 'value': 'Leyton'}`; satisfying ['marchcroft row', 'denbury row', 'osterlyn row', 'pentworth row']; violating ['ravensmere row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,450 a month', re-normalises to `{'value': 1450.0}`; satisfying ['marchcroft row', 'osterlyn row']; violating ['denbury row', 'pentworth row', 'ravensmere row']

### HO2-003  (retrieval_hard)

- request: `Help me find a 3-bed house in Peckham under £2,100 a month. If an option misses any of that, leave it out of your answer.`
- frozen listings: 5   fixture: `ho2_003_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '3-bed', re-normalises to `{'op': '==', 'value': 3}`; satisfying ['sowerby row', 'thackray row', 'vandermeer row', 'wexcombe row']; violating ['ulverton row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying ['sowerby row', 'thackray row', 'ulverton row', 'wexcombe row']; violating ['vandermeer row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Peckham', re-normalises to `{'granularity': 'borough', 'value': 'Peckham'}`; satisfying ['sowerby row', 'thackray row', 'ulverton row', 'vandermeer row']; violating ['wexcombe row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £2,100 a month', re-normalises to `{'value': 2100.0}`; satisfying ['sowerby row', 'ulverton row']; violating ['thackray row', 'vandermeer row', 'wexcombe row']

### HO2-004  (retrieval_hard)

- request: `I'm after a flat with at least 2 bedrooms in Tooting under £1,850 a month. I only want the ones that tick every box — drop the rest.`
- frozen listings: 5   fixture: `ho2_004_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said 'at least 2 bedrooms', re-normalises to `{'op': '>=', 'value': 2}`; satisfying ['yarnfield row', 'zephyrn row', 'calthorpe row', 'drayfield row']; violating ['brackendale row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['yarnfield row', 'zephyrn row', 'brackendale row', 'drayfield row']; violating ['calthorpe row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Tooting', re-normalises to `{'granularity': 'borough', 'value': 'Tooting'}`; satisfying ['yarnfield row', 'zephyrn row', 'brackendale row', 'calthorpe row']; violating ['drayfield row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,850 a month', re-normalises to `{'value': 1850.0}`; satisfying ['yarnfield row', 'brackendale row']; violating ['zephyrn row', 'calthorpe row', 'drayfield row']

### HO2-005  (retrieval_hard)

- request: `Looking for a 1-bed flat in Catford under £1,300 a month, ready to move into by 1 October. Skip anything that fails one of those conditions, please.`
- frozen listings: 5   fixture: `ho2_005_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '1-bed', re-normalises to `{'op': '==', 'value': 1}`; satisfying ['ellesbury row', 'foxhollow row', 'hazelbourne row', 'inglewhite row']; violating ['garrowby row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['ellesbury row', 'foxhollow row', 'garrowby row', 'inglewhite row']; violating ['hazelbourne row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Catford', re-normalises to `{'granularity': 'borough', 'value': 'Catford'}`; satisfying ['ellesbury row', 'foxhollow row', 'garrowby row', 'hazelbourne row']; violating ['inglewhite row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,300 a month', re-normalises to `{'value': 1300.0}`; satisfying ['ellesbury row', 'garrowby row']; violating ['foxhollow row', 'hazelbourne row', 'inglewhite row']
- **move_in_date** (`move_in_date_satisfied`, branch `unknown`) — user said '1 October', re-normalises to `{'value': '2026-10-01'}`; satisfying []; violating []; unknown ['ellesbury row', 'foxhollow row', 'garrowby row', 'hazelbourne row', 'inglewhite row']

### HO2-006  (retrieval_hard)

- request: `Trying to find a flat with 2 to 3 bedrooms in Acton under £1,950 a month. Just the ones that satisfy all of it; ignore the others.`
- frozen listings: 5   fixture: `ho2_006_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '2 to 3 bedrooms', re-normalises to `{'op': 'between', 'value': [2, 3]}`; satisfying ['kelsterne row', 'lambourne row', 'netherby row', 'ockendale row']; violating ['mortlake row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['kelsterne row', 'lambourne row', 'mortlake row', 'ockendale row']; violating ['netherby row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Acton', re-normalises to `{'granularity': 'borough', 'value': 'Acton'}`; satisfying ['kelsterne row', 'lambourne row', 'mortlake row', 'netherby row']; violating ['ockendale row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,950 a month', re-normalises to `{'value': 1950.0}`; satisfying ['kelsterne row', 'mortlake row']; violating ['lambourne row', 'netherby row', 'ockendale row']

### HO2-007  (retrieval_hard)

- request: `Please search for a 2-bed flat in Crouch End under £2,250 a month. Anything that does not meet all of those should not be in your list.`
- frozen listings: 5   fixture: `ho2_007_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '2-bed', re-normalises to `{'op': '==', 'value': 2}`; satisfying ['pyrford row', 'quenby row', 'stanbrook row', 'tarleton row']; violating ['rushmoor row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['pyrford row', 'quenby row', 'rushmoor row', 'tarleton row']; violating ['stanbrook row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Crouch End', re-normalises to `{'granularity': 'borough', 'value': 'Crouch End'}`; satisfying ['pyrford row', 'quenby row', 'rushmoor row', 'stanbrook row']; violating ['tarleton row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £2,250 a month', re-normalises to `{'value': 2250.0}`; satisfying ['pyrford row', 'rushmoor row']; violating ['quenby row', 'stanbrook row', 'tarleton row']

### HO2-008  (retrieval_hard)

- request: `I'd like a 2-bed flat in Bermondsey under £2,400 a month. Only show me ones that actually fit — skip anything that misses any of that.`
- frozen listings: 5   fixture: `ho2_008_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '2-bed', re-normalises to `{'op': '==', 'value': 2}`; satisfying ['uffington row', 'verwood row', 'xanthe row', 'yealand row']; violating ['whitmarsh row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['uffington row', 'verwood row', 'whitmarsh row', 'yealand row']; violating ['xanthe row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Bermondsey', re-normalises to `{'granularity': 'borough', 'value': 'Bermondsey'}`; satisfying ['uffington row', 'verwood row', 'whitmarsh row', 'xanthe row']; violating ['yealand row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £2,400 a month', re-normalises to `{'value': 2400.0}`; satisfying ['uffington row', 'whitmarsh row']; violating ['verwood row', 'xanthe row', 'yealand row']

### HO2-009  (retrieval_hard)

- request: `Could you dig out a 1-bed flat in Wood Green under £1,550 a month. Please leave out anything that does not meet every one of those.`
- frozen listings: 5   fixture: `ho2_009_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '1-bed', re-normalises to `{'op': '==', 'value': 1}`; satisfying ['zennorby row', 'ambrose row', 'carbery row', 'dunsfold row']; violating ['beaumaris row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['zennorby row', 'ambrose row', 'beaumaris row', 'dunsfold row']; violating ['carbery row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Wood Green', re-normalises to `{'granularity': 'borough', 'value': 'Wood Green'}`; satisfying ['zennorby row', 'ambrose row', 'beaumaris row', 'carbery row']; violating ['dunsfold row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,550 a month', re-normalises to `{'value': 1550.0}`; satisfying ['zennorby row', 'beaumaris row']; violating ['ambrose row', 'carbery row', 'dunsfold row']

### HO2-010  (retrieval_hard)

- request: `Hi — I'm looking for a flat with no more than 2 bedrooms in Hendon under £1,750 a month, ready to move into by 1 November. Do not include options that break any of those conditions.`
- frozen listings: 5   fixture: `ho2_010_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said 'no more than 2 bedrooms', re-normalises to `{'op': '<=', 'value': 2}`; satisfying ['eastnor row', 'fairholme row', 'hartsmere row', 'ilmington row']; violating ['glaisdale row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['eastnor row', 'fairholme row', 'glaisdale row', 'ilmington row']; violating ['hartsmere row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Hendon', re-normalises to `{'granularity': 'borough', 'value': 'Hendon'}`; satisfying ['eastnor row', 'fairholme row', 'glaisdale row', 'hartsmere row']; violating ['ilmington row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,750 a month', re-normalises to `{'value': 1750.0}`; satisfying ['eastnor row', 'glaisdale row']; violating ['fairholme row', 'hartsmere row', 'ilmington row']
- **move_in_date** (`move_in_date_satisfied`, branch `unknown`) — user said '1 November', re-normalises to `{'value': '2026-11-01'}`; satisfying []; violating []; unknown ['eastnor row', 'fairholme row', 'glaisdale row', 'hartsmere row', 'ilmington row']

### HO2-011  (retrieval_hard)

- request: `Can you find me a 2-bed house in New Cross under £1,650 a month. If an option misses any of that, leave it out of your answer.`
- frozen listings: 5   fixture: `ho2_011_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '2-bed', re-normalises to `{'op': '==', 'value': 2}`; satisfying ['kirkstall row', 'longmynd row', 'northiam row', 'oakhanger row']; violating ['merrivale row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying ['kirkstall row', 'longmynd row', 'merrivale row', 'oakhanger row']; violating ['northiam row']
- **area** (`area_match`, branch `satisfaction`) — user said 'New Cross', re-normalises to `{'granularity': 'borough', 'value': 'New Cross'}`; satisfying ['kirkstall row', 'longmynd row', 'merrivale row', 'northiam row']; violating ['oakhanger row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,650 a month', re-normalises to `{'value': 1650.0}`; satisfying ['kirkstall row', 'merrivale row']; violating ['longmynd row', 'northiam row', 'oakhanger row']

### HO2-012  (retrieval_hard)

- request: `I need a 1-bed flat in Streatham under £1,500 a month. I only want the ones that tick every box — drop the rest.`
- frozen listings: 5   fixture: `ho2_012_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said '1-bed', re-normalises to `{'op': '==', 'value': 1}`; satisfying ['pilgrims row', 'quarrendon row', 'sandhurst row', 'thornbury row']; violating ['redbourne row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['pilgrims row', 'quarrendon row', 'redbourne row', 'thornbury row']; violating ['sandhurst row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Streatham', re-normalises to `{'granularity': 'borough', 'value': 'Streatham'}`; satisfying ['pilgrims row', 'quarrendon row', 'redbourne row', 'sandhurst row']; violating ['thornbury row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,500 a month', re-normalises to `{'value': 1500.0}`; satisfying ['pilgrims row', 'redbourne row']; violating ['quarrendon row', 'sandhurst row', 'thornbury row']

### HO2-013  (retrieval_hard)

- request: `Help me find a flat with at least 2 bedrooms in Balham under £2,050 a month. Skip anything that fails one of those conditions, please.`
- frozen listings: 5   fixture: `ho2_013_hard.json`
- **bedroom_count** (`bedroom_count_match`, branch `satisfaction`) — user said 'at least 2 bedrooms', re-normalises to `{'op': '>=', 'value': 2}`; satisfying ['upwaltham row', 'vellacott row', 'yarrowby row', 'ashmansworth row']; violating ['wandlebury row']
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['upwaltham row', 'vellacott row', 'wandlebury row', 'ashmansworth row']; violating ['yarrowby row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Balham', re-normalises to `{'granularity': 'borough', 'value': 'Balham'}`; satisfying ['upwaltham row', 'vellacott row', 'wandlebury row', 'yarrowby row']; violating ['ashmansworth row']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £2,050 a month', re-normalises to `{'value': 2050.0}`; satisfying ['upwaltham row', 'wandlebury row']; violating ['vellacott row', 'yarrowby row', 'ashmansworth row']

### HO2-014  (retrieval_hard)

- request: `I'm after a studio in Deptford under £1,350 a month. Just the ones that satisfy all of it; ignore the others.`
- frozen listings: 4   fixture: `ho2_014_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'studio', re-normalises to `{'value': 'studio'}`; satisfying ['bishopstone row', 'chalvington row', 'fernbrook mews']; violating ['dunmowe row']
- **area** (`area_match`, branch `satisfaction`) — user said 'Deptford', re-normalises to `{'granularity': 'borough', 'value': 'Deptford'}`; satisfying ['bishopstone row', 'chalvington row', 'dunmowe row']; violating ['fernbrook mews']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,350 a month', re-normalises to `{'value': 1350.0}`; satisfying ['bishopstone row', 'dunmowe row']; violating ['chalvington row', 'fernbrook mews']

### HO2-015  (retrieval_hard)

- request: `Looking for a room in a house share in Forest Gate under £1,150 a month, ready to move into by 15 September. Anything that does not meet all of those should not be in your list.`
- frozen listings: 4   fixture: `ho2_015_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house share', re-normalises to `{'value': 'room_in_shared'}`; satisfying ['halstow mews', 'wraysbury mews', 'ashlin mews']; violating ['quillon mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Forest Gate', re-normalises to `{'granularity': 'borough', 'value': 'Forest Gate'}`; satisfying ['halstow mews', 'wraysbury mews', 'quillon mews']; violating ['ashlin mews']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £1,150 a month', re-normalises to `{'value': 1150.0}`; satisfying ['halstow mews', 'quillon mews']; violating ['wraysbury mews', 'ashlin mews']
- **move_in_date** (`move_in_date_satisfied`, branch `unknown`) — user said '15 September', re-normalises to `{'value': '2026-09-15'}`; satisfying []; violating []; unknown ['halstow mews', 'wraysbury mews', 'quillon mews', 'ashlin mews']

### HO2-016  (retrieval_hard)

- request: `Trying to find a house in Harringay under £2,300 a month. Only show me ones that actually fit — skip anything that misses any of that.`
- frozen listings: 4   fixture: `ho2_016_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying ['denbury mews', 'osterlyn mews', 'ravensmere mews']; violating ['pentworth mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Harringay', re-normalises to `{'granularity': 'borough', 'value': 'Harringay'}`; satisfying ['denbury mews', 'osterlyn mews', 'pentworth mews']; violating ['ravensmere mews']
- **budget** (`all_results_satisfy`, branch `satisfaction`) — user said 'under £2,300 a month', re-normalises to `{'value': 2300.0}`; satisfying ['denbury mews', 'pentworth mews']; violating ['osterlyn mews', 'ravensmere mews']

### HO2-017  (retrieval_hard)

- request: `Please search for a studio in Willesden Green, within 30 minutes of Baker Street. Please leave out anything that does not meet every one of those.`
- frozen listings: 4   fixture: `ho2_017_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'studio', re-normalises to `{'value': 'studio'}`; satisfying ['sowerby mews', 'ulverton mews', 'vandermeer mews']; violating ['thackray mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Willesden Green', re-normalises to `{'granularity': 'borough', 'value': 'Willesden Green'}`; satisfying ['sowerby mews', 'thackray mews', 'ulverton mews']; violating ['vandermeer mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 30 minutes', re-normalises to `{'value': 30}`; satisfying ['sowerby mews']; violating ['thackray mews', 'ulverton mews', 'vandermeer mews']

### HO2-018  (retrieval_hard)

- request: `I'd like a flat in Colindale, within 35 minutes of Moorgate. Do not include options that break any of those conditions.`
- frozen listings: 4   fixture: `ho2_018_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying ['wexcombe mews', 'zephyrn mews', 'brackendale mews']; violating ['yarnfield mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Colindale', re-normalises to `{'granularity': 'borough', 'value': 'Colindale'}`; satisfying ['wexcombe mews', 'yarnfield mews', 'zephyrn mews']; violating ['brackendale mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 35 minutes', re-normalises to `{'value': 35}`; satisfying ['wexcombe mews']; violating ['yarnfield mews', 'zephyrn mews', 'brackendale mews']

### HO2-019  (retrieval_hard)

- request: `Could you dig out a room in a house share in Leyton, within 40 minutes of Liverpool Street, ready to move into by 15 October. If an option misses any of that, leave it out of your answer.`
- frozen listings: 4   fixture: `ho2_019_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house share', re-normalises to `{'value': 'room_in_shared'}`; satisfying ['calthorpe mews', 'ellesbury mews', 'foxhollow mews']; violating ['drayfield mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Leyton', re-normalises to `{'granularity': 'borough', 'value': 'Leyton'}`; satisfying ['calthorpe mews', 'drayfield mews', 'ellesbury mews']; violating ['foxhollow mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 40 minutes', re-normalises to `{'value': 40}`; satisfying ['calthorpe mews']; violating ['drayfield mews', 'ellesbury mews', 'foxhollow mews']
- **move_in_date** (`move_in_date_satisfied`, branch `unknown`) — user said '15 October', re-normalises to `{'value': '2026-10-15'}`; satisfying []; violating []; unknown ['calthorpe mews', 'drayfield mews', 'ellesbury mews', 'foxhollow mews']

### HO2-020  (retrieval_hard)

- request: `Hi — I'm looking for a maisonette in Walthamstow. I only want the ones that tick every box — drop the rest.`
- frozen listings: 3   fixture: `ho2_020_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'maisonette', re-normalises to `{'value': 'maisonette'}`; satisfying ['hazelbourne mews', 'jarrowfield mews']; violating ['inglewhite mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Walthamstow', re-normalises to `{'granularity': 'borough', 'value': 'Walthamstow'}`; satisfying ['hazelbourne mews', 'inglewhite mews']; violating ['jarrowfield mews']

### HO2-021  (retrieval_hard)

- request: `Can you find me a studio in Peckham. Skip anything that fails one of those conditions, please.`
- frozen listings: 3   fixture: `ho2_021_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'studio', re-normalises to `{'value': 'studio'}`; satisfying ['kelsterne mews', 'mortlake mews']; violating ['lambourne mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Peckham', re-normalises to `{'granularity': 'borough', 'value': 'Peckham'}`; satisfying ['kelsterne mews', 'lambourne mews']; violating ['mortlake mews']

### HO2-022  (retrieval_hard)

- request: `I need a house in Acton, ready to move into by 1 December. Just the ones that satisfy all of it; ignore the others.`
- frozen listings: 3   fixture: `ho2_022_hard.json`
- **room_type** (`room_type_match`, branch `satisfaction`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying ['netherby mews', 'pyrford mews']; violating ['ockendale mews']
- **area** (`area_match`, branch `satisfaction`) — user said 'Acton', re-normalises to `{'granularity': 'borough', 'value': 'Acton'}`; satisfying ['netherby mews', 'ockendale mews']; violating ['pyrford mews']
- **move_in_date** (`move_in_date_satisfied`, branch `unknown`) — user said '1 December', re-normalises to `{'value': '2026-12-01'}`; satisfying []; violating []; unknown ['netherby mews', 'ockendale mews', 'pyrford mews']

### HO2-023  (retrieval_hard)

- request: `Help me find somewhere to rent in Tooting, within 40 minutes of Waterloo, ready to move into by 28 September. Anything that does not meet all of those should not be in your list.`
- frozen listings: 4   fixture: `ho2_023_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Tooting', re-normalises to `{'granularity': 'borough', 'value': 'Tooting'}`; satisfying ['rushmoor mews', 'stanbrook mews', 'uffington mews']; violating ['tarleton mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 40 minutes', re-normalises to `{'value': 40}`; satisfying ['rushmoor mews']; violating ['stanbrook mews', 'tarleton mews', 'uffington mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '28 September', re-normalises to `{'value': '2026-09-28'}`; satisfying ['rushmoor mews', 'stanbrook mews', 'tarleton mews']; violating ['uffington mews']

### HO2-024  (retrieval_hard)

- request: `I'm after somewhere to rent in Bermondsey, within 25 minutes of London Bridge, ready to move into by 5 October. Only show me ones that actually fit — skip anything that misses any of that.`
- frozen listings: 4   fixture: `ho2_024_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Bermondsey', re-normalises to `{'granularity': 'borough', 'value': 'Bermondsey'}`; satisfying ['verwood mews', 'whitmarsh mews', 'yealand mews']; violating ['xanthe mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 25 minutes', re-normalises to `{'value': 25}`; satisfying ['verwood mews']; violating ['whitmarsh mews', 'xanthe mews', 'yealand mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '5 October', re-normalises to `{'value': '2026-10-05'}`; satisfying ['verwood mews', 'whitmarsh mews', 'xanthe mews']; violating ['yealand mews']

### HO2-025  (retrieval_hard)

- request: `Looking for somewhere to rent in Catford, within 30 minutes of Blackfriars, ready to move into by 15 November. Please leave out anything that does not meet every one of those.`
- frozen listings: 4   fixture: `ho2_025_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Catford', re-normalises to `{'granularity': 'borough', 'value': 'Catford'}`; satisfying ['zennorby mews', 'ambrose mews', 'carbery mews']; violating ['beaumaris mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 30 minutes', re-normalises to `{'value': 30}`; satisfying ['zennorby mews']; violating ['ambrose mews', 'beaumaris mews', 'carbery mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '15 November', re-normalises to `{'value': '2026-11-15'}`; satisfying ['zennorby mews', 'ambrose mews', 'beaumaris mews']; violating ['carbery mews']

### HO2-026  (retrieval_hard)

- request: `Trying to find somewhere to rent in Streatham, within 35 minutes of Victoria, ready to move into by 20 October, with parking. Do not include options that break any of those conditions.`
- frozen listings: 5   fixture: `ho2_026_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Streatham', re-normalises to `{'granularity': 'borough', 'value': 'Streatham'}`; satisfying ['dunsfold mews', 'eastnor mews', 'glaisdale mews', 'hartsmere mews']; violating ['fairholme mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 35 minutes', re-normalises to `{'value': 35}`; satisfying ['dunsfold mews']; violating ['eastnor mews', 'fairholme mews', 'glaisdale mews', 'hartsmere mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '20 October', re-normalises to `{'value': '2026-10-20'}`; satisfying ['dunsfold mews', 'eastnor mews', 'fairholme mews', 'hartsmere mews']; violating ['glaisdale mews']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'parking', re-normalises to `{'value': 'parking'}`; satisfying ['dunsfold mews', 'eastnor mews', 'fairholme mews', 'glaisdale mews']; violating ['hartsmere mews']

### HO2-027  (retrieval_hard)

- request: `Please search for somewhere to rent in New Cross, within 40 minutes of Old Street, with a garden. If an option misses any of that, leave it out of your answer.`
- frozen listings: 4   fixture: `ho2_027_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'New Cross', re-normalises to `{'granularity': 'borough', 'value': 'New Cross'}`; satisfying ['ilmington mews', 'jevington mews', 'longmynd mews']; violating ['kirkstall mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 40 minutes', re-normalises to `{'value': 40}`; satisfying ['ilmington mews']; violating ['jevington mews', 'kirkstall mews', 'longmynd mews']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'a garden', re-normalises to `{'value': 'garden'}`; satisfying ['ilmington mews', 'jevington mews', 'kirkstall mews']; violating ['longmynd mews']

### HO2-028  (retrieval_hard)

- request: `I'd like somewhere to rent in Harringay, within 25 minutes of Farringdon, furnished. I only want the ones that tick every box — drop the rest.`
- frozen listings: 4   fixture: `ho2_028_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Harringay', re-normalises to `{'granularity': 'borough', 'value': 'Harringay'}`; satisfying ['merrivale mews', 'northiam mews', 'pilgrims mews']; violating ['oakhanger mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 25 minutes', re-normalises to `{'value': 25}`; satisfying ['merrivale mews']; violating ['northiam mews', 'oakhanger mews', 'pilgrims mews']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'furnished', re-normalises to `{'value': 'furnished'}`; satisfying ['merrivale mews', 'northiam mews', 'oakhanger mews']; violating ['pilgrims mews']

### HO2-029  (retrieval_hard)

- request: `Could you dig out somewhere to rent in Wood Green, within 30 minutes of Holborn, pet-friendly. Skip anything that fails one of those conditions, please.`
- frozen listings: 4   fixture: `ho2_029_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Wood Green', re-normalises to `{'granularity': 'borough', 'value': 'Wood Green'}`; satisfying ['quarrendon mews', 'redbourne mews', 'thornbury mews']; violating ['sandhurst mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 30 minutes', re-normalises to `{'value': 30}`; satisfying ['quarrendon mews']; violating ['redbourne mews', 'sandhurst mews', 'thornbury mews']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'pet-friendly', re-normalises to `{'value': 'pet_friendly'}`; satisfying ['quarrendon mews', 'redbourne mews', 'sandhurst mews']; violating ['thornbury mews']

### HO2-030  (retrieval_hard)

- request: `Hi — I'm looking for somewhere to rent in Balham, within 35 minutes of Aldgate, ready to move into by 20 September. Just the ones that satisfy all of it; ignore the others.`
- frozen listings: 4   fixture: `ho2_030_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Balham', re-normalises to `{'granularity': 'borough', 'value': 'Balham'}`; satisfying ['upwaltham mews', 'vellacott mews', 'yarrowby mews']; violating ['wandlebury mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 35 minutes', re-normalises to `{'value': 35}`; satisfying ['upwaltham mews']; violating ['vellacott mews', 'wandlebury mews', 'yarrowby mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '20 September', re-normalises to `{'value': '2026-09-20'}`; satisfying ['upwaltham mews', 'vellacott mews', 'wandlebury mews']; violating ['yarrowby mews']

### HO2-031  (retrieval_hard)

- request: `Can you find me somewhere to rent in Colindale, within 40 minutes of Paddington, ready to move into by 15 December. Anything that does not meet all of those should not be in your list.`
- frozen listings: 4   fixture: `ho2_031_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Colindale', re-normalises to `{'granularity': 'borough', 'value': 'Colindale'}`; satisfying ['ashmansworth mews', 'bishopstone mews', 'dunmowe mews']; violating ['chalvington mews']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 40 minutes', re-normalises to `{'value': 40}`; satisfying ['ashmansworth mews']; violating ['bishopstone mews', 'chalvington mews', 'dunmowe mews']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '15 December', re-normalises to `{'value': '2026-12-15'}`; satisfying ['ashmansworth mews', 'bishopstone mews', 'chalvington mews']; violating ['dunmowe mews']

### HO2-032  (retrieval_hard)

- request: `I need somewhere to rent in Deptford, within 25 minutes of Barbican, with a balcony. Only show me ones that actually fit — skip anything that misses any of that.`
- frozen listings: 4   fixture: `ho2_032_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Deptford', re-normalises to `{'granularity': 'borough', 'value': 'Deptford'}`; satisfying ['fernbrook terrace', 'halstow terrace', 'quillon terrace']; violating ['wraysbury terrace']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 25 minutes', re-normalises to `{'value': 25}`; satisfying ['fernbrook terrace']; violating ['halstow terrace', 'wraysbury terrace', 'quillon terrace']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'a balcony', re-normalises to `{'value': 'balcony'}`; satisfying ['fernbrook terrace', 'halstow terrace', 'wraysbury terrace']; violating ['quillon terrace']

### HO2-033  (retrieval_hard)

- request: `Help me find somewhere to rent in Hendon, within 30 minutes of Baker Street, ready to move into by 10 October, with a lift. Please leave out anything that does not meet every one of those.`
- frozen listings: 5   fixture: `ho2_033_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Hendon', re-normalises to `{'granularity': 'borough', 'value': 'Hendon'}`; satisfying ['ashlin terrace', 'marchcroft terrace', 'osterlyn terrace', 'pentworth terrace']; violating ['denbury terrace']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 30 minutes', re-normalises to `{'value': 30}`; satisfying ['ashlin terrace']; violating ['marchcroft terrace', 'denbury terrace', 'osterlyn terrace', 'pentworth terrace']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '10 October', re-normalises to `{'value': '2026-10-10'}`; satisfying ['ashlin terrace', 'marchcroft terrace', 'denbury terrace', 'pentworth terrace']; violating ['osterlyn terrace']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'a lift', re-normalises to `{'value': 'lift'}`; satisfying ['ashlin terrace', 'marchcroft terrace', 'denbury terrace', 'osterlyn terrace']; violating ['pentworth terrace']

### HO2-034  (retrieval_hard)

- request: `I'm after somewhere to rent in Forest Gate, within 35 minutes of Liverpool Street, ready to move into by 5 November, with bills included. Do not include options that break any of those conditions.`
- frozen listings: 5   fixture: `ho2_034_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Forest Gate', re-normalises to `{'granularity': 'borough', 'value': 'Forest Gate'}`; satisfying ['ravensmere terrace', 'sowerby terrace', 'ulverton terrace', 'vandermeer terrace']; violating ['thackray terrace']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 35 minutes', re-normalises to `{'value': 35}`; satisfying ['ravensmere terrace']; violating ['sowerby terrace', 'thackray terrace', 'ulverton terrace', 'vandermeer terrace']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '5 November', re-normalises to `{'value': '2026-11-05'}`; satisfying ['ravensmere terrace', 'sowerby terrace', 'thackray terrace', 'vandermeer terrace']; violating ['ulverton terrace']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'bills included', re-normalises to `{'value': 'bills_included'}`; satisfying ['ravensmere terrace', 'sowerby terrace', 'thackray terrace', 'ulverton terrace']; violating ['vandermeer terrace']

### HO2-035  (retrieval_hard)

- request: `Looking for somewhere to rent in Crouch End, within 40 minutes of Moorgate, ready to move into by 25 September, with a washing machine. If an option misses any of that, leave it out of your answer.`
- frozen listings: 5   fixture: `ho2_035_hard.json`
- **area** (`area_match`, branch `satisfaction`) — user said 'Crouch End', re-normalises to `{'granularity': 'borough', 'value': 'Crouch End'}`; satisfying ['wexcombe terrace', 'yarnfield terrace', 'brackendale terrace', 'calthorpe terrace']; violating ['zephyrn terrace']
- **commute** (`commute_leq_minutes`, branch `satisfaction`) — user said 'within 40 minutes', re-normalises to `{'value': 40}`; satisfying ['wexcombe terrace']; violating ['yarnfield terrace', 'zephyrn terrace', 'brackendale terrace', 'calthorpe terrace']
- **move_in_date** (`move_in_date_satisfied`, branch `satisfaction`) — user said '25 September', re-normalises to `{'value': '2026-09-25'}`; satisfying ['wexcombe terrace', 'yarnfield terrace', 'zephyrn terrace', 'calthorpe terrace']; violating ['brackendale terrace']
- **property_feature** (`property_feature_present`, branch `satisfaction`) — user said 'a washing machine', re-normalises to `{'value': 'washing_machine'}`; satisfying ['wexcombe terrace', 'yarnfield terrace', 'zephyrn terrace', 'brackendale terrace']; violating ['calthorpe terrace']

### HO2-036  (retrieval_soft)

- request: `I've just been offered a job near Liverpool Street and I don't know the area at all. What sort of places come up in Walthamstow at the moment?`
- frozen listings: 3   fixture: `ho2_036_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Walthamstow', re-normalises to `{'granularity': 'borough', 'value': 'Walthamstow'}`; satisfying ['drayfield terrace', 'ellesbury terrace', 'foxhollow terrace']; violating []

### HO2-037  (retrieval_soft)

- request: `We're thinking about moving to Peckham next year. Could you show me what is on the rental market there right now so we get a feel for it?`
- frozen listings: 3   fixture: `ho2_037_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Peckham', re-normalises to `{'granularity': 'borough', 'value': 'Peckham'}`; satisfying ['garrowby terrace', 'hazelbourne terrace', 'inglewhite terrace']; violating []

### HO2-038  (retrieval_soft)

- request: `My partner and I are browsing rather than committing. What is currently listed in Tooting?`
- frozen listings: 3   fixture: `ho2_038_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Tooting', re-normalises to `{'granularity': 'borough', 'value': 'Tooting'}`; satisfying ['jarrowfield terrace', 'kelsterne terrace', 'lambourne terrace']; violating []

### HO2-039  (retrieval_soft)

- request: `Give me a general picture of what is available to rent in Acton at the moment.`
- frozen listings: 3   fixture: `ho2_039_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Acton', re-normalises to `{'granularity': 'borough', 'value': 'Acton'}`; satisfying ['mortlake terrace', 'netherby terrace', 'ockendale terrace']; violating []

### HO2-040  (retrieval_soft)

- request: `What kind of rental stock does Crouch End have? Just curious what comes up.`
- frozen listings: 3   fixture: `ho2_040_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Crouch End', re-normalises to `{'granularity': 'borough', 'value': 'Crouch End'}`; satisfying ['pyrford terrace', 'quenby terrace', 'rushmoor terrace']; violating []

### HO2-041  (retrieval_soft)

- request: `I keep hearing Bermondsey is good value. What is listed there right now?`
- frozen listings: 3   fixture: `ho2_041_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Bermondsey', re-normalises to `{'granularity': 'borough', 'value': 'Bermondsey'}`; satisfying ['stanbrook terrace', 'tarleton terrace', 'uffington terrace']; violating []

### HO2-042  (retrieval_soft)

- request: `Show me what is on the market in Catford — no particular requirements yet.`
- frozen listings: 3   fixture: `ho2_042_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Catford', re-normalises to `{'granularity': 'borough', 'value': 'Catford'}`; satisfying ['verwood terrace', 'whitmarsh terrace', 'xanthe terrace']; violating []

### HO2-043  (retrieval_soft)

- request: `We might relocate to Wood Green. What is available there at present?`
- frozen listings: 3   fixture: `ho2_043_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Wood Green', re-normalises to `{'granularity': 'borough', 'value': 'Wood Green'}`; satisfying ['yealand terrace', 'zennorby terrace', 'ambrose terrace']; violating []

### HO2-044  (retrieval_soft)

- request: `Could you pull up whatever is currently listed in Balham for me to look through?`
- frozen listings: 3   fixture: `ho2_044_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Balham', re-normalises to `{'granularity': 'borough', 'value': 'Balham'}`; satisfying ['beaumaris terrace', 'carbery terrace', 'dunsfold terrace']; violating []

### HO2-045  (retrieval_soft)

- request: `I'd like to see the current rental listings in Deptford, please.`
- frozen listings: 3   fixture: `ho2_045_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Deptford', re-normalises to `{'granularity': 'borough', 'value': 'Deptford'}`; satisfying ['eastnor terrace', 'fairholme terrace', 'glaisdale terrace']; violating []

### HO2-046  (retrieval_soft)

- request: `What is renting in Harringay these days? I'm at the browsing stage.`
- frozen listings: 3   fixture: `ho2_046_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Harringay', re-normalises to `{'granularity': 'borough', 'value': 'Harringay'}`; satisfying ['hartsmere terrace', 'ilmington terrace', 'jevington terrace']; violating []

### HO2-047  (retrieval_soft)

- request: `Just exploring — what rental properties are showing in Colindale?`
- frozen listings: 3   fixture: `ho2_047_soft.json`
- **area** (`area_match`, branch `trivial`) — user said 'Colindale', re-normalises to `{'granularity': 'borough', 'value': 'Colindale'}`; satisfying ['kirkstall terrace', 'longmynd terrace', 'merrivale terrace']; violating []

### HO2-048  (retrieval_soft)

- request: `Any 4-bed houses in Streatham under £1,200 a month?`
- frozen listings: 0   fixture: `ho2_048_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £1,200 a month', re-normalises to `{'value': 1200.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Streatham', re-normalises to `{'granularity': 'borough', 'value': 'Streatham'}`; satisfying []; violating []
- **bedroom_count** (`bedroom_count_match`, branch `no_result`) — user said '4-bed', re-normalises to `{'op': '==', 'value': 4}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying []; violating []

### HO2-049  (retrieval_soft)

- request: `Is there anything with a garden in New Cross for under £700 a month?`
- frozen listings: 0   fixture: `ho2_049_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £700 a month', re-normalises to `{'value': 700.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'New Cross', re-normalises to `{'granularity': 'borough', 'value': 'New Cross'}`; satisfying []; violating []
- **property_feature** (`property_feature_present`, branch `no_result`) — user said 'a garden', re-normalises to `{'value': 'garden'}`; satisfying []; violating []

### HO2-050  (retrieval_soft)

- request: `I need a 3-bed flat in Hendon under £900 a month — anything?`
- frozen listings: 0   fixture: `ho2_050_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £900 a month', re-normalises to `{'value': 900.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Hendon', re-normalises to `{'granularity': 'borough', 'value': 'Hendon'}`; satisfying []; violating []
- **bedroom_count** (`bedroom_count_match`, branch `no_result`) — user said '3-bed', re-normalises to `{'op': '==', 'value': 3}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'flat', re-normalises to `{'value': 'flat'}`; satisfying []; violating []

### HO2-051  (retrieval_soft)

- request: `Looking for a studio in Leyton at under £500 a month. What have you got?`
- frozen listings: 0   fixture: `ho2_051_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £500 a month', re-normalises to `{'value': 500.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Leyton', re-normalises to `{'granularity': 'borough', 'value': 'Leyton'}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'studio', re-normalises to `{'value': 'studio'}`; satisfying []; violating []

### HO2-052  (retrieval_soft)

- request: `Are there any 5-bed houses in Forest Gate under £1,500 a month?`
- frozen listings: 0   fixture: `ho2_052_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £1,500 a month', re-normalises to `{'value': 1500.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Forest Gate', re-normalises to `{'granularity': 'borough', 'value': 'Forest Gate'}`; satisfying []; violating []
- **bedroom_count** (`bedroom_count_match`, branch `no_result`) — user said '5-bed', re-normalises to `{'op': '==', 'value': 5}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'house', re-normalises to `{'value': 'house'}`; satisfying []; violating []

### HO2-053  (retrieval_soft)

- request: `Anything in Willesden Green under £600 a month, any size?`
- frozen listings: 0   fixture: `ho2_053_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £600 a month', re-normalises to `{'value': 600.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Willesden Green', re-normalises to `{'granularity': 'borough', 'value': 'Willesden Green'}`; satisfying []; violating []

### HO2-054  (retrieval_soft)

- request: `Can you find a 2-bed maisonette in Bermondsey under £800 a month?`
- frozen listings: 0   fixture: `ho2_054_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £800 a month', re-normalises to `{'value': 800.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Bermondsey', re-normalises to `{'granularity': 'borough', 'value': 'Bermondsey'}`; satisfying []; violating []
- **bedroom_count** (`bedroom_count_match`, branch `no_result`) — user said '2-bed', re-normalises to `{'op': '==', 'value': 2}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'maisonette', re-normalises to `{'value': 'maisonette'}`; satisfying []; violating []

### HO2-055  (retrieval_soft)

- request: `I want a bungalow in Wood Green under £1,000 a month. Anything listed?`
- frozen listings: 0   fixture: `ho2_055_empty.json`
- **budget** (`all_results_satisfy`, branch `no_result`) — user said 'under £1,000 a month', re-normalises to `{'value': 1000.0}`; satisfying []; violating []
- **area** (`area_match`, branch `no_result`) — user said 'Wood Green', re-normalises to `{'granularity': 'borough', 'value': 'Wood Green'}`; satisfying []; violating []
- **room_type** (`room_type_match`, branch `no_result`) — user said 'bungalow', re-normalises to `{'value': 'bungalow'}`; satisfying []; violating []

### HO2-056  (calculation)

- request: `The agent quoted me £275 pw. My landlord reference form wants a monthly figure — what should I put?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-057  (calculation)

- request: `Everything round here is priced weekly. £340 a week is how much a month?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-058  (calculation)

- request: `I budget monthly but this one is listed at £395 per week. Convert it for me and show your working.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-059  (calculation)

- request: `Quick sanity check: is £425 a week more or less than £2,000 a month? Give me the monthly equivalent.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-060  (calculation)

- request: `A place I like is advertised at £480 per week. What is that as a monthly rent? Show the arithmetic.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-061  (calculation)

- request: `The listing says £1,290 pcm. My budget spreadsheet is weekly — what is the weekly equivalent?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-062  (calculation)

- request: `My flatmate works out everything by the week. £1,675 a month comes to what weekly?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-063  (calculation)

- request: `Rent is £2,480 monthly. I need the per-week number for a form.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-064  (calculation)

- request: `Rent is £1,420 a month. How much deposit can the landlord legally ask for under the Tenant Fees Act?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-065  (calculation)

- request: `The agent is asking for a deposit on a £1,580 pcm flat. What is the legal maximum they can hold?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-066  (calculation)

- request: `On £1,875 a month, what is the biggest deposit I can be asked for, and on what basis?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-067  (calculation)

- request: `Rent is £2,100 a month. How much deposit can the landlord legally ask for under the Tenant Fees Act?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-068  (calculation)

- request: `The agent is asking for a deposit on a £4,300 pcm flat. What is the legal maximum they can hold?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-069  (calculation)

- request: `On £4,750 a month, what is the biggest deposit I can be asked for, and on what basis?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-070  (calculation)

- request: `If I take a flat at £1,350 a month, what do I need up front on day one — first month plus the deposit?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-071  (calculation)

- request: `I have got £1,690 pcm agreed. How much cash do I need to hand over before I get the keys, counting the first month and the deposit?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-072  (calculation)

- request: `Total up front for a £2,250 a month place — first month and deposit together, please.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-073  (calculation)

- request: `What would £1,180 a month come to over a full 12-month tenancy?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-074  (calculation)

- request: `Over a year, how much rent is £1,925 per month in total?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-075  (calculation)

- request: `I want the twelve-month total for a £3,050 pcm tenancy.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-076  (memory)

- request: `You told me before what my ceiling was — what budget did I give you?`
- frozen listings: 0   fixture: `ho2_076_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-077  (memory)

- request: `Which area did I say I wanted to live in?`
- frozen listings: 0   fixture: `ho2_077_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-078  (memory)

- request: `Remind me which station I said I commute to.`
- frozen listings: 0   fixture: `ho2_078_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-079  (memory)

- request: `How many bedrooms did I tell you I needed?`
- frozen listings: 0   fixture: `ho2_079_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-080  (memory)

- request: `Did I mention anything about pets?`
- frozen listings: 0   fixture: `ho2_080_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-081  (memory)

- request: `What move-in date did I give you?`
- frozen listings: 0   fixture: `ho2_081_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-082  (memory)

- request: `Did I say furnished or unfurnished?`
- frozen listings: 0   fixture: `ho2_082_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-083  (memory)

- request: `What did I say about working from home?`
- frozen listings: 0   fixture: `ho2_083_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-084  (memory)

- request: `Did I tell you whether I need parking?`
- frozen listings: 0   fixture: `ho2_084_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-085  (memory)

- request: `How many of us did I say would be living there?`
- frozen listings: 0   fixture: `ho2_085_recall.json`
- no user hard constraint (by design for this stratum)

### HO2-086  (memory)

- request: `My budget has gone up — I can now go to £1,900 a month. Please remember that.`
- frozen listings: 0   fixture: `ho2_086_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-087  (memory)

- request: `Please make a note that I have decided on Peckham rather than anywhere else.`
- frozen listings: 0   fixture: `ho2_087_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-088  (memory)

- request: `Save this for next time: I need to be within 30 minutes of London Bridge.`
- frozen listings: 0   fixture: `ho2_088_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-089  (memory)

- request: `Note that I now need three bedrooms, not two.`
- frozen listings: 0   fixture: `ho2_089_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-090  (memory)

- request: `Remember that I have a dog, so anywhere I look has to allow pets.`
- frozen listings: 0   fixture: `ho2_090_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-091  (memory)

- request: `Please keep a note that I cannot move before 15 November.`
- frozen listings: 0   fixture: `ho2_091_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-092  (memory)

- request: `For future reference, I would prefer somewhere furnished.`
- frozen listings: 0   fixture: `ho2_092_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-093  (memory)

- request: `Record that I want a garden — that is non-negotiable for me now.`
- frozen listings: 0   fixture: `ho2_093_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-094  (memory)

- request: `Remember my new work address is near Farringdon, not where I said before.`
- frozen listings: 0   fixture: `ho2_094_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-095  (memory)

- request: `Please store that my maximum commute is 40 minutes door to door.`
- frozen listings: 0   fixture: `ho2_095_remember.json`
- no user hard constraint (by design for this stratum)

### HO2-096  (clarify)

- request: `Find me somewhere to live.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-097  (clarify)

- request: `I need a place by next month, can you sort it?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-098  (clarify)

- request: `What's the best option for me?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-099  (clarify)

- request: `Book me a viewing for the good one.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-100  (clarify)

- request: `Is it within my budget?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-101  (clarify)

- request: `How much would the deposit be?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-102  (clarify)

- request: `Which of those two is closer to work?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-103  (clarify)

- request: `Can you check if that one is still available?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-104  (clarify)

- request: `Can you sign the tenancy agreement on my behalf?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-105  (clarify)

- request: `What will rents in Peckham be in three years' time?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-106  (clarify)

- request: `Can you get the landlord to drop the rent by £200?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-107  (clarify)

- request: `Please transfer my deposit to the agent for me.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-108  (clarify)

- request: `Tell me which of my neighbours have criminal records.`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-109  (clarify)

- request: `Can you write me a reference from my previous landlord?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)

### HO2-110  (clarify)

- request: `Will I be approved for this tenancy by the referencing company?`
- frozen listings: 0   fixture: `(none)`
- no user hard constraint (by design for this stratum)
