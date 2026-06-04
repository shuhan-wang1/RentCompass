import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

print("== 1. import package ==")
from core.scraping import provider, normalize, rightmove, config
from core.scraping.config import RICH_COLUMNS
print("RICH_COLUMNS:", RICH_COLUMNS)
print("FAKE_CSV exists:", config.FAKE_CSV.exists())
print("SCRAPPER_DIR exists:", config.SCRAPPER_DIR.exists())

print("\n== 2. PAGE_MODEL parser ==")
sample_html = """
<html><head><script>
window.PAGE_MODEL = {"propertyData": {"bedrooms": 0, "propertySubType": "Apartment",
"keyFeatures": ["Furnished", "Concierge", "Gym"],
"location": {"latitude": 51.5237, "longitude": -0.1585},
"text": {"description": "<p>Lovely <b>studio</b> near campus.</p>"},
"lettings": {"letAvailableDate": "01/09/2026", "deposit": 2300, "furnishType": "Furnished",
"letType": "Long term", "minimumTermInMonths": 12}, "councilTaxBand": "C",
"images": [{"url": "https://img/1.jpg"}]}};
</script></head></html>
"""
pm = rightmove._extract_page_model(sample_html)
assert pm is not None, "PAGE_MODEL not parsed!"
rich = rightmove._rich_from_page_model(pm)
print("geo_location:", rich.get("geo_location"))
print("Room_Type_Category:", rich.get("Room_Type_Category"))
print("Detailed_Amenities:", rich.get("Detailed_Amenities"))
print("Available From:", rich.get("Available From"))
print("Payment_Rules:", rich.get("Payment_Rules"))
print("detail_images:", rich.get("_detail_images"))
assert rich["geo_location"] == "51.5237, -0.1585"
assert rich["Room_Type_Category"] == "Studio Apartment"
assert "Gym" in rich["Detailed_Amenities"]

print("\n== 3. normalize (geo provided -> no network) ==")
sparse = {
    "Price": "£1,800 pcm", "Address": "1 Test Street, London WC1E 6BT",
    "Description": "Apartment", "URL": "https://x/1", "Platform": "Rightmove",
    "geo_location": "51.5237, -0.1585", "Detailed_Amenities": "Gym, Concierge",
    "Room_Type_Category": "Studio Apartment",
    "_raw_description": "<p>Lovely studio near campus.</p>",
}
norm = normalize.normalize_property(sparse)
assert set(norm.keys()) == set(RICH_COLUMNS), set(norm.keys()) ^ set(RICH_COLUMNS)
print("keys OK; columns ==", len(norm))
print("Enhanced_Description:", norm["Enhanced_Description"][:120], "...")
assert "Room Type:" in norm["Enhanced_Description"]
assert "Amenities:" in norm["Enhanced_Description"]
assert norm["Images"] == []

print("\n== 4. CSV roundtrip ==")
import tempfile, os
tmp = os.path.join(tempfile.gettempdir(), "_rt_test.csv")
recs = [dict(norm)]
recs[0]["Images"] = ["https://img/1.jpg", "https://img/2.jpg"]
normalize.write_csv(recs, tmp)
back = normalize.read_csv(tmp)
print("roundtrip rows:", len(back), "Images type:", type(back[0]["Images"]).__name__)
assert back[0]["Images"] == ["https://img/1.jpg", "https://img/2.jpg"]
os.remove(tmp)

print("\n== 5. load_properties auto-mode fallback (no cache, no scrape) ==")
os.environ["PROPERTY_SOURCE"] = "auto"
os.environ["SCRAPE_ON_STARTUP"] = "0"
from core.data_loader import load_properties
props = load_properties()
print("loaded:", len(props), "properties")
assert len(props) > 0
sample = props[0]
print("sample keys present:", all(c in sample for c in ["Address", "Price", "Enhanced_Description"]))

print("\nALL SELFTESTS PASSED")
