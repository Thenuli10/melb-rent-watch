"""Build one row of attributes per DFFH suburb group (the dim_suburb grain).

SAL-level values are combined to the group using Census 2021 population as
weights:
- location: population-weighted mean of SAL representative points
- distance to station: population-weighted mean of SAL distances (plan 5.4 step 6)
- nearest station: nearest to the group's weighted point
- income: population-weighted mean of SAL median household incomes. A mean
  of medians is an approximation of the group median; documented in the README.
- LGA: the LGA of the most populous SAL in the group
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.transform.geo_distance import distance_band, nearest_station


def read_abs_sdmx_csv(path: Path, value_name: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"REGION": str})
    out = df[["REGION", "OBS_VALUE"]].rename(columns={"REGION": "sal_code", "OBS_VALUE": value_name})
    out[value_name] = pd.to_numeric(out[value_name], errors="coerce")
    return out


def read_wage_index(path: Path) -> pd.DataFrame:
    """ABS WPI SDMX CSV -> quarter_end, wpi_index."""
    df = pd.read_csv(path)
    period = pd.PeriodIndex(df["TIME_PERIOD"].str.replace("-", ""), freq="Q")
    out = pd.DataFrame({"quarter_end": period.end_time.normalize(), "wpi_index": df["OBS_VALUE"].astype(float)})
    return out.sort_values("quarter_end").reset_index(drop=True)


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return float(values.mean()) if values.notna().any() else np.nan
    return float(np.average(values[mask], weights=weights[mask]))


def build_sal_table(sal_points: pd.DataFrame, income: pd.DataFrame, population: pd.DataFrame,
                    stations: pd.DataFrame) -> pd.DataFrame:
    """SAL-level table: location, LGA, income, population and nearest station."""
    sal = sal_points.merge(income, on="sal_code", how="left").merge(population, on="sal_code", how="left")
    # ABS reports 0 for SALs too small to publish a median
    sal.loc[sal["median_weekly_hh_income"] <= 0, "median_weekly_hh_income"] = np.nan
    sal = sal.join(nearest_station(sal, stations))
    return sal


def build_suburb_groups(crosswalk: pd.DataFrame, sal: pd.DataFrame, regions: pd.DataFrame,
                        stations: pd.DataFrame, income_year: int) -> pd.DataFrame:
    """One row per suburb group with the columns of dw.dim_suburb."""
    m = crosswalk[["suburb_group", "sal_code"]].merge(sal, on="sal_code", how="left", validate="many_to_one")
    if m["latitude"].isna().any():
        missing = m.loc[m["latitude"].isna(), "sal_code"].tolist()
        raise RuntimeError(f"Crosswalk SAL codes missing from boundary file: {missing}")

    rows = []
    for group, g in m.groupby("suburb_group"):
        w = g["population"].fillna(0)
        rows.append({
            "suburb_group_name": group,
            "lga_name": g.sort_values("population", ascending=False)["lga_name"].iloc[0],
            "latitude": _weighted_mean(g["latitude"], w),
            "longitude": _weighted_mean(g["longitude"], w),
            "distance_to_station_km": _weighted_mean(g["distance_to_station_km"], w),
            "median_weekly_hh_income": _weighted_mean(g["median_weekly_hh_income"], w),
            "population_2021": int(w.sum()),
            "sal_count": len(g),
        })
    out = pd.DataFrame(rows)
    out["nearest_station"] = nearest_station(out, stations)["nearest_station"]
    out["distance_band"] = out["distance_to_station_km"].map(distance_band)
    out["income_reference_year"] = income_year
    out = out.merge(regions, left_on="suburb_group_name", right_on="suburb_group", how="left").drop(
        columns="suburb_group")
    out["latitude"] = out["latitude"].round(6)
    out["longitude"] = out["longitude"].round(6)
    out["distance_to_station_km"] = out["distance_to_station_km"].round(2)
    out["median_weekly_hh_income"] = out["median_weekly_hh_income"].round(2)
    return out[["suburb_group_name", "lga_name", "region", "latitude", "longitude", "nearest_station",
                "distance_to_station_km", "distance_band", "median_weekly_hh_income", "income_reference_year",
                "population_2021", "sal_count"]]
