-- Schemas and staging tables.
-- staging:   typed copies of each source, truncated and reloaded every run
-- dw:        star schema
-- analytics: views for Power BI and Excel (the only schema reporting tools read)
-- All DDL is idempotent so the pipeline can apply it on every run.

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS staging.rental (
    region          TEXT NOT NULL,
    suburb_group    TEXT NOT NULL,
    quarter_end     DATE NOT NULL,
    dwelling_type   TEXT NOT NULL,
    bedrooms        SMALLINT NOT NULL,
    median_rent     NUMERIC(8,2),
    new_bonds       INT
);

CREATE TABLE IF NOT EXISTS staging.suburb (
    suburb_group_name       TEXT NOT NULL,
    lga_name                TEXT,
    region                  TEXT,
    latitude                NUMERIC(9,6),
    longitude               NUMERIC(9,6),
    nearest_station         TEXT,
    distance_to_station_km  NUMERIC(6,2),
    distance_band           TEXT,
    median_weekly_hh_income NUMERIC(10,2),
    income_reference_year   SMALLINT,
    population_2021         INT,
    sal_count               SMALLINT
);

CREATE TABLE IF NOT EXISTS staging.suburb_sal (
    suburb_group            TEXT NOT NULL,
    sal_code                TEXT NOT NULL,
    sal_name                TEXT NOT NULL,
    match_method            TEXT,
    match_score             NUMERIC(5,1),
    population_2021         INT,
    median_weekly_hh_income NUMERIC(10,2),
    nearest_station         TEXT,
    distance_to_station_km  NUMERIC(6,2)
);

CREATE TABLE IF NOT EXISTS staging.station (
    station_id   TEXT NOT NULL,
    station_name TEXT NOT NULL,
    network      TEXT NOT NULL,
    latitude     NUMERIC(9,6) NOT NULL,
    longitude    NUMERIC(9,6) NOT NULL
);

CREATE TABLE IF NOT EXISTS staging.benchmark_income (
    benchmark               TEXT NOT NULL,
    median_weekly_hh_income NUMERIC(10,2) NOT NULL,
    reference_year          SMALLINT NOT NULL
);

CREATE TABLE IF NOT EXISTS staging.wage_index (
    quarter_end DATE NOT NULL,
    wpi_index   NUMERIC(8,2) NOT NULL
);
