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

Use the `uk_rent` conda environment (it has the runtime deps: chromadb,
sentence-transformers, faiss-cpu, langgraph, etc.):

```bash
conda run -n uk_rent python -m pytest -q
```

Or, from an activated env / editable install:

```bash
pip install -e ".[dev]"    # runtime deps + pytest, pytest-asyncio, import-linter
python -m pytest -q
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

A handful of tests exercise real external services and are **env-gated off** by
default, so they skip unless you explicitly enable them:

- `RUN_LIVE_OSM` — live OpenStreetMap / Overpass POI + geocoding calls.
- `RUN_LIVE_SCRAPE` — live OnTheMarket scraping.

Leave these unset for normal and CI runs. Set them only when deliberately
smoke-testing the live integrations.

## CI

`.github/workflows/ci.yml` runs the full suite (both trees) on every push and
pull request to `main`, on Python 3.11, with `PYTHONIOENCODING=utf-8`. The live
gates stay unset, so CI touches no network. A second job runs `gitleaks` secret
scanning against the working tree only (not full git history).
