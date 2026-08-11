"""Console entry point for the production evaluation gate.

The previous command compared arbitrary JSON to ``evals/thresholds.json``.  Nothing in
the real graph benchmark emitted that shape, so a green result could not certify a
release.  The entry point now accepts only a sealed v7 preregistration + evidence
manifest and deterministically re-scores its declared artifacts.
"""
from __future__ import annotations

from uk_rent_agent.evals.evidence_v7 import cli_gate


def main() -> int:
    return cli_gate()


if __name__ == "__main__":
    raise SystemExit(main())
