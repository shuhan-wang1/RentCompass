# Isolated tool and end-to-end audit helpers

These helpers support the independent persona audit recorded in
`evaluation/results/e2e_tools_2026_10_19/`. They are opt-in and do not alter the
production application or the default pytest suite.

`transport_fixtures.py` intercepts `requests.Session.send` at the upstream HTTP
boundary. Weather, TfL, Police, OSM and web-search responses are synthetic. Tool
implementations, input validation, orchestration, calculations and persistence
remain unchanged. Unknown upstreams fail closed. OnTheMarket requests are
explicitly rejected. Housing searches additionally require the audit boot
wrapper's cache-only producer and a frozen listing cache.

`instrumentation.py` records actual `Tool.execute` calls and their results. The
`ask_user` terminal path is handled directly by the FC executor, so its coverage
must also be read from completed-turn executor ledgers. Nested calls and outer
coordinator calls are separate observations; they must not be added together as
independent user tasks.

`contract_probe.py` exercises all 14 registered tools, all transport query modes,
parameter rejection, exact-detail misses, per-user memory, idempotent writes,
all six allowed web-search children and prohibited nested writes/recursion.
These are **tool integration probes**, not browser or model journey results.

`fault_probe.py` uses a separate `/audit/fault` cache and fixture-mode file to
exercise 503 responses, timeouts, empty evidence and recovery. It can run beside
persona journeys without changing their fixture modes or cached results.

The audit runs in a read-only Docker container with `--network none`, a frozen
source mount at `/work`, an isolated writable audit mount at `/audit`, and
`PYTHONPATH=/audit/harness:/work/src:/work/app:/work`. Copy these helper modules
into `/audit/harness`, supply only synthetic mutable databases and frozen
housing snapshots, then run:

```sh
python /audit/harness/contract_probe.py
python /audit/harness/fault_probe.py
```

The full run also uses a loopback browser proxy and a Unix-socket relay to the
existing configured model, with a bounded call/token budget. The archived run
scripts document that local setup; private model environment files and browser
cookies are excluded from published artifacts. Positive synthetic upstream
checks do not establish live upstream availability or current housing prices.

Interface success and task success are reported separately. The audit's
business assertions also check whether route time and fare belong to the same
itinerary, whether saved preferences are applied to searches, and whether final
answers preserve usable evidence. A completed audit may correctly report a
failing product acceptance gate.
