import pandas as pd
import pytest

from src.common import SQL_DIR, load_config
from src.load.load_warehouse import parse_checks
from src.transform.geo_distance import clean_station_name, distance_band, nearest_station


def test_nearest_station_haversine():
    stations = pd.DataFrame({
        "station_name": ["Flinders Street", "Box Hill"],
        "latitude": [-37.818095, -37.819060],
        "longitude": [144.966266, 145.121820],
    })
    # Melbourne Central is ~1 km north of Flinders Street
    pts = pd.DataFrame({"latitude": [-37.809973], "longitude": [144.962513]})
    out = nearest_station(pts, stations)
    assert out.loc[0, "nearest_station"] == "Flinders Street"
    assert out.loc[0, "distance_to_station_km"] == pytest.approx(0.96, abs=0.05)


@pytest.mark.parametrize("km, band", [(0.2, "<1 km"), (1.0, "1–2 km"), (4.99, "2–5 km"), (12, ">5 km")])
def test_distance_band(km, band):
    assert distance_band(km) == band


def test_clean_station_name():
    assert clean_station_name("Jordanville Station") == "Jordanville"
    assert clean_station_name("Melton Railway Station") == "Melton"


def test_is_stale():
    from datetime import date

    from src.pipeline import is_stale

    assert not is_stale(date(2026, 6, 30), date(2026, 11, 3), 210)
    assert is_stale(date(2026, 6, 30), date(2027, 2, 1), 210)
    assert is_stale(None, date(2026, 11, 3), 210)


def test_quality_check_file_parses_and_formats():
    checks = parse_checks((SQL_DIR / "05_quality_checks.sql").read_text(encoding="utf-8"))
    names = [n for n, _ in checks]
    assert "rent_out_of_bounds" in names and "series_not_contiguous" in names
    assert len(names) == len(set(names))
    for _, query in checks:
        formatted = query.format(**load_config()["quality"])
        assert "{" not in formatted and "failures" in formatted
