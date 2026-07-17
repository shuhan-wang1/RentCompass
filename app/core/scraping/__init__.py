"""
Real property-source layer for app.

Bridges the property scrapers (OnTheMarket primary, optional Zoopla via the
vendored ``legacy_scrapers/`` loader) into the rich property schema the RAG /
agent pipeline expects, and serves them through a hybrid cache (TTL) with
automatic fallback to the bundled fake CSV so demos never break.

Public entry points:
    provider.get_properties(...)       -> list[dict] (rich schema)
    provider.get_active_property_csv() -> Path of the CSV currently serving data
"""

from .provider import get_properties, get_active_property_csv, scrape_all  # noqa: F401
