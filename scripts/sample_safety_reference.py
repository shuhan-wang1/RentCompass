#!/usr/bin/env python3
"""Rebuild app/core/safety_reference.py's REFERENCE_COUNTS.

The reference exists so a crime count can be ranked instead of being fed to an
un-normalised formula. It must be reproducible rather than asserted, so this is the script
that produced it. Run it inside a container that can reach data.police.uk:

    docker exec -i uk-rent-app python - < scripts/sample_safety_reference.py

A point that returns 0 rows is reported and then EXCLUDED by hand: zero means "nothing
published for this point/month", not "no crime", and letting it into the reference would
drag the scale toward an artefact.
"""
import json, time, urllib.request

MONTH = "2026-05"          # the latest month the API had on 2026-07-26
POINTS = {
    "Hackney Central, London": (51.5450, -0.0553),
    "Camden Town, London": (51.5392, -0.1426),
    "Peckham, London": (51.4739, -0.0693),
    "Brixton, London": (51.4613, -0.1156),
    "Stratford, London": (51.5416, -0.0034),
    "Shoreditch, London": (51.5245, -0.0781),
    "Bloomsbury, London": (51.5220, -0.1244),
    "South Kensington, London": (51.4941, -0.1738),
    "Richmond, London": (51.4613, -0.3037),
    "Wimbledon, London": (51.4214, -0.2064),
    "Selly Oak, Birmingham": (52.4415, -1.9366),
    "Fallowfield, Manchester": (53.4423, -2.2166),
    "Headingley, Leeds": (53.8206, -1.5757),
    "Clifton, Bristol": (51.4650, -2.6120),
}

out = {}
for name, (lat, lng) in POINTS.items():
    for attempt in range(3):
        try:
            url = (f"https://data.police.uk/api/crimes-street/all-crime"
                   f"?lat={lat}&lng={lng}&date={MONTH}")
            with urllib.request.urlopen(url, timeout=30) as r:
                out[name] = len(json.load(r))
            print(f"{name:28} {out[name]:6}")
            break
        except Exception as e:
            if attempt == 2:
                print(f"{name:28} FAILED {type(e).__name__}")
            time.sleep(3)
    time.sleep(1)

print("\nzero-row points to EXCLUDE:", [k for k, v in out.items() if v == 0])
print(json.dumps({k: v for k, v in out.items() if v > 0}, indent=1))
