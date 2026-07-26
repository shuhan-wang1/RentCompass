#!/usr/bin/env python3
"""Rebuild app/core/commute_basis.py's CALIBRATION table.

commute_basis refuses straight-line estimates below a floor and puts a range around the
ones it keeps. Those two numbers must be reproducible rather than asserted, so this is the
script that produced them: for each pair it computes what maps_service's straight-line
estimator would say, then asks the TfL Journey Planner what the journey really takes.

    docker exec -i uk-rent-app python - < scripts/sample_commute_calibration.py

WHAT THIS CAN AND CANNOT ESTABLISH
----------------------------------
The estimator only fires when TfL returned NO journey, i.e. outside TfL coverage — exactly
where no TfL reference exists to compare against. So every pair here is inside London, and
the table measures the estimator's FORMULA against real public-transport journeys rather
than validating it in the domain where it actually runs. Say so wherever the numbers are
used; do not quietly upgrade this to a validation.
"""
import json
import math
import subprocess
import time

# (label, (origin lat, lng), (destination lat, lng))
PAIRS = [
    ("Tavistock Court WC1H -> UCL Gower Street", (51.5245, -0.1272), (51.5246, -0.1340)),
    ("Woburn Place WC1H -> UCL Gower Street", (51.5239, -0.1268), (51.5246, -0.1340)),
    ("Bloomsbury WC1H -> KCL Strand", (51.5245, -0.1272), (51.5115, -0.1160)),
    ("Camden NW1 -> UCL Gower Street", (51.5390, -0.1426), (51.5246, -0.1340)),
    ("Islington N1 0RW -> UCL Gower Street", (51.5350, -0.1080), (51.5246, -0.1340)),
    ("Shoreditch EC2A 3DU -> KCL Strand", (51.5260, -0.0800), (51.5115, -0.1160)),
    ("Hackney E8 -> KCL Strand", (51.5450, -0.0553), (51.5115, -0.1160)),
    ("Bow E3 2QB -> UCL Gower Street", (51.5290, -0.0250), (51.5246, -0.1340)),
    ("Peckham SE15 -> UCL Gower Street", (51.4700, -0.0690), (51.5246, -0.1340)),
    ("Canary Wharf E14 -> UCL Gower Street", (51.5054, -0.0235), (51.5246, -0.1340)),
    ("Stratford E15 -> UCL Gower Street", (51.5416, -0.0035), (51.5246, -0.1340)),
    ("Wembley HA9 -> UCL Gower Street", (51.5560, -0.2800), (51.5246, -0.1340)),
    ("Richmond TW9 -> UCL Gower Street", (51.4613, -0.3037), (51.5246, -0.1340)),
    ("Croydon CR0 -> UCL Gower Street", (51.3760, -0.0980), (51.5246, -0.1340)),
]


def straight_line(o, d):
    """The estimator in maps_service.estimate_travel_time_simple, transit mode."""
    R = 6371.0
    dlat = math.radians(d[0] - o[0])
    dlng = math.radians(d[1] - o[1])
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(o[0])) * math.cos(math.radians(d[0])) * math.sin(dlng / 2) ** 2)
    km = R * 2 * math.asin(math.sqrt(a))
    minutes = int((km * 1.3 / 20.0) * 60 + min(10, km * 2))
    return round(km, 2), minutes


def tfl_minutes(o, d):
    """Fastest TfL Journey Planner itinerary in minutes, or None. Flaky API — retried."""
    url = (f"https://api.tfl.gov.uk/Journey/JourneyResults/"
           f"{o[0]},{o[1]}/to/{d[0]},{d[1]}")
    for attempt in range(4):
        try:
            body = subprocess.run(["curl", "-s", "--max-time", "30", url],
                                  capture_output=True, text=True).stdout
            journeys = [j for j in (json.loads(body).get("journeys") or [])
                        if j.get("duration") is not None]
            if journeys:
                return min(int(j["duration"]) for j in journeys)
        except Exception:
            pass
        time.sleep(2 + attempt * 2)
    return None


rows = []
for label, o, d in PAIRS:
    km, est = straight_line(o, d)
    real = tfl_minutes(o, d)
    rows.append((label, km, est, real))
    print(f"{label:44} km={km:6.2f} est={est:3d} tfl={real}")
    time.sleep(0.6)

paired = [(e, r) for _, _, e, r in rows if r]
short = [(e, r) for e, r in paired if e < 15]
long_ = [(e, r) for e, r in paired if e >= 15]
if short:
    print("\nestimate < 15 min  -> ratio tfl/est min=%.2f max=%.2f  (the refusal floor)"
          % (min(r / e for e, r in short), max(r / e for e, r in short)))
if long_:
    print("estimate >= 15 min -> ratio tfl/est min=%.2f max=%.2f  (the reported band)"
          % (min(r / e for e, r in long_), max(r / e for e, r in long_)))
print("\nPaste the rows above into commute_basis.CALIBRATION, sorted by distance.")
