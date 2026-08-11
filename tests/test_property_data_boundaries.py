from dataclasses import replace
from pathlib import Path

from uk_rent_agent.config import Config
from uk_rent_agent.data.repository import PropertyRepository


def _write_csv(path: Path, address: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Price,Address,Description,URL,Available From,Platform,Images,geo_location,"
        "Room_Type_Category,Detailed_Amenities,Enhanced_Description\n"
        f"£1,200 pcm,{address},Real listing,https://example.test/a,Now,Provider,[],[],Flat,,\n",
        encoding="utf-8",
    )


def test_auto_mode_never_silently_uses_bundled_demo_rows(tmp_path):
    config = Config(project_root=tmp_path, property_source="auto")
    _write_csv(config.data_dir / "fake_property_listings.csv", "Demo Street")

    result = PropertyRepository(config).load()

    assert result.source == "none"
    assert result.csv_path is None
    assert result.properties == []
    assert result.is_stale is True


def test_csv_mode_is_an_explicit_demo_opt_in(tmp_path):
    config = Config(project_root=tmp_path, property_source="csv")
    _write_csv(config.data_dir / "fake_property_listings.csv", "Demo Street")

    result = PropertyRepository(config).load()

    assert result.source == "fake"
    assert result.properties[0]["_data_source"] == "fake"
    assert result.properties[0]["_data_observed_at"] == result.observed_at


def test_scraped_snapshot_preserves_staleness_and_provenance(tmp_path):
    config = replace(
        Config(project_root=tmp_path),
        property_source="scraper",
        scraper_cache_ttl_hours=-1,
    )
    _write_csv(config.data_dir / "scraped_property_listings.csv", "Real Street")

    result = PropertyRepository(config).load()

    assert result.source == "scraped"
    assert result.is_stale is True
    assert result.observed_at
    assert result.properties[0]["_data_is_stale"] is True


def test_scrape_capacity_exhaustion_is_incomplete_without_dispatch(monkeypatch):
    from core.scraping import on_demand

    class NoCapacity:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    monkeypatch.setattr(on_demand, "_SCRAPE_SLOTS", NoCapacity())
    monkeypatch.setattr(
        on_demand.onthemarket,
        "find_rich_onthemarket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    rows, incomplete = on_demand._scrape_live("london", 0, 2, 100, 5000, 15, 0.01)

    assert rows is None
    assert incomplete is True
