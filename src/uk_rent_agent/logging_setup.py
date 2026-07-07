import logging
import os

from uk_rent_agent.observability import JsonFormatter


def configure_logging() -> None:
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(), handlers=[handler], force=True
    )
