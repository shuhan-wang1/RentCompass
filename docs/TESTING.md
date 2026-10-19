# Testing

The suite lives in **two test trees**, both run by default (`pyproject.toml`
`testpaths = ["tests", "tests_refactor"]`).

## The two trees

| Tree | Targets | How imports resolve |
| --- | --- | --- |
| `tests/` | The **live runtime** under `app/` (the flat `core` / `rag` / `config` modules). | `tests/conftest.py` pins `src/` then `app/` onto the front of `sys.path` (app ends up first). `pyproject` also sets `pythonpath = ["src", "app", "."]`. |
| `tests_refactor/` | The installable **`uk_rent_agent` package** under `src/`. | Resolved from `src/` (via `pythonpath` / an editable install). |

Both trees are hermetic: all LLM and network calls are stubbed. Nothing hits an
external API during a normal run.

## Running the tests

The 2026-10-19 persona regressions are part of the default suite:
`test_revocable_auth.py`, `test_persona_constraint_regressions.py`, and
`test_persona_behavior_regressions.py`. They cover copied-session revocation,
explicit changes versus property questions, minimum bedrooms, whole homes,
unknown qualification, listing identity, and empty-result recovery across replay
and restart. Search-pipeline tests also cover weekly budget normalization and
unverified cat permission. Run both test trees with the default command below.

The independent local browser/API evidence and network isolation limits are recorded
in [the repair report](../evaluation/results/persona_2026_10_19/FIX_REPORT.md).

Create the same Python 3.12, hash-locked environment used by CI:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-bootstrap.lock
.venv/bin/python -m pip install --require-hashes -r requirements-production.lock
.venv/bin/python -m pip install --require-hashes -r requirements-ci.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Do **not** install the `[finetune]` extra (torch/torchvision/transformers/peft/
accelerate) to run tests — it is training-only and unnecessary here.

### Windows gotcha: `PYTHONIOENCODING`

Windows consoles default to gbk. The suite emits non-ASCII output (Chinese place
names). On Windows set UTF-8 first, or tests can fail on an encode error:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m pytest -q
```

## Live tests (opt-in, OFF by default)

The request-isolation and async chat regressions can be run without external APIs:

```bash
python -m pytest -q tests/test_listing_retrieval_isolation.py \
  tests/test_fix_memory_embeddings.py tests/test_native_chat_transport.py \
  tests/test_evaluation_verdicts.py
python -m evaluation.fault_injection.run --out /tmp/rentcompass-fault-check
```

Use an executable pytest temporary directory (`--basetemp`) when testing inside
a hardened container: deployment tests create executable shell fixtures and
cannot run those fixtures on a `noexec` `/tmp` mount.

A handful of tests exercise real external services and are **env-gated off** by
default, so they skip unless you explicitly enable them:

- `RUN_LIVE_OSM` — live OpenStreetMap / Overpass POI + geocoding calls.
- `RUN_LIVE_SCRAPE` — live OnTheMarket scraping.

Leave these unset for normal and CI runs. Set them only when deliberately
smoke-testing the live integrations.

## CI

`.github/workflows/ci.yml` has four required jobs on every push to `main` and
pull request into `main` or `telemetry/**`: both test trees run twice on Python
3.12 with isolated state and different random seeds; Compose builds and probes
the hardened production image; the fc_loop evaluator runs an offline smoke; and
the supply job runs the pinned secret/dependency/SBOM gates. Normal pytest and
the evaluator smoke do not call live application providers. Image/dependency
installation and the vulnerability gate do use their registries/databases.
