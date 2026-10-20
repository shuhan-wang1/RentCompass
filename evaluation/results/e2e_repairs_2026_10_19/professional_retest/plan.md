# Independent black-box retest: English hybrid-working renter

Status: prepared; waiting for parent readiness notice. No application access or real dialogue has occurred.

## Scope and safeguards

- Test only the parent-provided localhost URL and public browser helper once ready.
- Do not read product source, previous reports, or other tester evidence. Do not edit the product.
- Abort every non-localhost browser request. Never open listing external links, OnTheMarket, or any other external website. Do not access production ports 5001, 5002, or 8080.
- Maximum ten genuine model conversations. Preserve exact requests, responses, screenshots, observed state, and timestamps under this directory.
- Treat rental listings as historical frozen snapshots, and weather/TfL/Police/OSM/web responses as synthetic HTTP fixtures. Fixture correctness is not proof of current real-world correctness.
- Use refresh, separate conversations, and a parent-coordinated genuine service restart to simulate persistence; do not claim a restart happened unless observed or confirmed.

## Persona

An English-speaking professional moving alone to London, seeking an entire self-contained home rather than a room, with a single monthly budget covering rent and office commuting. Office near Bank station. Starts at three office days a week, then changes to one and later five. Wants practical, evidence-backed decisions and durable favourites.

## Planned genuine user turns (adapt only to actual results)

1. "I'm moving to London on my own and need a self-contained whole flat, not a room in a shared place. My office is near Bank station and I'll be there three days a week. My maximum is £2,200 a month including rent and my commute. Can you find a few suitable options?"
   - Check entire-home filtering, office destination, three-day frequency, total-cost interpretation, and acknowledgement of any missing input.
2. "Please compare the best three. Show the monthly rent, estimated monthly commuting cost, combined total, and journey time for each, and explain how you calculated the commute budget."
   - Check arithmetic, units, assumptions (weeks/month, return trips, time of travel), and actual evidence versus generic estimates.
3. "My employer has changed the plan: I only need to go to Bank one day a week now. Keep the same £2,200 total budget. Does that change which flat you recommend?"
   - Check frequency update, retained constraints, recomputation, and recommendation logic.
4. "Tell me more about [one exact returned listing]: the full rent, deposit or fees if known, furnishing, availability, and anything missing from the listing that I should check before viewing."
   - Use a real returned identifier/title; check listing consistency and explicit unknowns without invented details.
5. "For that flat, show me the actual public-transport journey to Bank for arriving at 9 am on a weekday, the adult fare, whether pay-as-you-go or a Travelcard makes more sense for one office day a week, and any line disruptions. Please distinguish actual results from estimates."
   - Check journey/fare/Travelcard/line-status integrations, evidence, date/time assumptions, and no unsupported claims about current reality.
6. "Now I may need to be in the office five days a week instead. Recalculate rent plus travel and compare pay-as-you-go with the relevant monthly Travelcard. Does this flat still fit £2,200?"
   - Check five-day update and all-in affordability, card relevance, arithmetic, caps and unknowns.
7. "What would everyday life near this flat be like? Please look up nearby supermarkets, a gym and somewhere useful for coffee or lunch, with walking distances and sources."
   - Exercise web/OSM fixtures through localhost only; check concrete useful amenities, grounding, and no external navigation.
8. "Save this flat as a favourite, with a note that I'm comparing the five-day commute to Bank and a £2,200 rent-plus-travel budget."
   - Check actual persisted save action, listing identity and user note; use UI if chat action is unsupported and record distinction.
9. After refresh and a separate conversation/session: "Which flat did I save, and what budget and office commute was I comparing it against?"
   - Check favourite recovery separately from conversation memory. Inspect UI/state through permitted local mechanisms. Coordinate restart before recovery if parent can provide it.
10. "Reopen my saved flat and give me a short decision summary: monthly rent, five-day monthly travel cost, total against £2,200, journey to Bank, and any important facts you couldn't verify."
   - Check saved identity, retained versus explicitly re-established context, internal consistency, honest limitations, and useful decision support.

## Non-model observations

Capture page load, error UI, session identifiers, visible history, favourites and notes before/after refresh/session switch/service restart. Local read-only API checks may corroborate state but must not substitute for observing user-facing behaviour. No additional model turns beyond ten.

## Report format

For each check: PASS/FAIL/UNVERIFIED, severity, expected versus actual, exact turn/action, evidence paths. Separate product behaviour, fixture validation, and real-world limitations. Report obstacles and incomplete checks without inferring success.
