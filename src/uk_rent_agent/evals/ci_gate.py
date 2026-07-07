from __future__ import annotations

import argparse
import json
from pathlib import Path

from uk_rent_agent.evals.metrics import EvalReport


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply metric floors to a JSON eval report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--thresholds", type=Path, default=Path("evals/thresholds.json"))
    args = parser.parse_args()
    report = EvalReport(json.loads(args.report.read_text(encoding="utf-8")))
    floors = json.loads(args.thresholds.read_text(encoding="utf-8"))
    if report.check(floors):
        print("eval gate passed")
        return 0
    print("eval gate failed: " + "; ".join(report.failures))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
