"""RentCompass offline evaluation framework (Phase 3: additive instrumentation).

Everything in this package is *inert* unless evaluation capture is explicitly
activated (env ``RENTCOMPASS_EVAL=1`` or the :func:`evaluation.metrics.collector.capture_run`
context manager). Production code paths import from here defensively (try/except)
so a missing/oddly-configured ``evaluation`` package never breaks the app.
"""
