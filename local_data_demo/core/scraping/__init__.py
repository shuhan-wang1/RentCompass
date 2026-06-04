"""
Real property-source layer for local_data_demo.

Bridges the local scrapers in ``scrapped_data_demo/scrapper`` (Rightmove +
Zoopla) into the rich property schema the RAG / agent pipeline expects, and
serves them through a hybrid cache (TTL) with automatic fallback to the bundled
fake CSV so demos never break.

Public entry points:
    provider.get_properties(...)       -> list[dict] (rich schema)
    provider.get_active_property_csv() -> Path of the CSV currently serving data
"""

from .provider import get_properties, get_active_property_csv, scrape_all  # noqa: F401
