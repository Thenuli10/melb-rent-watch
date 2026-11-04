import pandas as pd
from openpyxl import load_workbook

from src.report import build_excel


def sample_frames():
    rows = []
    for i, (suburb, region) in enumerate([("Footscray", "Outer Western Melbourne"), ("Carlton-Parkville", "Inner Melbourne")]):
        for ptype in ("2 bed flat", "3 bed house"):
            rows.append({
                "suburb_group_name": suburb, "region": region, "lga_name": "X", "property_type": ptype,
                "dwelling_type": ptype.split()[-1].title(), "bedrooms": int(ptype[0]),
                "median_weekly_rent": 500.0 + 100 * i, "new_bonds_count": 50, "is_reliable": True,
                "qoq_growth_pct": 1.0, "yoy_growth_pct": 4.0, "median_weekly_hh_income": 2000.0,
                "rent_to_income_pct": 25.0 + 5 * i,
                "affordability_band": "Moderate" if i == 0 else "Rental stress (30%+)",
                "nearest_station": "Footscray", "distance_to_station_km": 0.5 + i,
                "distance_band": "<1 km", "quarter_end": pd.Timestamp("2026-06-30"), "quarter_label": "2026 Q2",
            })
    latest = pd.DataFrame(rows)
    history = pd.DataFrame({"region": ["Inner Melbourne"] * 2, "property_type": ["2 bed flat"] * 2,
                            "quarter_end": pd.to_datetime(["2026-03-31", "2026-06-30"]),
                            "quarter_label": ["2026 Q1", "2026 Q2"], "avg_rent": [590.0, 600.0]})
    return latest, history


def test_excel_workbook_structure(tmp_path):
    latest, history = sample_frames()
    path = build_excel.build(latest, history, tmp_path / "summary.xlsx")
    wb = load_workbook(path)
    assert wb.sheetnames == ["Summary", "Rent by Region & Type", "Growth", "Top 20 Value Near Trains", "Notes",
                             "Data - Latest Quarter"]
    data = wb["Data - Latest Quarter"]
    assert data.max_row == len(latest) + 1
    # Formulas point at the full data range
    assert "$E$2:$E$5" in wb["Summary"]["B7"].value
    assert wb["Summary"]["B7"].value.startswith("=IFERROR(AVERAGEIFS(")
    # Percentages are stored as fractions (rows are sorted by region, so Inner Melbourne comes first)
    assert data["B2"].value == "Inner Melbourne"
    assert data["K2"].value == 0.30


def test_powerbi_measures_have_no_blank_traps():
    from src.report.build_powerbi import MEASURES
    from src.report.check_powerbi import blank_traps

    assert blank_traps(MEASURES) == []


def test_blank_trap_check_catches_the_rank_bug():
    from src.report.check_powerbi import blank_traps

    bad = {"Latest": [("In Top 15", "IF ( [Rank] <= 15, 1 )", "0")]}
    good = {"Latest": [("In Top 15", "VAR k = [Rank] RETURN IF ( NOT ISBLANK ( k ) && k <= 15, 1 )", "0")]}
    assert len(blank_traps(bad)) == 1
    assert blank_traps(good) == []


def test_powerbi_names_are_quoted():
    from src.report.build_powerbi import _q

    assert _q("Latest") == "Latest"
    assert _q("Model Metrics") == "'Model Metrics'"
    assert _q("% in Rental Stress") == "'% in Rental Stress'"
