import logging
import os

from uk_rent_agent.observability import JsonFormatter


def configure_logging() -> None:
    """Install one process-wide structured stderr handler.

    Uvicorn's ``module:factory`` path bypasses ``web.__main__``.  Keep this
    idempotent so both entry points (and repeated app factories in tests) may
    call it without stacking duplicate handlers or deleting foreign handlers.
    """
    root = logging.getLogger()
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root.setLevel(level)
    for existing in root.handlers:
        if getattr(existing, "_rentcompass_structured", False):
            existing.setLevel(level)
            return
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(level)
    setattr(handler, "_rentcompass_structured", True)
    root.addHandler(handler)
