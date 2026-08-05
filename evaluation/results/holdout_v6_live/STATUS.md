# Held-out v6 live-run status

- Frozen benchmark commit: `82274a1`
- Frozen product commit: `523745d`
- Cases: 180 (`evaluation/benchmark/holdout_v6/cases_holdout_v6.jsonl`)
- Static preflight: passed (exit 0)
- Clean-checkout identity: `82274a1`, `git-clean`
- Formal run status: **aborted before completion**
- Reason: the configured DeepSeek endpoint returned HTTP 401 authentication failure on
  the first live case; the runner was stopped immediately rather than spending the
  remaining requests on a known-invalid credential.
- Metrics: none.  The partial attempt is not an evaluation result and must not enter any
  denominator, report, or CV claim.
- CV: unchanged.

The frozen cases and evaluator remain valid.  Once a valid credential is supplied, rerun
the exact command in `holdout_v6/FREEZE.json` from a clean checkout and analyze the
complete raw records with `analyze_holdout_v5.py` before considering any CV update.
