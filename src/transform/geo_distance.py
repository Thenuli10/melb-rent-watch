"""Suburb geometry, LGA lookup and distance from each suburb to the nearest train station.

Suburb points are `representative_point()` of each SAL polygon (always inside
the suburb, unlike a plain centroid). Distances are great-circle (haversine)
straight-line distances in km.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

from src.common import load_config

EARTH_RADIUS_KM = 6371.0088
WGS84 = "EPSG:4326"
MGA55 = "EPSG:7855"  # GDA2020 / MGA zone 55, metres, used for geometry operations


def load_vic_suburbs(sal_zip: Path, gccsa_zip: Path, lga_zip: Path | None = None) -> gpd.GeoDataFrame:
    """Victorian SALs with a representative point, a Greater Melbourne flag and LGA name."""
    cfg = load_config()["boundaries"]
    sal = gpd.read_file(f"zip://{sal_zip}", columns=["SAL_CODE21", "SAL_NAME21", "STE_CODE21"])
    sal = sal[(sal["STE_CODE21"] == cfg["state_code"]) & sal.geometry.notna()].to_crs(MGA55)

    pts = sal.copy()
    pts["geometry"] = sal.geometry.representative_point()

    gcc = gpd.read_file(f"zip://{gccsa_zip}", columns=["GCC_CODE21"])
    gmel = gcc[gcc["GCC_CODE21"] == cfg["gccsa_code"]].to_crs(MGA55)
    pts["in_gmel"] = pts.within(gmel.geometry.union_all())

    if lga_zip is not None:
        lga = gpd.read_file(f"zip://{lga_zip}", columns=["LGA_NAME21", "STE_CODE21"])
        lga = lga[(lga["STE_CODE21"] == cfg["state_code"]) & lga.geometry.notna()].to_crs(MGA55)
        joined = gpd.sjoin(pts, lga[["LGA_NAME21", "geometry"]], how="left", predicate="within")
        pts["lga_name"] = joined.groupby(level=0)["LGA_NAME21"].first()
    else:
        pts["lga_name"] = None

    wgs = pts.to_crs(WGS84)
    out = gpd.GeoDataFrame({
        "sal_code": pts["SAL_CODE21"].astype(str),
        "sal_name": pts["SAL_NAME21"],
        "in_gmel": pts["in_gmel"].astype(bool),
        "lga_name": pts["lga_name"],
        "latitude": wgs.geometry.y,
        "longitude": wgs.geometry.x,
    }, geometry=wgs.geometry, crs=WGS84)
    return out.reset_index(drop=True)


def _read_gtfs_stops(outer: zipfile.ZipFile, folder: str) -> pd.DataFrame:
    inner = zipfile.ZipFile(io.BytesIO(outer.read(f"{folder}/google_transit.zip")))
    stops = pd.read_csv(inner.open("stops.txt"), dtype={"stop_id": str, "parent_station": str})
    return stops


def clean_station_name(name: str) -> str:
    name = str(name).strip()
    for suffix in (" Railway Station", " Station"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def load_train_stations(gtfs_zip: Path, gmel_polygon=None) -> pd.DataFrame:
    """Parent train stations (location_type = 1) from the PTV GTFS feed.

    Metro stations are always kept. V/Line stations are kept only inside
    Greater Melbourne (e.g. Melton, Tarneit), when enabled in config.
    """
    cfg = load_config()["stations"]
    outer = zipfile.ZipFile(gtfs_zip)

    frames = []
    for network, folder in cfg["feeds"].items():
        if network == "vline" and not cfg["include_vline_in_gmel"]:
            continue
        stops = _read_gtfs_stops(outer, folder)
        parents = stops[stops["location_type"] == 1].copy()
        parents["network"] = "Metro" if network == "metro_train" else "V/Line"
        frames.append(parents)
    st = pd.concat(frames, ignore_index=True)

    if gmel_polygon is not None:
        pts = gpd.GeoDataFrame(st, geometry=gpd.points_from_xy(st["stop_lon"], st["stop_lat"]), crs=WGS84)
        st = st[(st["network"] == "Metro") | pts.within(gmel_polygon).values]

    # Shared stations (e.g. Southern Cross) appear in both feeds with the same parent id
    st = st.sort_values("network").drop_duplicates("stop_id", keep="first")
    return pd.DataFrame({
        "station_id": st["stop_id"],
        "station_name": st["stop_name"].map(clean_station_name),
        "network": st["network"],
        "latitude": st["stop_lat"].astype(float),
        "longitude": st["stop_lon"].astype(float),
    }).reset_index(drop=True)


def gmel_polygon_wgs84(gccsa_zip: Path):
    gcc = gpd.read_file(f"zip://{gccsa_zip}", columns=["GCC_CODE21"])
    return gcc[gcc["GCC_CODE21"] == load_config()["boundaries"]["gccsa_code"]].to_crs(WGS84).geometry.union_all()


def nearest_station(points: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """For each row with latitude/longitude, the nearest station and its distance in km."""
    tree = BallTree(np.radians(stations[["latitude", "longitude"]].to_numpy()), metric="haversine")
    dist, idx = tree.query(np.radians(points[["latitude", "longitude"]].to_numpy()), k=1)
    return pd.DataFrame({
        "nearest_station": stations["station_name"].to_numpy()[idx[:, 0]],
        "distance_to_station_km": dist[:, 0] * EARTH_RADIUS_KM,
    }, index=points.index)


def distance_band(km: float) -> str | None:
    if pd.isna(km):
        return None
    for upper, label in load_config()["distance_bands"]:
        if km < float(upper):
            return label
    raise ValueError(km)
