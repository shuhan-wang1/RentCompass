from uk_rent_agent.config import Config
from uk_rent_agent.data.repository import PropertyRepository
from uk_rent_agent.domain.schema import RICH_COLUMNS


def test_repository_normalizes_schema_and_metadata(tmp_path):
    data = tmp_path / "local_data_demo" / "data"
    data.mkdir(parents=True)
    (data / "fake_property_listings.csv").write_text(
        ",".join(RICH_COLUMNS) + "\n"
        '"£1,200 pcm",1 Test Road London NW1 1AA,Flat,https://example.test,Now,Demo,[],'
        '"51.5, -0.1",Studio,Gym,None,Monthly,None,Description\n',
        encoding="utf-8",
    )
    cfg = Config(project_root=tmp_path, property_source="csv")
    result = PropertyRepository(cfg).load()
    assert result.source == "fake"
    assert result.csv_path.name == "fake_property_listings.csv"
    assert set(RICH_COLUMNS) <= set(result.properties[0])
    assert result.properties[0]["parsed_price"] == 1200
    assert result.properties[0]["postcode"] == "NW1 1AA"


def test_repository_auto_uses_scraped_cache(tmp_path):
    data = tmp_path / "local_data_demo" / "data"
    data.mkdir(parents=True)
    header = ",".join(RICH_COLUMNS) + "\n"
    (data / "fake_property_listings.csv").write_text(header, encoding="utf-8")
    scraped_row = (
        '£900 pcm,2 Test Road,Flat,https://example.test/2,Now,Demo,[],'
        '"51.5, -0.1",Studio,Gym,None,Monthly,None,Description\n'
    )
    (data / "scraped_property_listings.csv").write_text(header + scraped_row, encoding="utf-8")
    result = PropertyRepository(Config(project_root=tmp_path)).load()
    assert result.source == "scraped"
