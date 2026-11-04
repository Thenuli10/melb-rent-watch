"""Load transformed data into PostgreSQL: DDL, staging COPY, staging -> dw merge,
analytics views, quality checks and the run log."""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.common import SQL_DIR, load_config, log

DDL_FILES = ["01_schemas.sql", "02_dimensions.sql", "03_fact.sql"]
STAGING_COLUMNS = {
    "staging.rental": ["region", "suburb_group", "quarter_end", "dwelling_type", "bedrooms", "median_rent",
                       "new_bonds"],
    "staging.suburb": ["suburb_group_name", "lga_name", "region", "latitude", "longitude", "nearest_station",
                       "distance_to_station_km", "distance_band", "median_weekly_hh_income",
                       "income_reference_year", "population_2021", "sal_count"],
    "staging.suburb_sal": ["suburb_group", "sal_code", "sal_name", "match_method", "match_score",
                           "population_2021", "median_weekly_hh_income", "nearest_station",
                           "distance_to_station_km"],
    "staging.station": ["station_id", "station_name", "network", "latitude", "longitude"],
    "staging.wage_index": ["quarter_end", "wpi_index"],
    "staging.benchmark_income": ["benchmark", "median_weekly_hh_income", "reference_year"],
}


class QualityCheckError(RuntimeError):
    pass


@dataclass
class CheckResult:
    name: str
    failures: int

    @property
    def passed(self) -> bool:
        return self.failures == 0


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def apply_ddl(conn) -> None:
    with conn.cursor() as cur:
        for name in DDL_FILES:
            cur.execute(read_sql(name))
    conn.commit()


def create_views(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(read_sql("04_views.sql"))
    conn.commit()


def new_batch_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('dw.etl_batch_seq')")
        batch_id = cur.fetchone()[0]
    conn.commit()
    return batch_id


def log_event(conn, batch_id: int, step: str, status: str, *, source=None, file_name=None, quarter=None,
              checksum=None, row_count=None, detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO dw.etl_run_log
               (batch_id, step, source, file_name, quarter, checksum, row_count, status, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (batch_id, step, source, file_name, quarter, checksum, row_count, status, detail))
    conn.commit()


def _to_python(v):
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, pd.Timestamp):
        return v.date()
    return v


def copy_frame(cur, table: str, df: pd.DataFrame) -> int:
    cols = STAGING_COLUMNS[table]
    missing = set(cols) - set(df.columns)
    if missing:
        raise ValueError(f"{table}: frame is missing columns {sorted(missing)}")
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for row in df[cols].itertuples(index=False, name=None):
            cp.write_row([_to_python(v) for v in row])
    return len(df)


def load(conn, batch_id: int, frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Truncate staging, COPY every frame in, then merge into dw in one transaction."""
    counts = {}
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(STAGING_COLUMNS))
        for table, df in frames.items():
            counts[table] = copy_frame(cur, table, df)
            log.info("Staged %s rows into %s", f"{counts[table]:,}", table)
        cur.execute("SELECT set_config('melb.batch_id', %s, true)", (str(batch_id),))
        cur.execute(read_sql("06_load_dw.sql"))
    conn.commit()
    return counts


def parse_checks(sql: str) -> list[tuple[str, str]]:
    """Split 05_quality_checks.sql into (name, query) pairs."""
    parts = re.split(r"^--\s*check:\s*(\S+)\s*$", sql, flags=re.MULTILINE)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def run_quality_checks(conn) -> list[CheckResult]:
    params = load_config()["quality"]
    results = []
    with conn.cursor() as cur:
        for name, query in parse_checks(read_sql("05_quality_checks.sql")):
            cur.execute(query.format(**params))
            results.append(CheckResult(name, int(cur.fetchone()[0])))
    conn.commit()
    return results
