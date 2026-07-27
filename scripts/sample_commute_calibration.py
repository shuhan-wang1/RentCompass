#!/usr/bin/env python3
"""Rebuild app/core/commute_basis.py's CALIBRATION table, or refit the model from it.

commute_basis refuses straight-line estimates outside a measured domain, corrects the ones
inside it, and puts a range around what it keeps. Every one of those numbers must be
reproducible rather than asserted, so this is the script that produced them.

    # SAMPLE: 14 live TfL Journey Planner calls. Metered third-party API — owner approval
    # required before running. This is the ONLY mode that touches the network.
    docker exec -i uk-rent-app python - < scripts/sample_commute_calibration.py

    # REFIT: no network at all. Re-runs the least-squares fit and the residual table off the
    # CALIBRATION rows already checked into commute_basis, so anyone can recompute the
    # shipped constants for free.
    python scripts/sample_commute_calibration.py --refit

WHAT THIS CAN AND CANNOT ESTABLISH
----------------------------------
The estimator only fires when TfL returned NO journey, i.e. outside TfL coverage — exactly
where no TfL reference exists to compare against. So every pair here is inside London, and
the table measures the estimator's FORMULA against real public-transport journeys rather
than validating it in the domain where it actually runs. Say so wherever the numbers are
used; do not quietly upgrade this to a validation.

Three further limits of this sample, all load-bearing for the model fitted from it:
  * It records only the journey TOTAL. TfL also returns per-leg mode/duration/distance, which
    would separate street-network detour from modal speed — the two factors that the fitted
    pace term currently lumps together. Keeping ``journey['legs']`` on a re-run would settle
    that at no extra request count.
  * Distances span 0.47-16.71 km only. Nothing outside that is calibrated, so nothing outside
    it is corrected.
  * Every reference journey is TfL's FASTEST itinerary, i.e. public transport or walking. This
    sample says nothing about driving or cycling.
"""
import json
import math
import subprocess
import sys
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


def sample():
    """The metered path: one TfL Journey Planner query per pair. Owner approval required."""
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
    print("\nPaste the rows above into commute_basis.CALIBRATION, sorted by distance,")
    print("then re-run with --refit to re-derive the model constants and the band.")


# --------------------------------------------------------------------------- #
# --refit: no network. Re-derives the shipped model from the checked-in table. #
# --------------------------------------------------------------------------- #

def _sse_log(f, rows):
    """Sum of squared log ratios. LOG because the error that matters here is multiplicative:
    being 10 minutes out on a 12-minute walk and on a 60-minute cross-town trip are not the
    same mistake, and a plain squared-minutes loss would let the long pairs drown the short
    ones — which is how the first estimator came to be 6x low at 0.5 km."""
    total = 0.0
    for _label, km, _legacy, tfl in rows:
        v = f(km)
        if v <= 0:
            return float("inf")
        total += math.log(v / tfl) ** 2
    return total


def _nelder_mead(fn, x0, iters=4000):
    n = len(x0)
    pts = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] = p[i] * 1.25 if p[i] else 0.25
        pts.append(p)
    vals = [fn(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(len(pts)), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        best, worst = pts[0], pts[-1]
        cen = [sum(p[i] for p in pts[:-1]) / n for i in range(n)]
        refl = [cen[i] + (cen[i] - worst[i]) for i in range(n)]
        fr = fn(refl)
        if fr < vals[0]:
            exp_ = [cen[i] + 2 * (cen[i] - worst[i]) for i in range(n)]
            fe = fn(exp_)
            pts[-1], vals[-1] = (exp_, fe) if fe < fr else (refl, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = refl, fr
        else:
            con = [cen[i] + 0.5 * (worst[i] - cen[i]) for i in range(n)]
            fc = fn(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                for i in range(1, len(pts)):
                    pts[i] = [best[j] + 0.5 * (pts[i][j] - best[j]) for j in range(n)]
                    vals[i] = fn(pts[i])
    order = sorted(range(len(pts)), key=lambda i: vals[i])
    return pts[order[0]], vals[order[0]]


def refit():
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
    from core import commute_basis as cb

    rows = cb.CALIBRATION
    n = len(rows)

    paces = [tfl / km for _l, km, _e, tfl in rows]
    print("--- what the sample says about the SHAPE of the error ---")
    print("observed door-to-door pace: %.2f min/km at %.2f km  ->  %.2f min/km at %.2f km"
          % (max(paces), rows[0][1], min(paces), rows[-1][1]))
    print("that is a %.2fx spread. A street-network detour factor spans about 1.2-1.6, so the"
          % (max(paces) / min(paces)))
    print("spread is modal (walk -> bus -> tube/rail), not geometric: no single multiplier and")
    print("no detour factor can produce 6.0x at 0.47 km and 0.75x at 16.71 km at once.\n")

    print("--- refitting t(d) = A + B * d**Q by least squares on log(model/tfl) ---")
    x, sse = _nelder_mead(
        lambda p: _sse_log(lambda km: p[0] + p[1] * km ** p[2] if p[1] > 0 and p[2] > 0 else -1, rows),
        [4.0, 11.0, 0.6])
    print("unrounded optimum   A=%.4f  B=%.4f  Q=%.4f   SSE_log=%.5f  RMS_log=%.4f"
          % (x[0], x[1], x[2], sse, math.sqrt(sse / n)))

    A, B, Q = cb.CAL_OVERHEAD_MINUTES, cb.CAL_PACE_COEFFICIENT, cb.CAL_DISTANCE_EXPONENT
    shipped = _sse_log(lambda km: A + B * km ** Q, rows)
    legacy = _sse_log(lambda km: float(cb.legacy_straight_line_minutes(km, "transit")), rows)
    print("shipped constants   A=%-6s B=%-6s Q=%-6s SSE_log=%.5f  RMS_log=%.4f"
          % (A, B, Q, shipped, math.sqrt(shipped / n)))
    print("legacy formula                                SSE_log=%.5f  RMS_log=%.4f\n"
          % (legacy, math.sqrt(legacy / n)))

    print("--- is the fixed-overhead term earning its place? ---")
    forms = {
        "legacy int(3.9d + min(10,2d))": (0, None, None),
        "B*d                (1 param)": (1, [7.5], lambda p: lambda km: p[0] * km),
        "A + B*d            (2 param)": (2, [11.0, 3.7], lambda p: lambda km: p[0] + p[1] * km),
        "B*d**Q             (2 param)": (2, [15.0, 0.49],
                                         lambda p: lambda km: p[0] * km ** p[1] if min(p) > 0 else -1),
        "A + B*d**Q         (SHIPPED)": (3, [4.0, 11.0, 0.6],
                                         lambda p: lambda km: p[0] + p[1] * km ** p[2] if p[1] > 0 and p[2] > 0 else -1),
    }
    print("%-30s %8s %7s %8s %11s  %s" % ("form", "SSE_log", "RMSlog", "AICc", "LOO-MSElog",
                                          "fitted params"))
    for name, (k, start, mk) in forms.items():
        if mk is None:
            f = lambda km: float(cb.legacy_straight_line_minutes(km, "transit"))
            s = _sse_log(f, rows)
            loo = s / n
            p = []
        else:
            p, s = _nelder_mead(lambda q: _sse_log(mk(q), rows), start)
            loo = 0.0
            for i in range(n):
                held = rows[i]
                sub = rows[:i] + rows[i + 1:]
                q, _ = _nelder_mead(lambda z: _sse_log(mk(z), sub), start, iters=1200)
                loo += math.log(mk(q)(held[1]) / held[3]) ** 2
            loo /= n
        kk = k + 1
        aicc = n * math.log(s / n) + 2 * kk + (2 * kk * (kk + 1)) / (n - kk - 1)
        print("%-30s %8.5f %7.4f %8.2f %11.5f  %s" % (
            name, s, math.sqrt(s / n), aicc, loo, [round(v, 4) for v in p]))
    print("AICc and leave-one-out mildly prefer B*d**Q, i.e. the sample does not identify the")
    print("overhead term. It ships anyway: a pure power law sends t -> 0 as d -> 0, which is the")
    print("original defect again if the distance floor is ever lowered. See commute_basis.\n")

    print("--- per-pair residuals for the SHIPPED constants ---")
    print("%-42s %6s %5s %6s %7s %5s %7s %-9s" %
          ("pair", "km", "tfl", "legacy", "err", "cal", "err", "band"))
    ratios = []
    for row in cb.calibration_residuals():
        band = cb.calibrated_band(row["calibrated_minutes"])
        ratios.append(row["calibrated_ratio"])
        print("%-42s %6.2f %5d %6d %7.3f %5s %7s %-9s %s" % (
            row["label"], row["km"], row["tfl_minutes"], row["legacy_minutes"],
            row["legacy_ratio"],
            row["calibrated_minutes"] if row["calibrated_minutes"] else "-",
            ("%.3f" % row["calibrated_ratio"]) if row["calibrated_ratio"] else "-",
            ("%d-%d" % band) if band else "-",
            "in" if band and band[0] <= row["tfl_minutes"] <= band[1] else "OUTSIDE"))
    print("\ntfl/calibrated over %d pairs: min=%.4f max=%.4f -> band rounded OUTWARD to "
          "[%.2f, %.2f]" % (n, min(ratios), max(ratios), cb.CALIBRATED_RATIO_LOW,
                            cb.CALIBRATED_RATIO_HIGH))
    worst = max(max(r, 1 / r) for r in ratios)
    print("worst absolute calibrated error %.3fx; pairs over the %.1fx suppression gate: %d"
          % (worst, cb.MAX_ACCEPTED_RESIDUAL_RATIO,
             sum(1 for r in ratios if max(r, 1 / r) > cb.MAX_ACCEPTED_RESIDUAL_RATIO)))
    print("refusal floor: calibrated domain %.2f-%.2f km, i.e. %d minutes at the shortest "
          "measured pair" % (cb.CALIBRATED_MIN_KM, cb.CALIBRATED_MAX_KM,
                             cb.MIN_CALIBRATED_ESTIMATE_MINUTES))


if "--refit" in sys.argv:
    refit()
else:
    sample()
