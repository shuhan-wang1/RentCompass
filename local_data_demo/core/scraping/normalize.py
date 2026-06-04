"""
Normalisation: turn a sparse scraper dict into the rich 14-field schema the
RAG / agent pipeline expects, filling gaps where we can.

  - geo_location : taken from the source if present, else geocoded for free
                   (Postcodes.io / Nominatim via maps_service).
  - Room_Type_Category : inferred from text when the source doesn't provide it.
  - Enhanced_Description : composed from all available fields (this is the text
                           the FAISS index actually embeds, so it matters).

Also holds the CSV read/write helpers so the on-disk cache stays byte-compatible
with how the existing loader reads fake_property_listings.csv.
"""

import re
import ast
import pandas as pd

from .config import RICH_COLUMNS

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", str(text))).strip()


def infer_room_type(*texts: str) -> str:
    """Best-effort room/property type from free text (used when the source
    doesn't give a structured value, e.g. Zoopla)."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return ""
    if "studio" in blob:
        return "Studio"
    m = re.search(r"(\d+)\s*(?:bed|bedroom)", blob)
    if m:
        n = m.group(1)
        if "flat" in blob or "apartment" in blob:
            return f"{n} bed Flat"
        if "house" in blob:
            return f"{n} bed House"
        return f"{n} bed"
    if "en-suite" in blob or "ensuite" in blob:
        return "En-suite Room"
    if "room" in blob and ("share" in blob or "shared" in blob):
        return "Room (Shared)"
    return ""


def _geocode_fill(prop: dict) -> None:
    """Populate geo_location from the address via free geocoders, if missing."""
    if prop.get("geo_location"):
        return
    address = prop.get("Address")
    if not address:
        return
    try:
        # Lazy import: avoids pulling maps_service (pandas etc.) unless needed,
        # and dodges any import-order surprises.
        from ..maps_service import geocode_address
        geo = geocode_address(address)
        if geo and geo.get("lat") is not None and geo.get("lng") is not None:
            prop["geo_location"] = f"{geo['lat']}, {geo['lng']}"
    except Exception as e:  # geocoding is best-effort; never fatal
        print(f"  [normalize] geocode failed for {str(address)[:40]}: {e}")


def compose_enhanced_description(prop: dict) -> str:
    """Mirror the format of fake_property_listings.csv's Enhanced_Description so
    the embedding text is rich and consistent across real and fake data."""
    segments = []
    base = (prop.get("Description") or "").strip()
    if base:
        segments.append(base if base.endswith((".", "!", "?")) else base + ".")
    if prop.get("Room_Type_Category"):
        segments.append(f"Room Type: {prop['Room_Type_Category']}.")
    if prop.get("Detailed_Amenities"):
        segments.append(f"Amenities: {prop['Detailed_Amenities']}.")
    if prop.get("Guest_Policy"):
        segments.append(f"Guest Policy: {prop['Guest_Policy']}.")
    if prop.get("Payment_Rules"):
        segments.append(f"Payment: {prop['Payment_Rules']}.")
    if prop.get("Excluded_Features"):
        segments.append(f"Exclusions: {prop['Excluded_Features']}.")
    raw = _strip_html(prop.get("_raw_description"))
    if raw:
        segments.append(raw)
    return " ".join(s for s in segments if s).strip()


def normalize_property(prop: dict) -> dict:
    """Project an arbitrary scraper dict onto the canonical RICH_COLUMNS schema,
    filling missing fields (geo_location, Room_Type_Category, Enhanced_Description).
    Returns a fresh dict containing exactly RICH_COLUMNS."""
    out = dict(prop)

    out.setdefault("Platform", "")
    if not out.get("Images"):
        out["Images"] = []
    if not out.get("Available From"):
        out["Available From"] = "Contact agent"

    # Free-text-derived fields when the source left them blank.
    for col in (
        "Room_Type_Category",
        "Detailed_Amenities",
        "Guest_Policy",
        "Payment_Rules",
        "Excluded_Features",
    ):
        if out.get(col) is None:
            out[col] = ""
    if not out.get("Room_Type_Category"):
        out["Room_Type_Category"] = infer_room_type(
            out.get("Description", ""), _strip_html(out.get("_raw_description"))
        )

    _geocode_fill(out)
    if not out.get("geo_location"):
        out["geo_location"] = ""

    out["Enhanced_Description"] = compose_enhanced_description(out)

    return {col: out.get(col, "") for col in RICH_COLUMNS}


# ---------------------------------------------------------------------------
# CSV helpers — keep on-disk format identical to fake_property_listings.csv
# ---------------------------------------------------------------------------
def write_csv(properties: list[dict], path) -> None:
    df = pd.DataFrame(properties, columns=RICH_COLUMNS)
    df.to_csv(path, index=False)


def read_csv(path) -> list[dict]:
    df = pd.read_csv(path)
    # Empty cells -> '' (not NaN) so downstream string ops don't blow up.
    df = df.fillna("")
    if "Images" in df.columns:
        df["Images"] = df["Images"].apply(
            lambda x: ast.literal_eval(x)
            if isinstance(x, str) and x.strip().startswith("[")
            else ([] if x == "" else x)
        )
    return df.to_dict("records")
