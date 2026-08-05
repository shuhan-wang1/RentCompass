# held-out preflight —— 人工抽查清单

来源：`evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl`，静态门禁报告：`evaluation/benchmark/holdout_v2/preflight_report.json`，抽样 seed `20260805`。

静态检查查不了「写得对不对」。以下每题请人工确认四点，**任一项不通过 → 替换该题，不要在跑完之后用 N/A 消化**。

| # | case_id | 分层 | 硬约束明确可验证且不矛盾 | 已写明正确完成态 | 可评主张都有依据来源 | 通过? |
|---|---|---|---|---|---|---|
| 1 | `HO2-066` | calculation |  |  |  |  |
| 2 | `HO2-094` | memory |  |  |  |  |
| 3 | `HO2-017` | retrieval_hard |  |  |  |  |
| 4 | `HO2-105` | clarify |  |  |  |  |
| 5 | `HO2-096` | clarify |  |  |  |  |
| 6 | `HO2-022` | retrieval_hard |  |  |  |  |
| 7 | `HO2-070` | calculation |  |  |  |  |
| 8 | `HO2-058` | calculation |  |  |  |  |
| 9 | `HO2-098` | clarify |  |  |  |  |
| 10 | `HO2-038` | retrieval_soft |  |  |  |  |
| 11 | `HO2-092` | memory |  |  |  |  |
| 12 | `HO2-110` | clarify |  |  |  |  |
| 13 | `HO2-023` | retrieval_hard |  |  |  |  |
| 14 | `HO2-090` | memory |  |  |  |  |
| 15 | `HO2-009` | retrieval_hard |  |  |  |  |
| 16 | `HO2-074` | calculation |  |  |  |  |
| 17 | `HO2-099` | clarify |  |  |  |  |
| 18 | `HO2-104` | clarify |  |  |  |  |
| 19 | `HO2-048` | retrieval_soft |  |  |  |  |
| 20 | `HO2-079` | memory |  |  |  |  |

---

## HO2-066  (calculation)

- 请求：`On £1,875 a month, what is the biggest deposit I can be asked for, and on what basis?`
- 硬约束：`[]`
- 正确完成态：`Annual rent is £22,500, which is below the £50,000 threshold, so the cap is 5 weeks' rent: £2,163.46. The answer must name the threshold it applied.`
- fixture：`（无）` ｜ reference_calculations：`有`

## HO2-094  (memory)

- 请求：`Remember my new work address is near Farringdon, not where I said before.`
- 硬约束：`[]`
- 正确完成态：`Writes the preference to memory and confirms back what was stored (Farringdon). Correct behaviour is a confirmation, not a property search; not producing listings is not a defect here.`
- fixture：`ho2_094_remember.json` ｜ reference_calculations：`无`

## HO2-017  (retrieval_hard)

- 请求：`Please search for a studio in Willesden Green, within 30 minutes of Baker Street. Please leave out anything that does not meet every one of those.`
- 硬约束：`[{"type": "room_type_match", "user_text": "studio", "value": "studio"}, {"granularity": "borough", "type": "area_match", "user_text": "Willesden Green", "value": "Willesden Green"}, {"dest": "Baker Street", "type": "commute_leq_minutes", "user_text": "within 30 minutes", "value": 30}]`
- 正确完成态：`Present ONLY the frozen listings that satisfy every stated condition (property type = studio; commute to Baker Street <= 30 min; located in Willesden Green). The evidence deliberately contains options that break at least one condition; the user asked for those to be left out, so a correct answer mus`
- fixture：`ho2_017_hard.json` ｜ reference_calculations：`无`

## HO2-105  (clarify)

- 请求：`What will rents in Peckham be in three years' time?`
- 硬约束：`[]`
- 正确完成态：`The request is outside what a rental-search assistant can do (forecasting future market prices). The correct behaviour is to say plainly that it cannot be done and, where useful, point to what can be done instead. Refusing here is the success condition; producing a confident answer would be the defe`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-096  (clarify)

- 请求：`Find me somewhere to live.`
- 硬约束：`[]`
- 正确完成态：`The request cannot be answered as stated: no area, no budget, no property type — nothing to search on. The correct behaviour is to ask ONE specific clarifying question that names the missing piece, or to state plainly what is missing. Producing listings, a price or a deposit figure here would mean i`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-022  (retrieval_hard)

- 请求：`I need a house in Acton, ready to move into by 1 December. Just the ones that satisfy all of it; ignore the others.`
- 硬约束：`[{"type": "room_type_match", "user_text": "house", "value": "house"}, {"granularity": "borough", "type": "area_match", "user_text": "Acton", "value": "Acton"}, {"op": "<=", "type": "move_in_date_satisfied", "user_text": "1 December", "value": "2026-12-01"}]`
- 正确完成态：`Present ONLY the frozen listings that satisfy every stated condition (property type = house; located in Acton; available on or before 2026-12-01). The evidence deliberately contains options that break at least one condition; the user asked for those to be left out, so a correct answer must not put t`
- fixture：`ho2_022_hard.json` ｜ reference_calculations：`无`

## HO2-070  (calculation)

- 请求：`If I take a flat at £1,350 a month, what do I need up front on day one — first month plus the deposit?`
- 硬约束：`[]`
- 正确完成态：`First month £1,350 plus a 5-week deposit £1,557.69 gives £2,907.69. The answer must state the deposit basis it used.`
- fixture：`（无）` ｜ reference_calculations：`有`

## HO2-058  (calculation)

- 请求：`I budget monthly but this one is listed at £395 per week. Convert it for me and show your working.`
- 硬约束：`[]`
- 正确完成态：`Converts weekly to monthly with the frozen formula weekly*52/12 and states £1,711.67 per month (rounding to the nearest penny or pound is fine). Must not use *4 or *4.33.`
- fixture：`（无）` ｜ reference_calculations：`有`

## HO2-098  (clarify)

- 请求：`What's the best option for me?`
- 硬约束：`[]`
- 正确完成态：`The request cannot be answered as stated: 'best' with no criteria and no candidate set to rank. The correct behaviour is to ask ONE specific clarifying question that names the missing piece, or to state plainly what is missing. Producing listings, a price or a deposit figure here would mean inventin`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-038  (retrieval_soft)

- 请求：`My partner and I are browsing rather than committing. What is currently listed in Tooting?`
- 硬约束：`[{"granularity": "borough", "type": "area_match", "user_text": "Tooting", "value": "Tooting"}]`
- 正确完成态：`The user stated no hard condition, so nothing has to be filtered. A correct answer summarises the three frozen listings using only their structured fields (price, bedrooms, type, area, availability, features) and may ask what the user's budget or must-haves are. It must not invent listings, prices o`
- fixture：`ho2_038_soft.json` ｜ reference_calculations：`无`

## HO2-092  (memory)

- 请求：`For future reference, I would prefer somewhere furnished.`
- 硬约束：`[]`
- 正确完成态：`Writes the preference to memory and confirms back what was stored (furnished). Correct behaviour is a confirmation, not a property search; not producing listings is not a defect here.`
- fixture：`ho2_092_remember.json` ｜ reference_calculations：`无`

## HO2-110  (clarify)

- 请求：`Will I be approved for this tenancy by the referencing company?`
- 硬约束：`[]`
- 正确完成态：`The request is outside what a rental-search assistant can do (a third party's credit decision). The correct behaviour is to say plainly that it cannot be done and, where useful, point to what can be done instead. Refusing here is the success condition; producing a confident answer would be the defec`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-023  (retrieval_hard)

- 请求：`Help me find somewhere to rent in Tooting, within 40 minutes of Waterloo, ready to move into by 28 September. Anything that does not meet all of those should not be in your list.`
- 硬约束：`[{"granularity": "borough", "type": "area_match", "user_text": "Tooting", "value": "Tooting"}, {"dest": "Waterloo", "type": "commute_leq_minutes", "user_text": "within 40 minutes", "value": 40}, {"op": "<=", "type": "move_in_date_satisfied", "user_text": "28 September", "value": "2026-09-28"}]`
- 正确完成态：`Present ONLY the frozen listings that satisfy every stated condition (commute to Waterloo <= 40 min; located in Tooting; available on or before 2026-09-28). The evidence deliberately contains options that break at least one condition; the user asked for those to be left out, so a correct answer must`
- fixture：`ho2_023_hard.json` ｜ reference_calculations：`无`

## HO2-090  (memory)

- 请求：`Remember that I have a dog, so anywhere I look has to allow pets.`
- 硬约束：`[]`
- 正确完成态：`Writes the preference to memory and confirms back what was stored (a dog). Correct behaviour is a confirmation, not a property search; not producing listings is not a defect here.`
- fixture：`ho2_090_remember.json` ｜ reference_calculations：`无`

## HO2-009  (retrieval_hard)

- 请求：`Could you dig out a 1-bed flat in Wood Green under £1,550 a month. Please leave out anything that does not meet every one of those.`
- 硬约束：`[{"op": "==", "type": "bedroom_count_match", "user_text": "1-bed", "value": 1}, {"type": "room_type_match", "user_text": "flat", "value": "flat"}, {"granularity": "borough", "type": "area_match", "user_text": "Wood Green", "value": "Wood Green"}, {"field": "monthly_rent", "op": "<=", "type": "all_results_satisfy", "user_text": "under £1,550 a month", "value": 1550}]`
- 正确完成态：`Present ONLY the frozen listings that satisfy every stated condition (monthly rent <= GBP 1,550; bedrooms == 1; property type = flat; located in Wood Green). The evidence deliberately contains options that break at least one condition; the user asked for those to be left out, so a correct answer mus`
- fixture：`ho2_009_hard.json` ｜ reference_calculations：`无`

## HO2-074  (calculation)

- 请求：`Over a year, how much rent is £1,925 per month in total?`
- 硬约束：`[]`
- 正确完成态：`States £23,100 for the year.`
- fixture：`（无）` ｜ reference_calculations：`有`

## HO2-099  (clarify)

- 请求：`Book me a viewing for the good one.`
- 硬约束：`[]`
- 正确完成态：`The request cannot be answered as stated: 'the good one' has no referent and there is no prior turn in this conversation. The correct behaviour is to ask ONE specific clarifying question that names the missing piece, or to state plainly what is missing. Producing listings, a price or a deposit figur`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-104  (clarify)

- 请求：`Can you sign the tenancy agreement on my behalf?`
- 硬约束：`[]`
- 正确完成态：`The request is outside what a rental-search assistant can do (acting as the user's legal agent). The correct behaviour is to say plainly that it cannot be done and, where useful, point to what can be done instead. Refusing here is the success condition; producing a confident answer would be the defe`
- fixture：`（无）` ｜ reference_calculations：`无`

## HO2-048  (retrieval_soft)

- 请求：`Any 4-bed houses in Streatham under £1,200 a month?`
- 硬约束：`[{"field": "monthly_rent", "op": "<=", "type": "all_results_satisfy", "user_text": "under £1,200 a month", "value": 1200}, {"granularity": "borough", "type": "area_match", "user_text": "Streatham", "value": "Streatham"}, {"op": "==", "type": "bedroom_count_match", "user_text": "4-bed", "value": 4}, {"type": "room_type_match", "user_text": "house", "value": "house"}]`
- 正确完成态：`The frozen search returns no listings. The correct answer states honestly that no match was found at that budget in that area, does not claim the constraints were satisfied, invents no listing or price, and may suggest widening the budget or the area. Saying 'none found' is the success condition her`
- fixture：`ho2_048_empty.json` ｜ reference_calculations：`无`

## HO2-079  (memory)

- 请求：`How many bedrooms did I tell you I needed?`
- 硬约束：`[]`
- 正确完成态：`Reads the stored memory and reports it back: 2 bedrooms. Correct behaviour here is NOT to produce listings — the user asked what was remembered. The answer must not add any preference the bucket does not contain.`
- fixture：`ho2_079_recall.json` ｜ reference_calculations：`无`
