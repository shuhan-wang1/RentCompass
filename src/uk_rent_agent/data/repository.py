from __future__ import annotations

import ast
import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from uk_rent_agent.config import Config
from uk_rent_agent.data.parsing import extract_postcode, parse_price
from uk_rent_agent.domain.schema import RICH_COLUMNS


@dataclass(frozen=True)
class LoadResult:
    properties: list[dict]
    source: str
    csv_path: Path
    is_stale: bool


class PropertyRepository:
    """Single loading boundary for fake and scraped property data."""

    def __init__(self, config: Config, refresh: Callable[[], object] | None = None):
        self._config = config
        self._refresh = refresh
        self._cache: LoadResult | None = None

    @property
    def fake_path(self) -> Path:
        return self._config.data_dir / "fake_property_listings.csv"

    @property
    def scraped_path(self) -> Path:
        return self._config.data_dir / "scraped_property_listings.csv"

    def _is_stale(self, path: Path) -> bool:
        if not path.exists():
            return True
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        return age_hours > self._config.scraper_cache_ttl_hours

    def active_csv_path(self) -> Path:
        if self._config.property_source == "csv":
            return self.fake_path
        if self.scraped_path.exists():
            return self.scraped_path
        return self.fake_path

    def load(self, *, force_refresh: bool = False) -> LoadResult:
        if self._cache is not None and not force_refresh:
            return self._cache
        if force_refresh and self._refresh is not None:
            self._refresh()
        path = self.active_csv_path()
        source = "scraped" if path == self.scraped_path else "fake"
        rows = self._read(path)
        if not rows and path != self.fake_path:
            path, source, rows = self.fake_path, "fake", self._read(self.fake_path)
        self._cache = LoadResult(rows, source, path, self._is_stale(path))
        return self._cache

    def get_by_address(self, address: str) -> dict | None:
        needle = address.casefold().strip()
        if not needle:
            return None
        for item in self.load().properties:
            candidate = str(item.get("Address", "")).casefold()
            if needle in candidate or candidate in needle:
                return item
        return None

    def _read(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [self._normalize_keys(row) for row in csv.DictReader(handle)]

    @staticmethod
    def _normalize_keys(row: dict) -> dict:
        lookup = {str(key).casefold().replace(" ", "_"): value for key, value in row.items()}
        normalized: dict = {}
        for name in RICH_COLUMNS:
            key = name.casefold().replace(" ", "_")
            normalized[name] = lookup.get(key, "")
        images = normalized["Images"]
        if isinstance(images, str) and images.strip().startswith("["):
            try:
                images = ast.literal_eval(images)
            except (ValueError, SyntaxError):
                images = []
        normalized["Images"] = images if isinstance(images, list) else []
        normalized["parsed_price"] = parse_price(normalized["Price"])
        normalized["postcode"] = extract_postcode(normalized["Address"])
        return normalized
