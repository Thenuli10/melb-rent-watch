"""Generate the Power BI report as a Power BI Project (PBIP, text format).

PBIP is Power BI Desktop's source-control format: the semantic model is TMDL
and the report is PBIR JSON. Open powerbi/melb_rental.pbip in Power BI Desktop,
enter the database credentials when asked, and Refresh. Use File > Save as to
produce a .pbix if needed.

View-backed table columns are read from the analytics views' metadata, so the
model always matches the warehouse. Headline numbers quoted in page text (latest
quarter, benchmark income, model accuracy) are read at generation time, so
re-run after each new quarter:

    python -m src.report.build_powerbi
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from src.common import ROOT, database_url, get_engine, load_config, log

OUT_DIR = ROOT / "powerbi"
NAME = "melb_rental"
SM = f"{NAME}.SemanticModel"
RPT = f"{NAME}.Report"

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
THEME_NAME = "MelbRentWatch"
PAGE_W, PAGE_H = 1280, 720

# Navy palette: navy header band, light blue page, charts in white cards
INK, MUTED, NAVY, AMBER = "#1F2933", "#6B7785", "#1F3A5F", "#E9A23B"
HEADER_BG, HEADER_SUB = "#1F3A5F", "#C9D6E3"
PAGE_BG, CARD, LINE = "#EAF1F8", "#FFFFFF", "#DCE5EF"
ACCENT_BG, ACCENT_LINE = "#F3F7FB", "#D5E2EE"
FALL, HOT = "#4F86C6", "#B23A1E"  # growth colour scales: blue = rent fell, warm = faster growth
BAND_COLORS = {"Affordable": "#3A8F5C", "Moderate": "#E9A23B", "Rental stress (30%+)": "#C8553D",
               "Train station": "#A3AAB4"}
DEFAULT_TYPE = "2 bed flat"
DEFAULT_SUBURB = "Footscray"

# ---------------------------------------------------------------- semantic model

LATEST_STEPS = [
    "LatestQ = List.Max(View[quarter_end])",
    "Filtered = Table.SelectRows(View, each [quarter_end] = LatestQ and [is_reliable] = true "
    "and [median_weekly_rent] <> null)",
]

MAP_POINTS_M = """let
	Source = PostgreSQL.Database(Server, Database),
	Aff = Source{[Schema="analytics",Item="v_affordability"]}[Data],
	LatestQ = List.Max(Aff[quarter_end]),
	Sub = Table.SelectRows(Aff, each [quarter_end] = LatestQ and [is_reliable] = true and [median_weekly_rent] <> null),
	SubPts = Table.SelectColumns(Sub, {"suburb_group_name", "property_type", "latitude", "longitude", "affordability_band", "median_weekly_rent", "rent_to_income_pct", "nearest_station"}),
	SubRen = Table.RenameColumns(SubPts, {{"suburb_group_name", "point_name"}, {"affordability_band", "layer"}}),
	Types = List.Distinct(Sub[property_type]),
	St = Table.SelectRows(Source{[Schema="analytics",Item="v_stations"]}[Data], each [in_rental_area] = true),
	StCols = Table.SelectColumns(St, {"station_name", "latitude", "longitude"}),
	StNamed = Table.TransformColumns(StCols, {{"station_name", each _ & " station", type text}}),
	StTyped = Table.AddColumn(StNamed, "property_type", each Types),
	StRows = Table.ExpandListColumn(StTyped, "property_type"),
	StLayer = Table.AddColumn(Table.RenameColumns(StRows, {{"station_name", "point_name"}}), "layer", each "Train station"),
	Combined = Table.Combine({SubRen, StLayer}),
	Ordered = Table.AddColumn(Combined, "layer_order", each if [layer] = "Train station" then 0 else if [layer] = "Affordable" then 1 else if [layer] = "Moderate" then 2 else 3, Int64.Type),
	Typed = Table.TransformColumnTypes(Ordered, {{"point_name", type text}, {"property_type", type text}, {"layer", type text}, {"latitude", type number}, {"longitude", type number}, {"median_weekly_rent", type number}, {"rent_to_income_pct", type number}, {"nearest_station", type text}})
in
	Typed"""

PROPERTY_TYPE_M = """let
	Source = PostgreSQL.Database(Server, Database),
	View = Source{[Schema="analytics",Item="v_affordability"]}[Data],
	Types = Table.Distinct(Table.SelectColumns(View, {"property_type", "dwelling_type", "bedrooms"})),
	Sorted = Table.AddColumn(Types, "sort_order", each (if [dwelling_type] = "Flat" then 0 else 10) + [bedrooms], Int64.Type)
in
	Sorted"""

# model table -> (analytics view, extra M steps) or ("m", full M, explicit columns)
TABLES = {
    "Affordability": ("v_affordability", None),
    "Latest": ("v_affordability", LATEST_STEPS),
    "Suburbs": ("v_suburbs", None),
    "Stations": ("v_stations", None),
    "Forecast": ("v_rent_forecast", None),
    "Model Metrics": ("v_model_metrics", None),
    "Regression": ("v_regression_coefficients", None),
    "Refresh Info": ("v_refresh_info", None),
    "Benchmark": ("v_benchmark_income", None),
    "Property Type": ("m", PROPERTY_TYPE_M, [("property_type", "string"), ("dwelling_type", "string"),
                                            ("bedrooms", "int64"), ("sort_order", "int64")]),
    "Map Points": ("m", MAP_POINTS_M, [("point_name", "string"), ("property_type", "string"), ("layer", "string"),
                                       ("latitude", "double"), ("longitude", "double"),
                                       ("median_weekly_rent", "double"), ("rent_to_income_pct", "double"),
                                       ("nearest_station", "string"), ("layer_order", "int64")]),
}

PG_TO_TMDL = {
    "text": "string", "character varying": "string",
    "integer": "int64", "bigint": "int64", "smallint": "int64",
    "numeric": "double", "double precision": "double", "real": "double",
    "boolean": "boolean", "date": "dateTime",
    "timestamp with time zone": "dateTime", "timestamp without time zone": "dateTime",
}

FORMATS = {
    "median_weekly_rent": "\\$#,0", "median_weekly_hh_income": "\\$#,0", "income_census_2021": "\\$#,0",
    "income_census": "\\$#,0", "income_indexed": "\\$#,0",
    "actual_rent": "\\$#,0", "predicted_rent": "\\$#,0", "lower_80": "\\$#,0", "upper_80": "\\$#,0",
    "prev_q_rent": "\\$#,0", "rolling_4q_avg_rent": "\\$#,0", "distance_to_station_km": "0.0",
    "quarter_end": "mmm yyyy",
}
SUMMARIZE = {"median_weekly_rent": "average", "new_bonds_count": "sum"}
# Map series are drawn in sort order, so stations (0) go underneath the suburb dots
SORT_BY = {("Property Type", "property_type"): "sort_order", ("Regression", "Distance Band"): "'Band Order'",
           ("Map Points", "layer"): "layer_order"}

# Ranks used by the risers/fallers chart: only suburbs with a value take part
_RANK_YOY = ("RANKX ( FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Avg YoY Growth] ) ),\n"
             "    [Avg YoY Growth], , {order} )")

MEASURES = {
    "Latest": [
        ("Avg Rent", "AVERAGE ( Latest[median_weekly_rent] )", "\\$#,0"),
        # Plan section 8: rent vs the suburb's own (indexed) household income
        ("Rent to Income %", "DIVIDE ( AVERAGE ( Latest[median_weekly_rent] ), AVERAGE ( Latest[median_weekly_hh_income] ) )", "0.0%"),
        ("Avg QoQ Growth", "DIVIDE ( AVERAGE ( Latest[qoq_growth_pct] ), 100 )", "0.0%"),
        ("Avg YoY Growth", "DIVIDE ( AVERAGE ( Latest[yoy_growth_pct] ), 100 )", "+0.0%;-0.0%"),
        ("% Suburbs in Rental Stress",
         "DIVIDE (\n    CALCULATE ( COUNTROWS ( Latest ), Latest[affordability_band] = \"Rental stress (30%+)\" ),\n"
         "    COUNTROWS ( Latest )\n)", "0%"),
        ("% of Suburbs",
         "DIVIDE ( COUNTROWS ( Latest ), CALCULATE ( COUNTROWS ( Latest ), REMOVEFILTERS ( Latest[affordability_band] ) ) )",
         "0%"),
        # Best value page: rent against the viewer's own income, not the suburb's
        ("Rent / Your Income", "DIVIDE ( [Avg Rent], [Your Income] )", "0%"),
        # Blank (no rows in context) must not count as "within limits": DAX compares BLANK as 0
        ("In Range",
         "VAR d = MAX ( Latest[distance_to_station_km] )\nVAR r = MAX ( Latest[median_weekly_rent] )\n"
         "RETURN IF ( NOT ISBLANK ( d ) && NOT ISBLANK ( r ) && d <= [Max Distance km] && r <= [Weekly Budget $], 1 )",
         "0"),
        ("Value Score Raw",
         "VAR ri = [Rent / Your Income]\nVAR dist = AVERAGE ( Latest[distance_to_station_km] )\nVAR w = [Income Weight]\n"
         "RETURN IF ( [In Range] = 1, ( 1 - ri ) * w + ( 1 - MIN ( dist / 5, 1 ) ) * ( 1 - w ) )", "0.000"),
        # 0-100: scores differ in the third decimal, which is false precision
        ("Value Score", "VAR s = [Value Score Raw] RETURN IF ( NOT ISBLANK ( s ), ROUND ( 100 * s, 0 ) )", "0"),
        # The table groups by suburb only (station and band are measures), so ALLSELECTED of
        # the suburb column ranks each row against all suburbs, not just itself
        ("Rank",
         "IF ( NOT ISBLANK ( [Value Score Raw] ),\n"
         "    RANKX ( FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Value Score Raw] ) ),\n"
         "        [Value Score Raw], , DESC, Dense ) )", "0"),
        # A blank Rank (suburb outside the limits) compares as 0, which is <= 15: guard it
        ("In Top 15", "VAR k = [Rank] RETURN IF ( NOT ISBLANK ( k ) && k <= 15, 1 )", "0"),
        ("Top Rank", "IF ( [In Top 15] = 1, [Rank] )", "0"),
        ("Top Station", "IF ( [In Top 15] = 1, SELECTEDVALUE ( Latest[nearest_station] ) )", None),
        ("Top Distance Band", "IF ( [In Top 15] = 1, SELECTEDVALUE ( Latest[distance_band] ) )", None),
        ("Top Rent", "IF ( [In Top 15] = 1, [Avg Rent] )", "\\$#,0"),
        ("Top Rent / Your Income", "IF ( [In Top 15] = 1, [Rent / Your Income] )", "0%"),
        ("Top YoY Growth", "IF ( [In Top 15] = 1, [Avg YoY Growth] )", "+0.0%;-0.0%"),
        ("Top Score", "IF ( [In Top 15] = 1, [Value Score] )", "0"),
        ("Best Value Title",
         "VAR n = COUNTROWS ( FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Value Score Raw] ) ) )\n"
         "VAR best = TOPN ( 1, FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Value Score Raw] ) ),\n"
         "    [Value Score Raw], DESC )\n"
         "RETURN IF ( n = 0, \"No suburbs fit these limits: try a higher budget or distance\",\n"
         "    CONCATENATEX ( best, Latest[suburb_group_name], \", \" ) & \" is the best value of \" & n\n"
         "    & \" suburbs that fit your limits (top 15 shown)\" )", None),
        ("Shown Rent / Your Income", "IF ( [In Range] = 1, [Rent / Your Income] )", "0%"),
        ("Shown Rent", "IF ( [In Range] = 1, [Avg Rent] )", "\\$#,0"),
        # 30% of the viewer's income: the most they can pay before rental stress
        ("Stress Rent Limit", "0.3 * [Your Income]", "\\$#,0"),
        # Summary cards and breakdowns for the suburbs that fit the viewer's limits
        ("Suburbs In Limits",
         "VAR n = COUNTROWS ( FILTER ( VALUES ( Latest[suburb_group_name] ), [In Range] = 1 ) )\n"
         "RETURN IF ( n > 0, n )", "0"),
        ("Best Value Suburb",
         "VAR best = TOPN ( 1, FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Value Score Raw] ) ),\n"
         "    [Value Score Raw], DESC )\n"
         "RETURN CONCATENATEX ( best, Latest[suburb_group_name], \", \" )", None),
        ("Cheapest Rent In Limits",
         "MINX ( FILTER ( VALUES ( Latest[suburb_group_name] ), [In Range] = 1 ), [Avg Rent] )", "\\$#,0"),
        ("Dot Colour", "IF ( [In Top 15] = 1, \"#1F3A5F\", \"#C5CEDA\" )", None),
        ("Scatter Note",
         "VAR best3 = TOPN ( 3, FILTER ( ALLSELECTED ( Latest[suburb_group_name] ), NOT ISBLANK ( [Value Score Raw] ) ),\n"
         "    [Value Score Raw], DESC )\n"
         "RETURN \"Best: \" & CONCATENATEX ( best3, Latest[suburb_group_name], \", \", [Value Score Raw], DESC )\n"
         "    & \"  ·  30% of income = \" & FORMAT ( [Stress Rent Limit], \"$#,0\" ) & \"/wk\"", None),
        ("Shown Distance km", "IF ( [In Range] = 1, AVERAGE ( Latest[distance_to_station_km] ) )", "0.0"),
        # Top 10 risers, plus any suburb where rent actually fell. Many suburbs sit at exactly
        # 0.0% (DFFH medians are rounded), so "bottom 10" would be ties picked alphabetically
        ("Fastest and Falling YoY",
         "VAR g = [Avg YoY Growth]\nVAR hi = " + _RANK_YOY.format(order="DESC")
         + "\nRETURN IF ( NOT ISBLANK ( g ) && ( hi <= 10 || g < 0 ), g )",
         "+0.0%;-0.0%"),
    ],
    "Affordability": [
        ("Avg Rent (all quarters)", "AVERAGE ( Affordability[median_weekly_rent] )", "\\$#,0"),
        ("YoY Growth (last 2 years)",
         "VAR q = MAX ( Affordability[quarter_end] )\n"
         "VAR lastQ = CALCULATE ( MAX ( Affordability[quarter_end] ), ALL ( Affordability ) )\n"
         "RETURN IF ( q > EDATE ( lastQ, -24 ), DIVIDE ( AVERAGE ( Affordability[yoy_growth_pct] ), 100 ) )",
         "+0.0%;-0.0%"),
        # Action titles: state the conclusion for whatever the slicers select
        ("Trend Title",
         "VAR lastQ = CALCULATE ( MAX ( Affordability[quarter_end] ), ALL ( Affordability ) )\n"
         "VAR nowR = CALCULATE ( [Avg Rent (all quarters)], Affordability[quarter_end] = lastQ )\n"
         "VAR thenR = CALCULATE ( [Avg Rent (all quarters)], Affordability[quarter_end] = EOMONTH ( lastQ, -60 ) )\n"
         "RETURN \"Typical \" & SELECTEDVALUE ( 'Property Type'[property_type], \"\" ) & \" rent rose \"\n"
         "    & FORMAT ( DIVIDE ( nowR, thenR ) - 1, \"0%\" ) & \" in 5 years, from \" & FORMAT ( thenR, \"$#,0\" )\n"
         "    & \" to \" & FORMAT ( nowR, \"$#,0\" ) & \" a week\"", None),
        ("Growth Title",
         "VAR lastQ = CALCULATE ( MAX ( Affordability[quarter_end] ), ALL ( Affordability ) )\n"
         "VAR nowG = CALCULATE ( AVERAGE ( Affordability[yoy_growth_pct] ), Affordability[quarter_end] = lastQ )\n"
         "VAR thenG = CALCULATE ( AVERAGE ( Affordability[yoy_growth_pct] ), Affordability[quarter_end] = EOMONTH ( lastQ, -21 ) )\n"
         "RETURN \"Rent growth has \" & IF ( nowG < thenG, \"cooled\", \"picked up\" ) & \" from \"\n"
         "    & FORMAT ( thenG / 100, \"0%\" ) & \" to \" & FORMAT ( nowG / 100, \"0%\" )\n"
         "    & \" a year over the last two years (darker = faster growth)\"", None),
    ],
    "Forecast": [
        ("Actual Rent",
         "VAR q = MAX ( Forecast[quarter_end] )\nVAR lastQ = CALCULATE ( MAX ( Forecast[quarter_end] ), ALL ( Forecast ) )\n"
         "RETURN IF ( q > EDATE ( lastQ, -36 ), AVERAGE ( Forecast[actual_rent] ) )", "\\$#,0"),
        # Forecast lines start at the last actual quarter so they join the actual line
        ("Forecast Rent", "IF ( [Is Last Actual], AVERAGE ( Forecast[actual_rent] ), AVERAGE ( Forecast[predicted_rent] ) )", "\\$#,0"),
        ("Forecast Low (80%)", "IF ( [Is Last Actual], AVERAGE ( Forecast[actual_rent] ), AVERAGE ( Forecast[lower_80] ) )", "\\$#,0"),
        ("Forecast High (80%)", "IF ( [Is Last Actual], AVERAGE ( Forecast[actual_rent] ), AVERAGE ( Forecast[upper_80] ) )", "\\$#,0"),
        ("Is Last Actual",
         "VAR lastActual = CALCULATE ( MAX ( Forecast[quarter_end] ), ALL ( Forecast ), NOT ISBLANK ( Forecast[actual_rent] ) )\n"
         "RETURN MAX ( Forecast[quarter_end] ) = lastActual", None),
    ],
    "Model Metrics": [
        ("Overall MAPE", "CALCULATE ( AVERAGE ( 'Model Metrics'[mape] ), 'Model Metrics'[segment_type] = \"overall\" ) / 100", "0.00%"),
        ("Overall MAE", "CALCULATE ( AVERAGE ( 'Model Metrics'[mae] ), 'Model Metrics'[segment_type] = \"overall\" )", "\\$#,0.00"),
        ("Published",
         "IF ( CALCULATE ( COUNTROWS ( 'Model Metrics' ), 'Model Metrics'[segment_type] = \"overall\",\n"
         "    'Model Metrics'[is_selected] = TRUE () ) > 0, \"Yes\" )", None),
    ],
    "Regression": [
        ("Train Premium",
         "IF ( NOT ISBLANK ( SELECTEDVALUE ( Regression[Distance Band] ) ), EXP ( SUM ( Regression[coefficient] ) ) - 1 )",
         "+0.0%;-0.0%"),
        ("Premium Within 2 km",
         "CALCULATE ( EXP ( SUM ( Regression[coefficient] ) ) - 1, Regression[term] = \"near_model:near_station\" )",
         "+0.0%;-0.0%"),
        ("Premium 95% CI",
         "IF ( NOT ISBLANK ( SELECTEDVALUE ( Regression[Distance Band] ) ),\n"
         "    FORMAT ( EXP ( SUM ( Regression[conf_low] ) ) - 1, \"+0.0%;-0.0%\" ) & \" to \" &\n"
         "    FORMAT ( EXP ( SUM ( Regression[conf_high] ) ) - 1, \"+0.0%;-0.0%\" ) )", None),
        ("p-value",
         "VAR p = SUM ( Regression[p_value] )\n"
         "RETURN IF ( NOT ISBLANK ( SELECTEDVALUE ( Regression[Distance Band] ) ), IF ( p < 0.001, \"<0.001\", FORMAT ( p, \"0.000\" ) ) )",
         None),
    ],
    "Benchmark": [
        ("Benchmark Income", "MAX ( Benchmark[income_indexed] )", "\\$#,0"),
    ],
    "Map Points": [
        # Blank when the property type slicer leaves no rows. A visual keeps any row where a
        # measure returns a value, so a measure that is never blank (e.g. a constant size)
        # brings back every property type's dot and the map draws them as pies
        ("Places", "IF ( [Map Show] = 1, COUNTROWS ( 'Map Points' ) )", "0"),
        # Map Points isn't related to Latest, so suburb rows look up their Latest values by name.
        # The property type slicer filters both through the shared Property Type table.
        ("Map Show",
         "VAR lay = SELECTEDVALUE ( 'Map Points'[layer] )\nVAR nm = SELECTEDVALUE ( 'Map Points'[point_name] )\n"
         "RETURN IF ( lay = \"Train station\", 1,\n"
         "    CALCULATE ( [In Range], TREATAS ( { nm }, Latest[suburb_group_name] ) ) )", "0"),
        ("Map Colour",
         "VAR lay = SELECTEDVALUE ( 'Map Points'[layer] )\nVAR nm = SELECTEDVALUE ( 'Map Points'[point_name] )\n"
         "RETURN IF ( lay = \"Train station\", \"#B8C4D2\",\n"
         "    IF ( CALCULATE ( [In Top 15], TREATAS ( { nm }, Latest[suburb_group_name] ) ) = 1,\n"
         "        \"#1F3A5F\", \"#5B9BD5\" ) )", None),
    ],
}

# Regression terms look like 'band_model:C(distance_band, levels=BAND_ORDER)[T.<1 km]'
CALC_COLUMNS = {
    "Regression": [
        # Distance terms only: the band model's controls (House, regions) use the same [T.x] pattern
        ("Distance Band",
         "VAR t = Regression[term]\nVAR p = SEARCH ( \"[T.\", t, 1, 0 )\n"
         "RETURN IF ( LEFT ( t, 11 ) = \"band_model:\" && CONTAINSSTRING ( t, \"C(distance_band\" ) && p > 0,\n"
         "    MID ( t, p + 3, LEN ( t ) - p - 3 ) )", "string"),
        # Derived from [term], not [Distance Band]: sorting a column by a column computed
        # from it is a circular dependency in Power BI
        ("Band Order",
         "VAR t = Regression[term]\nRETURN SWITCH ( TRUE (),\n"
         "    LEFT ( t, 11 ) <> \"band_model:\", BLANK (),\n"
         "    CONTAINSSTRING ( t, \"[T.<1 km]\" ), 1,\n"
         "    CONTAINSSTRING ( t, \"[T.1–2 km]\" ), 2,\n"
         "    CONTAINSSTRING ( t, \"[T.2–5 km]\" ), 3 )", "int64"),
    ],
}

# What-if parameters: (table, slicer column, measure, values, default, format, divisor).
# A few hand-picked values each, so they fit as one row of tile buttons (no scrolling
# dropdowns). The weight is whole percent to avoid floating-point values like 0.7000000001.
# "{bench}" is replaced with the benchmark income rounded to $100 at generation time.
WHAT_IF = [
    ("Param Distance", "Distance option", "Max Distance km", [0.5, 1, 1.5, 2, 3, 5], 1.5, "0.0", 1),
    ("Param Budget", "Budget option", "Weekly Budget $", [400, 500, 600, 700, 850, 1000], 700, "\\$#,0", 1),
    ("Param Income", "Income option", "Your Income", [1500, 2000, "{bench}", 2500, 3000, 4000], "{bench}",
     "\\$#,0", 1),
    ("Param Weight", "Weight option (%)", "Income Weight", [50, 70, 90], 70, "0", 100),
]


def what_if_specs(bench_default: int) -> list[tuple]:
    out = []
    for table, column, measure, values, default, fmt, divisor in WHAT_IF:
        vals = sorted({bench_default if v == "{bench}" else v for v in values})
        dflt = bench_default if default == "{bench}" else default
        out.append((table, column, measure, vals, dflt, fmt, divisor))
    return out

RELATIONSHIPS = [
    ("Affordability", "suburb_key", "Suburbs", "suburb_key"),
    ("Latest", "suburb_key", "Suburbs", "suburb_key"),
    ("Affordability", "property_type", "Property Type", "property_type"),
    ("Latest", "property_type", "Property Type", "property_type"),
    ("Forecast", "property_type", "Property Type", "property_type"),
    ("Map Points", "property_type", "Property Type", "property_type"),
]


def _tag() -> str:
    return str(uuid.uuid4())


def _q(name: str) -> str:
    """Quote a TMDL object name when needed."""
    return f"'{name}'" if any(ch in name for ch in " .=:'$%&()-<>[]/") or name[0].isdigit() else name


def _indent(text: str, tabs: int) -> str:
    pad = "\t" * tabs
    return "\n".join(pad + line if line else line for line in text.splitlines())


def view_columns() -> pd.DataFrame:
    return pd.read_sql("""
        SELECT table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'analytics'
        ORDER BY table_name, ordinal_position""", get_engine())


def _measure_tmdl(name: str, expr: str, fmt: str | None) -> str:
    body = expr if "\n" not in expr else "\n" + _indent(expr, 3)
    out = f"\tmeasure {_q(name)} = {body}\n"
    if fmt:
        out += f"\t\tformatString: {fmt}\n"
    out += f"\t\tlineageTag: {_tag()}\n\n"
    return out


def _column_tmdl(table: str, name: str, dtype: str) -> str:
    is_numeric = dtype in ("int64", "double")
    summarize = SUMMARIZE.get(name, "none" if not is_numeric or name.endswith("_key") or name in (
        "bedrooms", "latitude", "longitude", "sort_order") else "sum")
    out = f"\tcolumn {_q(name)}\n\t\tdataType: {dtype}\n"
    if name in FORMATS:
        out += f"\t\tformatString: {FORMATS[name]}\n"
    if (table, name) in SORT_BY:
        out += f"\t\tsortByColumn: {SORT_BY[(table, name)]}\n"
    out += f"\t\tlineageTag: {_tag()}\n\t\tsummarizeBy: {summarize}\n\t\tsourceColumn: {name}\n"
    if name == "latitude":
        out += "\t\tdataCategory: Latitude\n"
    elif name == "longitude":
        out += "\t\tdataCategory: Longitude\n"
    return out + "\n"


def table_tmdl(table: str, spec: tuple, cols: pd.DataFrame) -> str:
    out = f"table {_q(table)}\n\tlineageTag: {_tag()}\n\n"
    for name, expr, fmt in MEASURES.get(table, []):
        out += _measure_tmdl(name, expr, fmt)

    if spec[0] == "m":
        _, m, explicit = spec
        for name, dtype in explicit:
            out += _column_tmdl(table, name, dtype)
    else:
        view, steps = spec
        tcols = cols[cols["table_name"] == view]
        if tcols.empty:
            raise RuntimeError(f"View analytics.{view} not found; run the pipeline first")
        for r in tcols.itertuples():
            dtype = PG_TO_TMDL.get(r.data_type)
            if dtype is None:
                raise ValueError(f"No TMDL type for {view}.{r.column_name} ({r.data_type})")
            out += _column_tmdl(table, r.column_name, dtype)
        src = [f"Source = PostgreSQL.Database(Server, Database)",
               f"View = Source{{[Schema=\"analytics\",Item=\"{view}\"]}}[Data]"] + (steps or [])
        last = src[-1].split(" = ", 1)[0]
        m = "let\n" + ",\n".join("\t" + s for s in src) + f"\nin\n\t{last}"

    for name, expr, dtype in CALC_COLUMNS.get(table, []):
        out += f"\tcolumn {_q(name)} =\n{_indent(expr, 3)}\n\t\tdataType: {dtype}\n\t\tlineageTag: {_tag()}\n"
        if (table, name) in SORT_BY:
            out += f"\t\tsortByColumn: {SORT_BY[(table, name)]}\n"
        out += "\t\tsummarizeBy: none\n\n"

    out += f"\tpartition {_q(table)} = m\n\t\tmode: import\n\t\tsource =\n{_indent(m, 4)}\n\n"
    out += "\tannotation PBI_ResultType = Table\n"
    return out


def what_if_tmdl(table: str, column: str, measure: str, values, default, fmt, divisor) -> str:
    value = f"SELECTEDVALUE ( {_q(table)}[{column}], {default} )"
    table_ctor = "{ " + ", ".join(str(v) for v in values) + " }"
    if divisor != 1:
        value = f"DIVIDE ( {value}, {divisor} )"
    return (
        f"table {_q(table)}\n\tlineageTag: {_tag()}\n\n"
        f"\tmeasure {_q(measure)} = {value}\n"
        f"\t\tformatString: {fmt if divisor == 1 else '0.00'}\n\t\tlineageTag: {_tag()}\n\n"
        f"\tcolumn {_q(column)}\n\t\tdataType: double\n\t\tformatString: {fmt}\n\t\tlineageTag: {_tag()}\n"
        f"\t\tsummarizeBy: none\n\t\tsourceColumn: [Value]\n\n"
        f"\t\textendedProperty ParameterMetadata =\n\t\t\t\t{{\n\t\t\t\t  \"version\": 0\n\t\t\t\t}}\n\n"
        f"\tpartition {_q(table)} = calculated\n\t\tmode: import\n"
        f"\t\tsource = {table_ctor}\n\n"
        "\tannotation PBI_Id = " + uuid.uuid4().hex + "\n"
    )


def server_from_env() -> tuple[str, str]:
    """Host:port and database name only; credentials are never written to the project."""
    from urllib.parse import urlsplit

    u = urlsplit(database_url())
    db = u.path.lstrip("/") or "postgres"
    # Pooler user names look like postgres.<project-ref>; the direct host is db.<project-ref>.supabase.co
    if load_config().get("powerbi", {}).get("direct_connection") and "." in (u.username or ""):
        ref = u.username.split(".", 1)[1]
        return f"db.{ref}.supabase.co:5432", db
    return f"{u.hostname}:{u.port or 5432}", db


def write_semantic_model(base: Path, facts: dict) -> None:
    cols = view_columns()
    d = base / SM / "definition"
    (d / "tables").mkdir(parents=True, exist_ok=True)
    server, db = server_from_env()

    (base / SM / "definition.pbism").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}}, indent=2), encoding="utf-8")
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1567\n\n", encoding="utf-8")

    names = list(TABLES) + [w[0] for w in WHAT_IF]
    model = ("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
             "\tsourceQueryCulture: en-AU\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
             "annotation PBI_QueryOrder = " + json.dumps(["Server", "Database"] + list(TABLES)) + "\n\n"
             + "".join(f"ref table {_q(n)}\n" for n in names))
    (d / "model.tmdl").write_text(model, encoding="utf-8")

    (d / "expressions.tmdl").write_text(
        f"expression Server = \"{server}\" meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]\n"
        f"\tlineageTag: {_tag()}\n\n\tannotation PBI_ResultType = Text\n\n"
        f"expression Database = \"{db}\" meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]\n"
        f"\tlineageTag: {_tag()}\n\n\tannotation PBI_ResultType = Text\n", encoding="utf-8")

    for table, spec in TABLES.items():
        (d / "tables" / f"{table}.tmdl").write_text(table_tmdl(table, spec, cols), encoding="utf-8")
    for w in what_if_specs(facts["bench_default"]):
        (d / "tables" / f"{w[0]}.tmdl").write_text(what_if_tmdl(*w), encoding="utf-8")

    rel = "".join(
        f"relationship {_tag()}\n\tfromColumn: {_q(ft)}.{_q(fc)}\n\ttoColumn: {_q(tt)}.{_q(tc)}\n\n"
        for ft, fc, tt, tc in RELATIONSHIPS)
    (d / "relationships.tmdl").write_text(rel, encoding="utf-8")


# ---------------------------------------------------------------- report helpers

def lit(v) -> dict:
    return {"expr": {"Literal": {"Value": _literal(v)}}}


def _literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v}D"
    return "'" + str(v).replace("'", "''") + "'"


def col(table: str, column: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}}


def meas(table: str, measure: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": measure}}


def avg(field: dict) -> dict:
    """Aggregate a column with Average (QueryAggregateFunction 1)."""
    return {"Aggregation": {"Expression": field, "Function": 1}}


def _ref(field: dict) -> tuple[str, str]:
    kind = next(iter(field))
    if kind == "Aggregation":
        e, p = _ref(field["Aggregation"]["Expression"])
        return f"Avg({e}", f"{p})"
    return field[kind]["Expression"]["SourceRef"]["Entity"], field[kind]["Property"]


def proj(field: dict, display: str | None = None) -> dict:
    entity, prop = _ref(field)
    p = {"field": field, "queryRef": f"{entity}.{prop}", "nativeQueryRef": display or prop}
    if display:
        p["displayName"] = display
    return p


def color(hex_: str) -> dict:
    return {"solid": {"color": lit(hex_)}}


def series_color_rules(column: dict, colors: dict[str, str]) -> list[dict]:
    """Fixed colour per category value, e.g. Affordable = green."""
    return [{"properties": {"fill": color(c)},
             "selector": {"data": [{"scopeId": {"Comparison": {
                 "ComparisonKind": 0, "Left": column, "Right": {"Literal": {"Value": _literal(v)}}}}}]}}
            for v, c in colors.items()]


def gradient(measure: dict, lo: str, mid: str, hi: str, mid_value: float = 0.0) -> dict:
    """Diverging colour scale driven by a measure (conditional formatting)."""
    return {"solid": {"color": {"expr": {"FillRule": {
        "Input": measure,
        "FillRule": {"linearGradient3": {
            "min": {"color": {"Literal": {"Value": _literal(lo)}}},
            "mid": {"color": {"Literal": {"Value": _literal(mid)}}, "value": {"Literal": {"Value": f"{mid_value}D"}}},
            "max": {"color": {"Literal": {"Value": _literal(hi)}}},
            "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}},
        }},
    }}}}}


def default_filter(table: str, column: str, value) -> dict:
    """Slicer default selection, in the same form Power BI Desktop saves it."""
    return {"filter": {"Version": 2, "From": [{"Name": "d", "Entity": table, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": column}}],
                           "Values": [[{"Literal": {"Value": _literal(value)}}]]}}}]}}


class Page:
    """A report page on a 1280 x 720 canvas. Visuals sit in white rounded cards on a
    pale blue page, on a tight grid (12 px margin, 10 px gutter)."""

    def __init__(self, name: str, display: str):
        self.name, self.display = name, display
        self.visuals: list[dict] = []

    def add(self, vname: str, x, y, w, h, visual: dict, title: str | None = None,
            title_measure: dict | None = None, title_off: bool = False, subtitle: str | None = None,
            subtitle_measure: dict | None = None,
            card: bool = True, accent: bool = False, fill: str | None = None, radius: int = 8) -> None:
        container = {
            "$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.4.0/schema.json",
            "name": vname,
            "position": {"x": x, "y": y, "z": len(self.visuals) * 1000, "width": w, "height": h,
                         "tabOrder": len(self.visuals) * 1000},
            "visual": visual,
        }
        if fill:
            vco = {"background": [{"properties": {"show": lit(True), "color": color(fill), "transparency": lit(0)}}],
                   "border": [{"properties": {"show": lit(True), "color": color(fill), "radius": lit(radius)}}],
                   "padding": [{"properties": {"top": lit(6), "bottom": lit(4), "left": lit(16), "right": lit(16)}}]}
        elif card or accent:
            vco = {
                "background": [{"properties": {"show": lit(True), "color": color(ACCENT_BG if accent else CARD),
                                               "transparency": lit(0)}}],
                "border": [{"properties": {"show": lit(True), "color": color(ACCENT_LINE if accent else LINE),
                                           "radius": lit(radius), "width": lit(1)}}],
                "padding": [{"properties": {"top": lit(6), "bottom": lit(6), "left": lit(10), "right": lit(10)}}],
            }
        else:
            vco = {"background": [{"properties": {"show": lit(False)}}],
                   "border": [{"properties": {"show": lit(False)}}]}
        if title or title_measure:
            text = {"expr": title_measure} if title_measure else lit(title)
            vco["title"] = [{"properties": {"show": lit(True), "text": text, "fontSize": lit(11),
                                            "fontColor": color(INK), "bold": lit(True), "titleWrap": lit(True)}}]
        elif title_off:
            vco["title"] = [{"properties": {"show": lit(False)}}]
        if subtitle or subtitle_measure:
            text = {"expr": subtitle_measure} if subtitle_measure else lit(subtitle)
            vco["subTitle"] = [{"properties": {"show": lit(True), "text": text, "fontSize": lit(8),
                                               "fontColor": color(MUTED)}}]
        visual.setdefault("visualContainerObjects", vco)
        self.visuals.append(container)

    def text(self, vname, x, y, w, h, paragraphs: list[tuple[str, dict]], card: bool = False,
             accent: bool = False, fill: str | None = None, radius: int = 8) -> None:
        paras = [{"textRuns": [{"value": t, "textStyle": style}]} for t, style in paragraphs]
        self.add(vname, x, y, w, h, {"visualType": "textbox",
                                     "objects": {"general": [{"properties": {"paragraphs": paras}}]}},
                 card=card, accent=accent, fill=fill, radius=radius)

    def header_bar(self, title: str, subtitle: str) -> None:
        self.text("header", 0, 0, PAGE_W, 64, [
            (title, {"fontSize": "18pt", "fontWeight": "bold", "color": "#FFFFFF"}),
            (subtitle, {"fontSize": "9pt", "color": HEADER_SUB}),
        ], fill=HEADER_BG, radius=0)


H2 = {"fontSize": "11pt", "fontWeight": "bold", "color": NAVY}
BODY = {"fontSize": "9pt", "color": INK}
SMALL = {"fontSize": "8pt", "color": MUTED}


def card(field: dict, label: str, size: int = 22, hero: bool = False) -> dict:
    """KPI card. hero=True: white text, for a card placed on a navy fill."""
    return {"visualType": "card",
            "query": {"queryState": {"Values": {"projections": [proj(field, label)]}}},
            "objects": {"labels": [{"properties": {"color": color("#FFFFFF" if hero else NAVY), "fontSize": lit(size)}}],
                        "categoryLabels": [{"properties": {"show": lit(True), "fontSize": lit(9),
                                                           "color": color(HEADER_SUB if hero else MUTED)}}]}}


# Skinny bars: more space between categories (X-axis > Layout > Space between bars)
def int_lit(n: int) -> dict:
    """Literal for integer-typed formatting properties (innerPadding, maxMarginFactor)."""
    return {"expr": {"Literal": {"Value": f"{int(n)}L"}}}


def skinny(extra: dict | None = None, padding: int = 40) -> dict:
    return {"categoryAxis": [{"properties": {"innerPadding": int_lit(padding), **(extra or {})}}]}


def slicer(field: dict, label: str, default=None, strict: bool = False, tiles: bool = False,
           on_dark: bool = False) -> dict:
    """Slicer. tiles=True shows every value as a horizontal row of buttons (no dropdown, no
    scrolling); otherwise a dropdown. With a default it is single-select and shows that value."""
    objs = {"data": [{"properties": {"mode": lit("Basic" if tiles else "Dropdown")}}],
            "header": [{"properties": {"show": lit(True), "text": lit(label),
                                       "fontColor": color(HEADER_SUB if on_dark else MUTED),
                                       "textSize": lit(8)}}],
            "items": [{"properties": {"textSize": lit(9), "fontColor": color(INK), "background": color(CARD)}}]}
    general = {}
    if tiles:
        general["orientation"] = {"expr": {"Literal": {"Value": "1D"}}}  # 1 = horizontal
    if default is not None:
        entity, prop = _ref(field)
        sel = {"singleSelect": lit(True)}
        if strict:
            sel["strictSingleSelect"] = lit(True)
        objs["selection"] = [{"properties": sel}]
        general["filter"] = default_filter(entity, prop, default)
    if general:
        objs["general"] = [{"properties": general}]
    return {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [proj(field, label)]}}},
            "objects": objs}


def type_slicer(label: str = "Property type") -> dict:
    v = slicer(col("Property Type", "property_type"), label, default=DEFAULT_TYPE, strict=True, on_dark=True)
    v["syncGroup"] = {"groupName": "PropertyType", "fieldChanges": True, "filterChanges": True}
    return v


def chart(vtype: str, roles: dict[str, list[dict]], sort: tuple[dict, str] | None = None,
          objects: dict | None = None) -> dict:
    q: dict = {"queryState": {r: {"projections": p} for r, p in roles.items()}}
    if sort:
        q["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}], "isDefaultSort": True}
    v = {"visualType": vtype, "query": q}
    if objects:
        v["objects"] = objects
    return v


NO_TOTALS = {"total": [{"properties": {"totals": lit(False)}}]}
NO_SUBTOTALS = {"subTotals": [{"properties": {"rowSubtotals": lit(False), "columnSubtotals": lit(False)}}]}


# ---------------------------------------------------------------- facts quoted in page text

def report_facts() -> dict:
    e = get_engine()
    q = pd.read_sql("SELECT max(quarter_label) AS q FROM analytics.v_affordability "
                    "WHERE quarter_end = (SELECT max(quarter_end) FROM analytics.v_affordability)", e)["q"].iloc[0]
    bench = pd.read_sql("SELECT income_census, income_indexed FROM analytics.v_benchmark_income "
                        "WHERE benchmark = 'Greater Melbourne'", e).iloc[0]
    mt = pd.read_sql("SELECT model_name, mape, is_selected FROM analytics.v_model_metrics "
                     "WHERE segment_type = 'overall'", e).set_index("model_name")
    reg = pd.read_sql("SELECT term, coefficient, conf_low, conf_high FROM analytics.v_regression_coefficients "
                      "WHERE term = 'near_model:near_station' OR term LIKE 'band_model:C(distance_band%%'", e)
    near = reg[reg["term"] == "near_model:near_station"].iloc[0]
    bands = {t.split("[T.")[1][:-1]: f"{np.expm1(lo):+.1%} to {np.expm1(hi):+.1%}"
             for t, lo, hi in zip(reg["term"], reg["conf_low"], reg["conf_high"]) if "[T." in t}
    return {
        "near": {k: float(np.expm1(near[c])) for k, c in (("premium", "coefficient"), ("low", "conf_low"),
                                                          ("high", "conf_high"))},
        "bands": bands,
        "quarter": q,
        "bench_census": float(bench["income_census"]),
        "bench_indexed": float(bench["income_indexed"]),
        "bench_default": int(round(float(bench["income_indexed"]) / 100) * 100),
        "mape": {k: float(v) for k, v in mt["mape"].items()},
        "selected": mt.index[mt["is_selected"]].tolist()[0] if mt["is_selected"].any() else "",
    }


# ---------------------------------------------------------------- pages

def build_pages(f: dict) -> list[Page]:
    """Three pages, charts and numbers only. Navy header band (y 0-64) holds the title and
    compact dropdowns; content fills y 76-708 in white cards (16 px margin, 12 px gutter).
    Bar charts are horizontal with 70% spacing so bars stay slim."""
    L, A, F, R, mp, mm = "Latest", "Affordability", "Forecast", "Regression", "Map Points", "Model Metrics"
    pages = []
    TOP, BOTTOM = 76, 708
    H = BOTTOM - TOP
    SY, SH = 8, 48

    def dropdowns(page: Page, items: list[tuple[str, dict]], width: int = 150) -> None:
        x = 1264 - len(items) * width - (len(items) - 1) * 8
        for name, vis in items:
            page.add(name, x, SY, width, SH, vis, card=False)
            x += width + 8

    def hbar(category: dict, cat_label: str, value: dict, val_label: str, *, colors: dict | None = None,
             fill: str = NAVY, sort_dir: str | None = "Descending", padding: int = 70,
             tooltips: list[dict] | None = None, sort_field: dict | None = None) -> dict:
        roles = {"Category": [proj(category, cat_label)], "Y": [proj(value, val_label)]}
        if tooltips:
            roles["Tooltips"] = tooltips
        objects = {
            "dataPoint": series_color_rules(category, colors) if colors else [{"properties": {"fill": color(fill)}}],
            "labels": [{"properties": {"show": lit(True), "fontSize": lit(9)}}],
            "valueAxis": [{"properties": {"show": lit(False)}}],
            **skinny({"fontSize": lit(9), "maxMarginFactor": int_lit(45)}, padding=padding),
        }
        sort = (sort_field or value, sort_dir) if sort_dir else None
        return chart("clusteredBarChart", roles, sort=sort, objects=objects)

    region = lambda: slicer(col("Suburbs", "region"), "Region", on_dark=True)

    # ---------------------------------------------------------------- 1. Overview
    p = Page("overview", "Overview")
    p.header_bar("Melbourne Rental Affordability", f"{f['quarter']}")
    dropdowns(p, [("type", type_slicer()), ("region", region())], width=180)
    kpis = [(meas(L, "Avg Rent"), "Typical weekly rent"),
            (meas(L, "Avg YoY Growth"), "Rent growth, past year"),
            (meas(L, "Rent to Income %"), "Rent / local income"),
            (meas(L, "% Suburbs in Rental Stress"), "Suburbs in rental stress"),
            (meas(R, "Premium Within 2 km"), "Rent premium within 2 km of a station")]
    w = (1248 - 4 * 12) // 5
    for i, (fld, label) in enumerate(kpis):
        hero = i == 0
        p.add(f"kpi{i}", 16 + i * (w + 12), TOP, w, 92, card(fld, label, size=24 if hero else 20, hero=hero),
              fill=NAVY if hero else None)
    row2, h2 = TOP + 104, 260
    p.add("premium", 16, row2, 400, h2, hbar(col(R, "Distance Band"), "Distance to station",
                                             meas(R, "Train Premium"), "Premium vs >5 km",
                                             sort_field=col(R, "Distance Band"), sort_dir="Ascending",
                                             tooltips=[proj(meas(R, "Premium 95% CI"), "95% range")]),
          "Rent premium by distance to station (all Melbourne)")
    p.add("bands", 428, row2, 400, h2, hbar(col(L, "affordability_band"), "Band", meas(L, "% of Suburbs"),
                                            "% of suburbs", sort_field=col(L, "affordability_band"),
                                            sort_dir="Ascending",
                                            colors={k: v for k, v in BAND_COLORS.items() if k != "Train station"}),
          "Share of suburbs by affordability band")
    p.add("regions", 840, row2, 424, h2, hbar(col("Suburbs", "region"), "Region", meas(L, "Avg Rent"),
                                              "Typical rent", padding=45),
          "Typical weekly rent by region")
    row3 = row2 + h2 + 12
    p.add("trend", 16, row3, 1248, BOTTOM - row3, chart("lineChart", {
        "Category": [proj(col(A, "quarter_end"), "Quarter")],
        "Y": [proj(meas(A, "Avg Rent (all quarters)"), "Typical rent")],
    }, objects={"dataPoint": [{"properties": {"fill": color(NAVY)}}]}), "Typical weekly rent over time")
    pages.append(p)

    # ---------------------------------------------------------------- 2. Best value near trains
    p = Page("value", "Best value near trains")
    p.header_bar("Best value near trains", f"Your income default: ${f['bench_indexed']:,.0f}/wk")
    dropdowns(p, [
        ("type", type_slicer()),
        ("region", region()),
        ("distance", slicer(col("Param Distance", "Distance option"), "Max km", default=1.5, strict=True,
                            on_dark=True)),
        ("budget", slicer(col("Param Budget", "Budget option"), "Max rent $/wk", default=700, strict=True,
                          on_dark=True)),
        ("income", slicer(col("Param Income", "Income option"), "Your income $/wk", default=f["bench_default"],
                          strict=True, on_dark=True)),
        ("weight", slicer(col("Param Weight", "Weight option (%)"), "Weight on cost %", default=70,
                          strict=True, on_dark=True)),
    ], width=124)
    # Left column: summary cards, the top-15 table (15 rows fit), then two breakdowns
    cards = [(meas(L, "Suburbs In Limits"), "Suburbs in your limits"),
             (meas(L, "Best Value Suburb"), "Best value"),
             (meas(L, "Cheapest Rent In Limits"), "Cheapest rent"),
             (meas(L, "Stress Rent Limit"), "30% of your income")]
    for i, (fld, label) in enumerate(cards):
        hero = i == 1
        p.add(f"sum{i}", 16 + i * 153, TOP, 141, 84, card(fld, label, size=13 if hero else 18, hero=hero),
              fill=NAVY if hero else None)
    p.add("bandDonut", 16, TOP + 468, 294, H - 468, chart("donutChart", {
        "Category": [proj(col(L, "distance_band"), "Distance to station")],
        "Y": [proj(meas(L, "Suburbs In Limits"), "Suburbs")],
    }, objects={
        "legend": [{"properties": {"show": lit(True), "position": lit("Right")}}],
        "labels": [{"properties": {"show": lit(True)}}],
    }), "In your limits, by distance to station")
    p.add("regionBars", 322, TOP + 468, 294, H - 468,
          hbar(col("Suburbs", "region"), "Region", meas(L, "Suburbs In Limits"), "Suburbs", padding=40),
          "In your limits, by region")
    p.add("valueTable", 16, TOP + 96, 600, 360, chart("tableEx", {"Values": [
        proj(meas(L, "Top Rank"), "#"),
        proj(col(L, "suburb_group_name"), "Suburb group"),
        proj(meas(L, "Top Station"), "Nearest station"),
        proj(meas(L, "Top Distance Band"), "Distance"),
        proj(meas(L, "Top Rent"), "Rent $/wk"),
        proj(meas(L, "Top Rent / Your Income"), "Of income"),
        proj(meas(L, "Top Score"), "Score"),
    ]}, sort=(meas(L, "Top Rank"), "Ascending"), objects=NO_TOTALS), "Top 15 suburbs by value score")
    rh = (H - 12) // 2
    # Map: only suburbs within the limits (navy = top 15, blue = others), grey = stations
    p.add("map", 628, TOP, 636, rh, chart("map", {
        "Y": [proj(col(mp, "latitude"), "Latitude")],
        "X": [proj(col(mp, "longitude"), "Longitude")],
        "Tooltips": [proj(col(mp, "point_name"), "Place"),
                     proj(meas(mp, "Places"), "Places"),
                     proj(avg(col(mp, "median_weekly_rent")), "Median rent ($/wk)")],
    }, objects={
        "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": meas(mp, "Map Colour")}}}},
                       "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}}],
        "legend": [{"properties": {"show": lit(False)}}],
    }), "Where they are: top 15 (navy), other suburbs in your limits (blue), stations (grey)")
    p.add("valueScatter", 628, TOP + rh + 12, 636, H - rh - 12, chart("scatterChart", {
        "Category": [proj(col(L, "suburb_group_name"), "Suburb group")],
        "X": [proj(meas(L, "Shown Distance km"), "Distance to station (km)")],
        "Y": [proj(meas(L, "Shown Rent"), "Weekly rent ($)")],
        "Tooltips": [proj(meas(L, "Shown Rent / Your Income"), "Share of your income"),
                     proj(meas(L, "Top Rank"), "Rank")],
    }, objects={
        "y1AxisReferenceLine": [{"properties": {
            "show": lit(True), "value": {"expr": meas(L, "Stress Rent Limit")},
            "displayName": lit("30% of your income"),
            "lineColor": color(BAND_COLORS["Rental stress (30%+)"]), "style": lit("dashed"),
            "dataLabelShow": lit(True),
            "shadeShow": lit(True), "shadeRegion": lit("before"),
            "shadeColor": color(BAND_COLORS["Affordable"]), "shadeTransparency": lit(88)},
            "selector": {"id": "1"}}],
        "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": meas(L, "Dot Colour")}}}},
                       "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}}],
        "categoryAxis": [{"properties": {"start": lit(0), "end": {"expr": meas(L, "Max Distance km")}}}],
    }), "Weekly rent vs distance to station", subtitle_measure=meas(L, "Scatter Note"))
    pages.append(p)

    # ---------------------------------------------------------------- 3. Trends & forecast
    p = Page("trends", "Trends & forecast")
    p.header_bar("Rent growth and forecast", f"{f['quarter']}")
    dropdowns(p, [("type", type_slicer()), ("region", region()),
                  ("suburb", slicer(col(F, "suburb_group_name"), "Forecast suburb", default=DEFAULT_SUBURB,
                                    strict=True, on_dark=True))], width=170)
    # 2 x 2 grid, as rearranged by hand in Power BI: heatmap over model error on the left,
    # risers over forecast on the right
    LW, RX, RW = 642, 670, 594
    hh, rh = 375, 343
    yoy2 = meas(A, "YoY Growth (last 2 years)")
    p.add("heatmap", 16, TOP, LW, hh, chart("pivotTable", {
        "Rows": [proj(col(A, "suburb_group_name"), "Suburb group")],
        "Columns": [proj(col(A, "quarter_label"), "Quarter")],
        "Values": [proj(yoy2, "YoY growth")],
    }, objects={
        **NO_SUBTOTALS,
        "values": [{"properties": {"backColor": gradient(yoy2, FALL, "#FFFFFF", HOT)},
                    "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                 "metadata": "Affordability.YoY Growth (last 2 years)"}}],
    }), "Year-on-year rent growth by suburb, last 8 quarters")
    fx = meas(L, "Fastest and Falling YoY")
    risers = hbar(col(L, "suburb_group_name"), "Suburb group", fx, "YoY growth", padding=60)
    risers["objects"]["dataPoint"] = [{"properties": {"fill": gradient(fx, FALL, "#F2E6D8", HOT)},
                                       "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}}]
    p.add("risers", RX, TOP, RW, rh, risers, "Fastest-growing suburbs and suburbs where rent fell (YoY)")
    p.add("forecast", RX, TOP + rh + 12, RW, H - rh - 12, chart("lineChart", {
        "Category": [proj(col(F, "quarter_label"), "Quarter")],
        "Y": [proj(meas(F, "Actual Rent"), "Actual"), proj(meas(F, "Forecast Rent"), "Forecast"),
              proj(meas(F, "Forecast Low (80%)"), "Low (80%)"), proj(meas(F, "Forecast High (80%)"), "High (80%)")],
    }, sort=(col(F, "quarter_label"), "Ascending"), objects={
        "dataPoint": [{"properties": {"fill": color(c)}, "selector": {"metadata": f"Forecast.{m}"}}
                      for m, c in (("Actual Rent", NAVY), ("Forecast Rent", AMBER),
                                   ("Forecast Low (80%)", "#C9CED6"), ("Forecast High (80%)", "#C9CED6"))],
        "legend": [{"properties": {"show": lit(True), "position": lit("Top")}}],
    }), "Median weekly rent: actual and forecast")
    p.add("metrics", 16, TOP + hh + 12, LW, H - hh - 12,
          hbar(col(mm, "model_name"), "Model", meas(mm, "Overall MAPE"), "Average error", sort_dir="Ascending",
               colors={m: (NAVY if m == f["selected"] else "#B8C4D2") for m in f["mape"]},
               tooltips=[proj(meas(mm, "Overall MAE"), "Average error $/wk")]),
          "Average forecast error by model (navy = in use)")
    pages.append(p)
    return pages


def theme() -> dict:
    return {
        "name": THEME_NAME,
        "dataColors": [NAVY, "#2A9D8F", AMBER, "#C8553D", "#7B6D8D", "#4F86C6", MUTED, "#3A8F5C"],
        "background": "#FFFFFF", "foreground": INK, "tableAccent": NAVY,
        "textClasses": {"label": {"fontFace": "Segoe UI", "color": INK}},
    }


def write_report(base: Path, facts: dict) -> None:
    r = base / RPT
    d = r / "definition"
    (d / "pages").mkdir(parents=True, exist_ok=True)
    (r / "definition.pbir").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{SM}"}}}, indent=2), encoding="utf-8")
    (d / "version.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0"}, indent=2), encoding="utf-8")

    theme_dir = r / "StaticResources" / "SharedResources" / "BaseThemes"
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / f"{THEME_NAME}.json").write_text(json.dumps(theme(), indent=2), encoding="utf-8")
    (d / "report.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": THEME_NAME, "type": "SharedResources",
                                          "reportVersionAtImport": {"visual": "2.4.0", "report": "3.0.0",
                                                                    "page": "2.0.0"}}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources", "items": [
            {"name": THEME_NAME, "path": f"BaseThemes/{THEME_NAME}.json", "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True},
    }, indent=2), encoding="utf-8")

    pages = build_pages(facts)
    (d / "pages" / "pages.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.name for p in pages], "activePageName": pages[0].name}, indent=2), encoding="utf-8")
    for p in pages:
        pd_ = d / "pages" / p.name
        (pd_ / "visuals").mkdir(parents=True)
        (pd_ / "page.json").write_text(json.dumps({
            "$schema": f"{SCHEMA}/item/report/definition/page/2.0.0/schema.json",
            "name": p.name, "displayName": p.display, "displayOption": "FitToPage",
            "height": PAGE_H, "width": PAGE_W,
            "objects": {"background": [{"properties": {"color": color(PAGE_BG), "transparency": lit(0)}}],
                        "outspace": [{"properties": {"color": color(PAGE_BG), "transparency": lit(0)}}]},
        }, indent=2), encoding="utf-8")
        for v in p.visuals:
            vd = pd_ / "visuals" / v["name"]
            vd.mkdir()
            (vd / "visual.json").write_text(json.dumps(v, indent=2), encoding="utf-8")


def build(out_dir: Path = OUT_DIR) -> Path:
    facts = report_facts()
    # Replace the generated definition but keep Power BI Desktop's local .pbi folder
    # (cached data and settings), so a regenerated report doesn't need a full reload
    for sub in (SM, RPT):
        if (out_dir / sub).exists():
            for child in (out_dir / sub).iterdir():
                if child.name == ".pbi":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_semantic_model(out_dir, facts)
    write_report(out_dir, facts)
    pbip = out_dir / f"{NAME}.pbip"
    pbip.write_text(json.dumps({
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": RPT}}],
        "settings": {"enableAutoRecovery": True}}, indent=2), encoding="utf-8")
    (out_dir / ".gitignore").write_text("**/.pbi/localSettings.json\n**/.pbi/cache.abf\n", encoding="utf-8")
    return pbip


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    log.info("Wrote %s", build())
