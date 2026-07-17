"""
Cross-city contamination guard: street-name false positives vs real city rows
(Fix 3).

_MAJOR_CITIES contains tokens that are also common London street names
(york, reading, bath, durham, hull, derby, preston, exeter, lancaster, salford).
A match mid-address (a STREET) must NOT drop the row; only a match in a genuine
city position (last comma segment, or right before a UK postcode) counts.
"""

from core.scraping import on_demand


# --------------------------------------------------------------------------
# False positives: city token used as a STREET name in a London address.
# --------------------------------------------------------------------------
def test_york_way_street_not_dropped_for_london():
    # Google's real HQ address; "York Way" is a street, not the city of York.
    assert on_demand._wrong_city("6 Pancras Square, York Way, London N1C 4AG", "london") is False


def test_reading_road_street_not_dropped_for_london():
    assert on_demand._wrong_city("12 Reading Road, London SW1A 1AA", "london") is False


def test_bath_street_not_dropped_for_london():
    assert on_demand._wrong_city("5 Bath Street, London EC1V 9LB", "london") is False


def test_durham_road_street_not_dropped_for_london():
    assert on_demand._wrong_city("40 Durham Road, London N7 7DT", "london") is False


# --------------------------------------------------------------------------
# True positives: another major city sits in a real city position.
# --------------------------------------------------------------------------
def test_manchester_listing_dropped_for_london_last_segment():
    assert on_demand._wrong_city("Deansgate, Manchester", "london") is True


def test_manchester_listing_dropped_for_london_before_postcode():
    assert on_demand._wrong_city("10 Deansgate, Manchester M3 2EN", "london") is True


def test_london_listing_dropped_for_manchester():
    assert on_demand._wrong_city("Baker Street, London NW1", "manchester") is True


# --------------------------------------------------------------------------
# Guards / neutral cases unchanged.
# --------------------------------------------------------------------------
def test_local_suburb_kept():
    assert on_demand._wrong_city("High Street, Feltham", "london") is False


def test_unknown_requested_city_never_filters():
    assert on_demand._wrong_city("Anywhere, London", None) is False


def test_requested_city_named_in_position_is_kept():
    # A "Manchester Road" street in a genuine London row must survive.
    assert on_demand._wrong_city("Manchester Road, Isle of Dogs, London E14 3BD", "london") is False


def test_requested_city_row_kept_even_with_other_city_street():
    # Requested Manchester, a "London Road" street in Manchester -> keep.
    assert on_demand._wrong_city("London Road, Manchester M1 2PW", "manchester") is False


# --------------------------------------------------------------------------
# End-to-end through _clean: a York-Way London row survives the filter.
# --------------------------------------------------------------------------
def test_clean_keeps_york_way_london_row():
    rows = [
        {"Address": "6 Pancras Square, York Way, London N1C 4AG",
         "URL": "https://www.onthemarket.com/details/1/", "Price": "£2000 pcm"},
        {"Address": "10 Deansgate, Manchester M3 2EN",
         "URL": "https://www.onthemarket.com/details/2/", "Price": "£1500 pcm"},
    ]
    out = on_demand._clean(rows, "london")
    addrs = [r["Address"] for r in out]
    assert any("York Way" in a for a in addrs)          # kept
    assert not any("Manchester" in a for a in addrs)    # dropped
