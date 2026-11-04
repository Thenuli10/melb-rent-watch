"""Reshape the wide DFFH moving-annual-rents workbook into one row per
suburb group x quarter x property type.

Sheet layout (one sheet per dwelling type):
    row ~1: quarter labels ("Mar 2000"), each repeated over a Count and a Median column
    row ~2: metric labels ("Count", "Median")
    data:   col A = region (only on the first row of each region), col B = suburb group
Header rows are located by content, not by fixed offsets.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from src.common import load_config

QUARTER_LABEL = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})$")
MONTH_NUM = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


class LayoutError(RuntimeError):
    """The workbook no longer looks the way this parser expects."""


def parse_quarter_label(label: str) -> pd.Timestamp:
    """'Mar 2026' -> 2026-03-31 (quarter-end date)."""
    m = QUARTER_LABEL.match(str(label).strip())
    if not m:
        raise ValueError(f"Not a quarter label: {label!r}")
    month = MONTH_NUM[m.group(1)[:3]]
    if month % 3:
        raise ValueError(f"Month {m.group(1)} is not a quarter end: {label!r}")
    return pd.Timestamp(year=int(m.group(2)), month=month, day=1) + pd.offsets.MonthEnd(0)


def _find_header_rows(raw: pd.DataFrame, metrics: list[str]) -> tuple[int, int]:
    """Return (quarter_row, metric_row) indices by scanning the first rows."""
    for i in range(min(len(raw), 20)):
        values = [str(v).strip() for v in raw.iloc[i, 2:].dropna()]
        if values and sum(bool(QUARTER_LABEL.match(v)) for v in values) >= 0.9 * len(values):
            nxt = {str(v).strip() for v in raw.iloc[i + 1, 2:].dropna()}
            if nxt and nxt <= set(metrics):
                return i, i + 1
    raise LayoutError("Could not find the quarter/metric header rows")


def reshape_sheet(raw: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Turn one raw sheet (read with header=None) into long format."""
    q_row, m_row = _find_header_rows(raw, cfg["expected_metrics"])

    quarters = raw.iloc[q_row, 2:].ffill()
    metrics = raw.iloc[m_row, 2:]
    columns = pd.MultiIndex.from_arrays(
        [quarters.astype(str).str.strip(), metrics.astype(str).str.strip()], names=["quarter_label", "metric"])

    body = raw.iloc[m_row + 1:].reset_index(drop=True)
    labels = pd.DataFrame({"region": body.iloc[:, 0].ffill(), "suburb_group": body.iloc[:, 1]})
    values = body.iloc[:, 2:]
    values.columns = columns

    # Drop trailing empty columns and anything that is not a known metric
    keep = [c for c in columns if c[1] in cfg["expected_metrics"]]
    values = values[keep]

    mask = labels["suburb_group"].notna() & ~labels["suburb_group"].astype(str).str.strip().isin(cfg["skip_rows"])
    labels, values = labels[mask], values[mask]
    labels["suburb_group"] = labels["suburb_group"].astype(str).str.strip()
    labels["region"] = labels["region"].astype(str).str.strip()

    values.index = pd.MultiIndex.from_frame(labels)
    long = values.stack(level=["quarter_label", "metric"], future_stack=True).rename("value").reset_index()

    nulls = {t.lower() for t in cfg["null_tokens"]}
    long["value"] = long["value"].map(
        lambda v: np.nan if v is None or (isinstance(v, str) and v.strip().lower() in nulls) else v)
    long["value"] = pd.to_numeric(long["value"], errors="raise")

    key = ["region", "suburb_group", "quarter_label", "metric"]
    dupes = long.duplicated(key)
    if dupes.any():
        raise LayoutError(f"Duplicate suburb/quarter cells, e.g. {long.loc[dupes, key].head(3).values.tolist()}")
    tidy = (long.set_index(key)["value"].unstack("metric")
                .reset_index()
                .rename(columns={"Median": "median_rent", "Count": "new_bonds"}))
    tidy.columns.name = None
    tidy["quarter_end"] = tidy["quarter_label"].map(parse_quarter_label)
    return tidy.drop(columns="quarter_label")


def reshape_workbook(path: Path) -> pd.DataFrame:
    cfg = load_config()["rental"]
    sheets = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
    missing = set(cfg["sheets"]) - set(sheets)
    if missing:
        raise LayoutError(f"Workbook is missing sheets: {sorted(missing)}")

    frames = []
    for sheet_name, (dwelling_type, bedrooms) in cfg["sheets"].items():
        df = reshape_sheet(sheets[sheet_name], cfg)
        df["dwelling_type"] = dwelling_type
        df["bedrooms"] = int(bedrooms)
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)

    unknown = set(out["region"]) - set(cfg["metro_regions"]) - {
        "Geelong", "Ballarat", "Bendigo", "Other Regional Centres"}
    if unknown:
        raise LayoutError(f"Unknown regions in workbook: {sorted(unknown)}; update metro_regions in sources.yaml")
    out = out[out["region"].isin(cfg["metro_regions"])]

    out["new_bonds"] = out["new_bonds"].astype("Int64")
    out = out[["region", "suburb_group", "quarter_end", "dwelling_type", "bedrooms", "median_rent", "new_bonds"]]
    return out.sort_values(["suburb_group", "dwelling_type", "bedrooms", "quarter_end"]).reset_index(drop=True)


if __name__ == "__main__":
    import sys

    df = reshape_workbook(Path(sys.argv[1]))
    print(df.head(10).to_string())
    print(df.shape, df["quarter_end"].min(), df["quarter_end"].max(), df["suburb_group"].nunique())
