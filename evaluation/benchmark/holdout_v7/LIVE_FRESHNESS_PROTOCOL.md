# v7 live-freshness capture protocol

This track is operational evidence and must never be pooled with the deterministic
fixture track. It requires network access and therefore is intentionally not executed by
offline tests or by repository review.

1. Freeze `PREREGISTRATION.json` before the first request. Bind full 40-character
   product/capture/evaluator commits, the immutable deployed OCI image digest, every
   production prompt ID/version/file hash, and the exact model/tool policy files.
2. Build a fresh `fc_loop` image from `product_sha`. Record the digest returned by the
   registry; mutable tags are not evidence. Verify the public response advertises the
   same architecture and release identity before capture.
3. Start from the frozen live-freshness case set. Each case must name a
   `semantic_cluster` and `template_cluster`. Run every case three times. Do not reuse
   frozen listing fixtures, deterministic cache snapshots, or the fixture track's output.
4. For every recommended listing, capture the source URL, source retrieval timestamp,
   source availability/status, price, and the response's provenance. Independently
   revisit the source within the preregistered 24-hour window. A blocked/unreachable
   source is unknown and cannot be counted as fresh.
5. Emit one `rentcompass/eval-run/v7` row per case/repeat. Copy the exact run binding
   produced by `run_binding_from_prereg`; do not infer it from a branch name. Record
   violations even when the response boundary later replaces or blocks the body.
6. Stop and retain the partial package on identity drift, image restart, prompt/policy
   drift, missing request, unknown tool result, or capture-age breach. Partial evidence is
   a HOLD, never a reduced denominator.
7. Generate `release_report.json` with the offline command in `README.md`, seal all file
   hashes in `manifest.json`, and run `uk-rent-eval-gate`. Only exit 0 authorizes release.

Secrets, raw user identifiers, cookies, and full unredacted production logs must not be
placed in the evidence package.
