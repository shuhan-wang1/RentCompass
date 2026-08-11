# Local diagnostic golden sets

The tiny intent/retrieval golden sets in this directory are useful for unit-level
diagnostics. They do not execute the production graph and must not authorize a release.
`thresholds.json` is retained only for historical result interpretation; the
`uk-rent-eval-gate` command no longer consumes it.

Production `fc_loop` releases use the frozen dual-track v7 evidence protocol in
`evaluation/benchmark/holdout_v7/`. That gate checks identities and hashes, recomputes the
declared report, enforces at least three repeats with nested semantic/template-cluster
bootstrap, and exits non-zero on HOLD or BLOCK.
