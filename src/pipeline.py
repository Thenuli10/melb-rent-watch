"""Run the full refresh: download -> transform -> load -> quality checks -> models.

    python -m src.pipeline                 # normal scheduled run; exits early if no new quarter
    python -m src.pipeline --force         # reload even if the quarter is already loaded
    python -m src.pipeline --rental-file path/to/workbook.xlsx   # use a manually downloaded workbook
    python -m src.pipeline --skip-models   # ETL only

Exit code is non-zero if any step or quality check fails, so GitHub Actions marks the run failed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.common import INTERIM_DIR, connect, load_config, log, setup_logging
from src.ingest import download_income, download_rental, download_stations
from src.load import load_warehouse as lw
from src.transform import clean_suburbs, geo_distance, suburb_attributes
from src.transform.reshape_rental import reshape_workbook


def already_loaded(conn, quarter: str) -> bool:
    """True if the warehouse already has a successful load of this rental quarter."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('dw.etl_run_log') IS NOT NULL")
        if not cur.fetchone()[0]:
            return False
        cur.execute("""SELECT 1 FROM dw.etl_run_log
                       WHERE step = 'load' AND source = 'rental' AND quarter = %s AND status = 'success'
                       LIMIT 1""", (quarter,))
        return cur.fetchone() is not None


def transform(downloads: dict) -> dict[str, pd.DataFrame]:
    cfg = load_config()
    rental = reshape_workbook(downloads["rental"].path)
    log.info("Rental: %s rows, %d suburb groups, %s to %s", f"{len(rental):,}", rental["suburb_group"].nunique(),
             rental["quarter_end"].min().date(), rental["quarter_end"].max().date())

    b = downloads["boundaries"]
    sal_points = geo_distance.load_vic_suburbs(b["sal"].path, b["gccsa"].path, b["lga"].path)
    gmel = geo_distance.gmel_polygon_wgs84(b["gccsa"].path)
    stations = geo_distance.load_train_stations(downloads["gtfs"].path, gmel)
    log.info("Suburbs: %d in Victoria (%d in Greater Melbourne); stations: %d",
             len(sal_points), int(sal_points["in_gmel"].sum()), len(stations))

    crosswalk = clean_suburbs.build_crosswalk(rental["suburb_group"].unique().tolist(), sal_points)

    income = suburb_attributes.read_abs_sdmx_csv(downloads["census"]["median_hh_income"].path,
                                                 "median_weekly_hh_income")
    population = suburb_attributes.read_abs_sdmx_csv(downloads["census"]["population"].path, "population")
    sal_table = suburb_attributes.build_sal_table(pd.DataFrame(sal_points.drop(columns="geometry")),
                                                  income, population, stations)
    suburbs = suburb_attributes.build_suburb_groups(
        crosswalk, sal_table, rental[["suburb_group", "region"]].drop_duplicates(), stations,
        cfg["income"]["reference_year"])

    bridge = crosswalk.merge(
        sal_table[["sal_code", "population", "median_weekly_hh_income", "nearest_station",
                   "distance_to_station_km"]], on="sal_code", how="left")
    bridge = bridge.rename(columns={"population": "population_2021"})
    bridge["distance_to_station_km"] = bridge["distance_to_station_km"].round(2)

    wpi = suburb_attributes.read_wage_index(downloads["wage_index"].path)

    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    rental.to_csv(INTERIM_DIR / "rental_long.csv", index=False)
    suburbs.to_csv(INTERIM_DIR / "suburbs.csv", index=False)
    stations.to_csv(INTERIM_DIR / "stations.csv", index=False)

    return {
        "staging.rental": rental,
        "staging.suburb": suburbs,
        "staging.suburb_sal": bridge,
        "staging.station": stations,
        "staging.wage_index": wpi,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="reload even if this quarter is already loaded")
    parser.add_argument("--rental-file", type=Path, help="use a manually downloaded DFFH workbook")
    parser.add_argument("--skip-models", action="store_true", help="skip regression and forecast")
    args = parser.parse_args(argv)
    setup_logging()

    # 1. Download
    if args.rental_file:
        rental_dl = download_rental.register_local_file(args.rental_file)
    else:
        rental_dl = download_rental.run()
    downloads = {"rental": rental_dl}

    conn = connect()
    try:
        lw.apply_ddl(conn)
        if already_loaded(conn, rental_dl.quarter) and not args.force:
            log.info("Quarter %s is already loaded; nothing to do (use --force to reload)", rental_dl.quarter)
            return 0

        batch_id = lw.new_batch_id(conn)
        log.info("Starting batch %d for rental quarter %s", batch_id, rental_dl.quarter)

        income_dl = download_income.run()
        geo_dl = download_stations.run()
        downloads["census"] = {k: income_dl[k] for k in ("median_hh_income", "population")}
        downloads["wage_index"] = income_dl["wage_index"]
        downloads["gtfs"] = geo_dl.pop("gtfs")
        downloads["boundaries"] = geo_dl

        all_files = [rental_dl, downloads["wage_index"], downloads["gtfs"],
                     *downloads["census"].values(), *downloads["boundaries"].values()]
        for d in all_files:
            lw.log_event(conn, batch_id, "download", "success" if d.is_new else "skipped", source=d.source,
                         file_name=d.path.name, quarter=d.quarter, checksum=d.checksum,
                         detail="new file" if d.is_new else "already on disk")

        # 2. Transform
        try:
            frames = transform(downloads)
        except clean_suburbs.UnmatchedSuburbError as e:
            lw.log_event(conn, batch_id, "transform", "failed", source="rental", quarter=rental_dl.quarter,
                         detail=str(e))
            log.error("%s", e)
            return 1

        # 3. Load
        counts = lw.load(conn, batch_id, frames)
        lw.create_views(conn)

        # 4. Quality checks
        results = lw.run_quality_checks(conn)
        failed = [r for r in results if not r.passed]
        for r in results:
            log.info("check %-40s %s", r.name, "ok" if r.passed else f"FAILED ({r.failures})")
            lw.log_event(conn, batch_id, "quality_check", "success" if r.passed else "failed",
                         source=r.name, row_count=r.failures)
        lw.log_event(conn, batch_id, "load", "failed" if failed else "success", source="rental",
                     file_name=rental_dl.path.name, quarter=rental_dl.quarter, checksum=rental_dl.checksum,
                     row_count=counts["staging.rental"],
                     detail=f"failed checks: {[r.name for r in failed]}" if failed else None)
        if failed:
            log.error("%d quality check(s) failed", len(failed))
            return 1

        # 5. Models
        if not args.skip_models:
            from src.models import forecast, regression

            for name, module in (("regression", regression), ("forecast", forecast)):
                try:
                    detail = module.run()
                    lw.log_event(conn, batch_id, "model", "success", source=name, detail=detail)
                except Exception as e:  # a model failure should not hide a good load
                    lw.log_event(conn, batch_id, "model", "failed", source=name, detail=repr(e)[:500])
                    log.exception("%s model failed", name)
                    return 1
    finally:
        conn.close()

    log.info("Pipeline finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
