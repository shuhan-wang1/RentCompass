"""Replace a <!--MARKER--> in the report with the contents of a file. Keeps every number
in the report traceable to the artefact that produced it (no hand transcription)."""
import sys
from pathlib import Path

report, marker, src = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
text = report.read_text(encoding="utf-8")
token = f"<!--{marker}-->"
if token not in text:
    raise SystemExit(f"marker {token} not found in {report}")
report.write_text(text.replace(token, src.read_text(encoding="utf-8").rstrip("\n")),
                  encoding="utf-8")
print(f"spliced {marker} <- {src}")
