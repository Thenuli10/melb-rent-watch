"""Build the Excel stakeholder summary from the analytics views.

The plan's Power Query refresh needs desktop Excel; Excel on the web can't
connect to PostgreSQL. So the pipeline regenerates this workbook after every
load instead. Headline and matrix numbers are Excel formulas over the data
sheets, so they stay traceable and recalculate if a stakeholder filters or
edits the data.

    python -m src.report.build_excel
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.table import Table, TableStyleInfo

from src.common import ROOT, get_engine, load_config, log

OUTPUT = ROOT / "excel" / "stakeholder_summary.xlsx"

FONT = "Arial"
NAVY = "1F3A5F"
INPUT_FILL = PatternFill("solid", fgColor="FFFF00")
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
BAND_FILL = PatternFill("solid", fgColor="EEF2F7")
THIN = Side(style="thin", color="C9D1DC")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)

TYPE_ORDER = ["1 bed flat", "2 bed flat", "3 bed flat", "2 bed house", "3 bed house", "4 bed house"]
VALUE_MAX_KM = 1.5
VALUE_TOP_N = 20
HISTORY_QUARTERS = 20

LATEST_SQL = """
SELECT suburb_group_name, region, lga_name, property_type, dwelling_type, bedrooms,
       median_weekly_rent, new_bonds_count, is_reliable, qoq_growth_pct, yoy_growth_pct,
       median_weekly_hh_income, rent_to_income_pct, affordability_band,
       nearest_station, distance_to_station_km, distance_band, quarter_end, quarter_label
FROM analytics.v_affordability
WHERE quarter_end = (SELECT max(quarter_end) FROM analytics.v_affordability)
  AND median_weekly_rent IS NOT NULL
"""

HISTORY_SQL = """
SELECT region, property_type, quarter_end, quarter_label, avg(median_weekly_rent) AS avg_rent
FROM analytics.v_affordability
WHERE median_weekly_rent IS NOT NULL AND is_reliable
  AND quarter_end > (SELECT max(quarter_end) FROM analytics.v_affordability) - interval '%s months'
GROUP BY region, property_type, quarter_end, quarter_label
"""


# ---------------------------------------------------------------- helpers

def _font(**kw) -> Font:
    return Font(name=FONT, **kw)


def _style_header(ws, row: int, first_col: int, last_col: int) -> None:
    for c in range(first_col, last_col + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BOX


def _title(ws, text: str, subtitle: str | None = None) -> None:
    ws["A1"] = text
    ws["A1"].font = _font(bold=True, size=16, color=NAVY)
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = _font(italic=True, size=10, color="555555")


def _widths(ws, widths: dict[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _apply_font_everywhere(wb: Workbook) -> None:
    """Default any unstyled cell to Arial, keeping explicit styling."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.font is None or cell.font.name != FONT:
                    f = cell.font
                    cell.font = Font(name=FONT, size=f.size, bold=f.bold, italic=f.italic, color=f.color)


# ---------------------------------------------------------------- data

def load_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    engine = get_engine()
    latest = pd.read_sql(LATEST_SQL, engine, parse_dates=["quarter_end"])
    history = pd.read_sql(HISTORY_SQL % (3 * HISTORY_QUARTERS), engine, parse_dates=["quarter_end"])
    for c in ("median_weekly_rent", "qoq_growth_pct", "yoy_growth_pct", "median_weekly_hh_income",
              "rent_to_income_pct", "distance_to_station_km"):
        latest[c] = latest[c].astype(float)
    history["avg_rent"] = history["avg_rent"].astype(float)
    return latest, history


# ---------------------------------------------------------------- sheets

DATA_COLUMNS = [
    # header, source column, number format, width
    ("Suburb group", "suburb_group_name", None, 34),
    ("Region", "region", None, 24),
    ("LGA", "lga_name", None, 18),
    ("Property type", "property_type", None, 13),
    ("Median rent ($/wk)", "median_weekly_rent", '"$"#,##0', 12),
    ("New bonds (12 mths)", "new_bonds_count", "#,##0", 11),
    ("Reliable (10+ bonds)", "is_reliable", None, 11),
    ("QoQ growth", "qoq_growth", "0.0%", 10),
    ("YoY growth", "yoy_growth", "0.0%", 10),
    ("Household income, indexed ($/wk)", "median_weekly_hh_income", '"$"#,##0', 14),
    ("Rent to income", "rent_to_income", "0.0%", 10),
    ("Affordability band", "affordability_band", None, 20),
    ("Nearest station", "nearest_station", None, 20),
    ("Distance to station (km)", "distance_to_station_km", "0.00", 11),
    ("Distance band", "distance_band", None, 11),
]
COL = {src: get_column_letter(i + 1) for i, (_, src, _, _) in enumerate(DATA_COLUMNS)}
DATA_SHEET = "Data - Latest Quarter"


def _rng(src: str, n_rows: int) -> str:
    """Absolute range on the data sheet for a source column, e.g. 'Data - Latest Quarter'!$E$2:$E$640."""
    c = COL[src]
    return f"'{DATA_SHEET}'!${c}$2:${c}${n_rows + 1}"


def write_data_sheet(wb: Workbook, latest: pd.DataFrame) -> int:
    ws = wb.create_sheet(DATA_SHEET)
    df = latest.copy()
    df["qoq_growth"] = df["qoq_growth_pct"] / 100
    df["yoy_growth"] = df["yoy_growth_pct"] / 100
    df["rent_to_income"] = df["rent_to_income_pct"] / 100
    df["is_reliable"] = df["is_reliable"].map({True: "Yes", False: "No"})
    df["property_type"] = pd.Categorical(df["property_type"], TYPE_ORDER, ordered=True)
    df = df.sort_values(["region", "suburb_group_name", "property_type"])

    for j, (header, _, _, width) in enumerate(DATA_COLUMNS, start=1):
        ws.cell(row=1, column=j, value=header)
        ws.column_dimensions[get_column_letter(j)].width = width
    _style_header(ws, 1, 1, len(DATA_COLUMNS))
    ws.row_dimensions[1].height = 42

    for i, rec in enumerate(df.itertuples(index=False), start=2):
        rec = rec._asdict()
        for j, (_, src, fmt, _) in enumerate(DATA_COLUMNS, start=1):
            v = rec[src]
            v = None if pd.isna(v) else (str(v) if src == "property_type" else v)
            cell = ws.cell(row=i, column=j, value=v)
            if fmt:
                cell.number_format = fmt

    n = len(df)
    table = Table(displayName="tblLatest", ref=f"A1:{get_column_letter(len(DATA_COLUMNS))}{n + 1}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(table)
    ws.freeze_panes = "B2"
    return n


def write_summary(wb: Workbook, latest: pd.DataFrame, n: int, quarter_label: str) -> None:
    ws = wb.active
    ws.title = "Summary"
    _title(ws, "Melbourne Rental Affordability: Stakeholder Summary",
           f"Latest quarter: {quarter_label} (moving annual median rents)   |   Workbook generated: {date.today():%d %b %Y}")

    ws["A4"] = "Headline numbers, latest quarter (reliable rows only: 10+ new bonds)"
    ws["A4"].font = _font(bold=True, size=12, color=NAVY)

    headers = ["Property type", "Average of suburb median rents ($/wk)", "Average QoQ growth",
               "Average YoY growth", "Suburbs in rental stress (30%+)", "Suburbs reported"]
    for j, h in enumerate(headers, start=1):
        ws.cell(row=5, column=j, value=h)
    _style_header(ws, 5, 1, len(headers))
    ws.row_dimensions[5].height = 45

    rent, typ, rel = _rng("median_weekly_rent", n), _rng("property_type", n), _rng("is_reliable", n)
    qoq, yoy, band = _rng("qoq_growth", n), _rng("yoy_growth", n), _rng("affordability_band", n)
    for i, t in enumerate(TYPE_ORDER, start=6):
        ws.cell(row=i, column=1, value=t)
        ws.cell(row=i, column=2, value=f'=IFERROR(AVERAGEIFS({rent},{typ},$A{i},{rel},"Yes"),"-")').number_format = '"$"#,##0'
        ws.cell(row=i, column=3, value=f'=IFERROR(AVERAGEIFS({qoq},{typ},$A{i},{rel},"Yes"),"-")').number_format = "0.0%"
        ws.cell(row=i, column=4, value=f'=IFERROR(AVERAGEIFS({yoy},{typ},$A{i},{rel},"Yes"),"-")').number_format = "0.0%"
        ws.cell(row=i, column=5, value=(f'=IFERROR(COUNTIFS({typ},$A{i},{rel},"Yes",{band},"Rental stress*")'
                                        f'/F{i},"-")')).number_format = "0.0%"
        ws.cell(row=i, column=6, value=f'=COUNTIFS({typ},$A{i},{rel},"Yes")').number_format = "#,##0"
    last = 5 + len(TYPE_ORDER)
    tot = last + 1
    ws.cell(row=tot, column=1, value="All property types")
    ws.cell(row=tot, column=2, value=f'=IFERROR(AVERAGEIFS({rent},{rel},"Yes"),"-")').number_format = '"$"#,##0'
    ws.cell(row=tot, column=3, value=f'=IFERROR(AVERAGEIFS({qoq},{rel},"Yes"),"-")').number_format = "0.0%"
    ws.cell(row=tot, column=4, value=f'=IFERROR(AVERAGEIFS({yoy},{rel},"Yes"),"-")').number_format = "0.0%"
    ws.cell(row=tot, column=5, value=f'=IFERROR(COUNTIFS({rel},"Yes",{band},"Rental stress*")/F{tot},"-")').number_format = "0.0%"
    ws.cell(row=tot, column=6, value=f"=SUM(F6:F{last})").number_format = "#,##0"
    for c in range(1, 7):
        ws.cell(row=tot, column=c).font = _font(bold=True)
        ws.cell(row=tot, column=c).fill = BAND_FILL
    for r in range(6, tot + 1):
        for c in range(1, 7):
            ws.cell(row=r, column=c).border = BOX

    # Commentary, written from the same data at generation time
    rel_rows = latest[latest["is_reliable"]]
    stress = (rel_rows["affordability_band"] == "Rental stress (30%+)").mean()
    near = rel_rows[(rel_rows["distance_to_station_km"] <= VALUE_MAX_KM) & (rel_rows["property_type"] == "2 bed flat")]
    best = near.sort_values("rent_to_income_pct").head(3)
    fastest = rel_rows.groupby("region")["yoy_growth_pct"].mean().sort_values(ascending=False)
    notes = [
        f"{stress:.0%} of reliable suburb x property type combinations have rent at or above 30% of local "
        "median household income (indexed to the latest Wage Price Index).",
        f"Cheapest 2 bed flats relative to income within {VALUE_MAX_KM} km of a station: "
        + "; ".join(f"{r.suburb_group_name} ({r.rent_to_income_pct:.1f}%, near {r.nearest_station})"
                    for r in best.itertuples()) + ".",
        f"Fastest average YoY rent growth by region: {fastest.index[0]} ({fastest.iloc[0]:.1f}%); "
        f"slowest: {fastest.index[-1]} ({fastest.iloc[-1]:.1f}%).",
        "Income is compared at household level: a single-income renter faces a much higher share. "
        "See the Notes sheet for sources and caveats.",
    ]
    c0 = tot + 2
    ws.cell(row=c0, column=1, value="Commentary (auto-generated at refresh)").font = _font(bold=True, size=12, color=NAVY)
    for k, text in enumerate(notes, start=1):
        cell = ws.cell(row=c0 + k, column=1, value=f"• {text}")
        ws.merge_cells(start_row=c0 + k, start_column=1, end_row=c0 + k, end_column=6)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[c0 + k].height = 32

    _widths(ws, {"A": 22, "B": 20, "C": 14, "D": 14, "E": 18, "F": 12})
    ws.freeze_panes = "A6"


def write_region_by_type(wb: Workbook, regions: list[str], n: int) -> None:
    ws = wb.create_sheet("Rent by Region & Type")
    _title(ws, "Average of suburb median rents ($/wk) by region and property type",
           "Latest quarter, reliable rows only. Formulas read the 'Data - Latest Quarter' sheet.")
    ws.cell(row=4, column=1, value="Region")
    for j, t in enumerate(TYPE_ORDER, start=2):
        ws.cell(row=4, column=j, value=t)
    _style_header(ws, 4, 1, len(TYPE_ORDER) + 1)

    rent, typ, reg, rel = (_rng("median_weekly_rent", n), _rng("property_type", n), _rng("region", n),
                           _rng("is_reliable", n))
    for i, region in enumerate(regions, start=5):
        ws.cell(row=i, column=1, value=region).border = BOX
        for j in range(2, len(TYPE_ORDER) + 2):
            col = get_column_letter(j)
            cell = ws.cell(row=i, column=j,
                           value=f'=IFERROR(AVERAGEIFS({rent},{reg},$A{i},{typ},{col}$4,{rel},"Yes"),"-")')
            cell.number_format = '"$"#,##0'
            cell.border = BOX
    last = 4 + len(regions)

    chart = BarChart()
    chart.type = "bar"
    chart.title = "Average median rent by region ($/wk)"
    chart.y_axis.title = "$ per week"
    chart.height, chart.width = 11, 22
    for j in (3, 6):  # 2 bed flat, 3 bed house
        chart.add_data(Reference(ws, min_col=j, min_row=4, max_row=last), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=5, max_row=last))
    ws.add_chart(chart, f"A{last + 3}")
    _widths(ws, {"A": 26, **{get_column_letter(j): 13 for j in range(2, len(TYPE_ORDER) + 2)}})


def write_growth(wb: Workbook, regions: list[str], history: pd.DataFrame, n: int) -> None:
    ws = wb.create_sheet("Growth")
    _title(ws, "Rent growth by region",
           "Averages of suburb growth rates, latest quarter, reliable rows only.")
    for j, h in enumerate(["Region", "Average QoQ growth", "Average YoY growth", "Suburb x type rows"], start=1):
        ws.cell(row=4, column=j, value=h)
    _style_header(ws, 4, 1, 4)
    qoq, yoy, reg, rel = _rng("qoq_growth", n), _rng("yoy_growth", n), _rng("region", n), _rng("is_reliable", n)
    for i, region in enumerate(regions, start=5):
        ws.cell(row=i, column=1, value=region)
        ws.cell(row=i, column=2, value=f'=IFERROR(AVERAGEIFS({qoq},{reg},$A{i},{rel},"Yes"),"-")').number_format = "0.0%"
        ws.cell(row=i, column=3, value=f'=IFERROR(AVERAGEIFS({yoy},{reg},$A{i},{rel},"Yes"),"-")').number_format = "0.0%"
        ws.cell(row=i, column=4, value=f'=COUNTIFS({reg},$A{i},{rel},"Yes")')
        for c in range(1, 5):
            ws.cell(row=i, column=c).border = BOX
    last = 4 + len(regions)

    # Trend tables: average of suburb medians per region, last HISTORY_QUARTERS quarters
    row0 = last + 3
    for k, ptype in enumerate(["2 bed flat", "3 bed house"]):
        h = history[history["property_type"] == ptype]
        pivot = h.pivot_table(index=["quarter_end", "quarter_label"], columns="region", values="avg_rent")
        pivot = pivot.reindex(columns=regions).reset_index().sort_values("quarter_end")
        top = row0 + k * (len(pivot) + 22)
        ws.cell(row=top, column=1, value=f"Average median rent ($/wk), {ptype}, by region").font = _font(bold=True, color=NAVY)
        ws.cell(row=top + 1, column=1, value="Quarter")
        for j, region in enumerate(regions, start=2):
            ws.cell(row=top + 1, column=j, value=region)
        _style_header(ws, top + 1, 1, len(regions) + 1)
        ws.row_dimensions[top + 1].height = 45
        for i, (_, rec) in enumerate(pivot.iterrows(), start=top + 2):
            ws.cell(row=i, column=1, value=rec["quarter_label"])
            for j, region in enumerate(regions, start=2):
                v = rec[region]
                c = ws.cell(row=i, column=j, value=None if pd.isna(v) else round(float(v), 2))
                c.number_format = '"$"#,##0'
        end = top + 1 + len(pivot)

        chart = LineChart()
        chart.title = f"{ptype.capitalize()}: average median rent by region"
        chart.y_axis.title = "$ per week"
        chart.height, chart.width = 10, 24
        chart.add_data(Reference(ws, min_col=2, max_col=len(regions) + 1, min_row=top + 1, max_row=end),
                       titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=top + 2, max_row=end))
        ws.add_chart(chart, f"A{end + 2}")
    _widths(ws, {"A": 26, **{get_column_letter(j): 13 for j in range(2, len(regions) + 2)}})


def write_top_value(wb: Workbook, latest: pd.DataFrame) -> None:
    ws = wb.create_sheet("Top 20 Value Near Trains")
    _title(ws, f"Top {VALUE_TOP_N} value suburbs within {VALUE_MAX_KM} km of a train station",
           "Value score = (1 - rent to income) x income weight + (1 - min(distance / 5 km, 1)) x distance weight. "
           "Higher is better.")

    ws["A4"], ws["B4"] = "Income weight", 0.7
    ws["A5"], ws["B5"] = "Distance weight", 0.3
    for ref in ("B4", "B5"):
        ws[ref].font = _font(color="0000FF", bold=True)
        ws[ref].fill = INPUT_FILL
        ws[ref].number_format = "0.00"
    ws["B4"].comment = Comment("Input: same default weights as the Power BI Value Score (plan section 8). "
                               "Change to re-score; the Rank column updates.", "melb-rent-watch")
    ws["C4"] = "← yellow cells are inputs; rows are listed in order of the default weights"
    ws["C4"].font = _font(italic=True, size=9, color="555555")

    rel = latest[latest["is_reliable"] & (latest["distance_to_station_km"] <= VALUE_MAX_KM)].copy()
    rel["ri"] = rel["rent_to_income_pct"] / 100
    rel["score"] = (1 - rel["ri"]) * 0.7 + (1 - (rel["distance_to_station_km"] / 5).clip(upper=1)) * 0.3
    top = rel.sort_values("score", ascending=False).head(VALUE_TOP_N)

    headers = ["Rank", "Suburb group", "Region", "Property type", "Median rent ($/wk)",
               "Household income, indexed ($/wk)", "Rent to income", "Nearest station", "Distance (km)",
               "YoY growth", "Value score"]
    hr = 7
    for j, h in enumerate(headers, start=1):
        ws.cell(row=hr, column=j, value=h)
    _style_header(ws, hr, 1, len(headers))
    ws.row_dimensions[hr].height = 42
    first, last = hr + 1, hr + len(top)
    for i, r in enumerate(top.itertuples(index=False), start=first):
        ws.cell(row=i, column=1, value=f"=RANK(K{i},$K${first}:$K${last})")
        ws.cell(row=i, column=2, value=r.suburb_group_name)
        ws.cell(row=i, column=3, value=r.region)
        ws.cell(row=i, column=4, value=r.property_type)
        ws.cell(row=i, column=5, value=r.median_weekly_rent).number_format = '"$"#,##0'
        ws.cell(row=i, column=6, value=r.median_weekly_hh_income).number_format = '"$"#,##0'
        ws.cell(row=i, column=7, value=f"=E{i}/F{i}").number_format = "0.0%"
        ws.cell(row=i, column=8, value=r.nearest_station)
        ws.cell(row=i, column=9, value=r.distance_to_station_km).number_format = "0.00"
        ws.cell(row=i, column=10, value=None if pd.isna(r.yoy_growth_pct) else r.yoy_growth_pct / 100).number_format = "0.0%"
        ws.cell(row=i, column=11, value=f"=(1-G{i})*$B$4+(1-MIN(I{i}/5,1))*$B$5").number_format = "0.000"
        for c in range(1, len(headers) + 1):
            ws.cell(row=i, column=c).border = BOX
    _widths(ws, {"A": 7, "B": 34, "C": 24, "D": 13, "E": 12, "F": 14, "G": 10, "H": 20, "I": 10, "J": 10, "K": 10})
    ws.freeze_panes = f"C{first}"


def write_notes(wb: Workbook, quarter_label: str) -> None:
    ws = wb.create_sheet("Notes")
    _title(ws, "Sources, refresh and caveats")
    cfg = load_config()
    rows = [
        ("Refresh", ""),
        ("Latest rental quarter", quarter_label),
        ("Workbook generated", f"{date.today():%d %b %Y}"),
        ("How to refresh", "This workbook is regenerated by the pipeline (python -m src.pipeline, or "
                           "python -m src.report.build_excel). Formulas recalculate when opened."),
        ("", ""),
        ("Sources", ""),
        ("Rents and bond counts", "DFFH / Homes Victoria Rental Report, 'Moving annual median rent by suburb and "
                                  f"town' ({cfg['rental']['landing_page']})"),
        ("Household income", "ABS Census 2021, G02 median total household income ($/weekly) by suburb (SAL), "
                             "via the ABS Data API"),
        ("Income indexing", "ABS Wage Price Index, Victoria, total hourly rates excl. bonuses (base: Sep 2021 "
                            "quarter, the Census quarter)"),
        ("Train stations", f"PTV GTFS feed ({cfg['stations']['gtfs_url']}), metro + V/Line stations in Greater Melbourne"),
        ("Boundaries", "ABS ASGS Edition 3 (2021): Suburbs and Localities, Greater Capital City, LGA"),
        ("", ""),
        ("Definitions", ""),
        ("Median rent", "Moving annual median: leases that started in the 12 months to the quarter end"),
        ("Rent to income", "Median weekly rent / median weekly household income (indexed)"),
        ("Affordability band", "Affordable < 25%; Moderate 25-30%; Rental stress 30%+"),
        ("Reliable", f"10+ new bonds in the period (quality.min_bond_count = {cfg['quality']['min_bond_count']})"),
        ("Distance to station", "Straight-line km from the suburb group's population-weighted point to the "
                                "nearest station"),
        ("", ""),
        ("Caveats", ""),
        ("Grouped suburbs", "DFFH reports suburb groups (e.g. Carlton-Parkville); variation within a group is hidden"),
        ("Dated income", "Census income is from August 2021, adjusted only for average wage growth"),
        ("Household vs individual", "Household income includes all earners; single renters face higher ratios"),
        ("Averages", "Region and type figures are simple averages of suburb medians, not true medians"),
    ]
    for i, (k, v) in enumerate(rows, start=3):
        a, b = ws.cell(row=i, column=1, value=k), ws.cell(row=i, column=2, value=v)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        a.alignment = Alignment(vertical="top")
        if k and not v:
            a.font = _font(bold=True, size=12, color=NAVY)
        else:
            a.font = _font(bold=True)
    _widths(ws, {"A": 26, "B": 110})


# ---------------------------------------------------------------- main

def build(latest: pd.DataFrame, history: pd.DataFrame, path: Path = OUTPUT) -> Path:
    regions = load_config()["rental"]["metro_regions"]
    quarter_label = latest["quarter_label"].iloc[0]

    wb = Workbook()
    n = len(latest)
    write_summary(wb, latest, n, quarter_label)
    write_region_by_type(wb, regions, n)
    write_growth(wb, regions, history, n)
    write_top_value(wb, latest)
    write_notes(wb, quarter_label)
    write_data_sheet(wb, latest)
    _apply_font_everywhere(wb)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def run() -> str:
    latest, history = load_frames()
    path = build(latest, history)
    detail = f"{path.name}: {len(latest)} rows for {latest['quarter_label'].iloc[0]}"
    log.info("Excel summary: %s", detail)
    return detail


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    print(run())
