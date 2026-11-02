-- Dimensions, reference tables and the ETL run log.

CREATE TABLE IF NOT EXISTS dw.dim_suburb (
    suburb_key              SERIAL PRIMARY KEY,
    suburb_group_name       TEXT NOT NULL UNIQUE,
    lga_name                TEXT,
    region                  TEXT,            -- DFFH region, e.g. Inner Melbourne, Outer Western Melbourne
    latitude                NUMERIC(9,6),    -- population-weighted point of the group's suburbs
    longitude               NUMERIC(9,6),
    nearest_station         TEXT,
    distance_to_station_km  NUMERIC(6,2),
    distance_band           TEXT,
    median_weekly_hh_income NUMERIC(10,2),   -- ABS Census 2021, not indexed
    income_reference_year   SMALLINT,
    population_2021         INT,
    sal_count               SMALLINT,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dw.bridge_suburb_sal (
    suburb_key              INT REFERENCES dw.dim_suburb,
    sal_code                TEXT,
    sal_name                TEXT NOT NULL,
    match_method            TEXT,            -- exact / fuzzy / override
    match_score             NUMERIC(5,1),
    population_2021         INT,
    median_weekly_hh_income NUMERIC(10,2),
    nearest_station         TEXT,
    distance_to_station_km  NUMERIC(6,2),
    PRIMARY KEY (suburb_key, sal_code)
);

CREATE TABLE IF NOT EXISTS dw.dim_date (
    date_key        INT PRIMARY KEY,         -- yyyymmdd of quarter end
    quarter_end     DATE NOT NULL UNIQUE,
    year            SMALLINT,
    quarter         SMALLINT,
    quarter_label   TEXT,                    -- e.g. '2026 Q1'
    financial_year  TEXT                     -- e.g. 'FY2025-26'
);

CREATE TABLE IF NOT EXISTS dw.dim_property_type (
    property_type_key SERIAL PRIMARY KEY,
    dwelling_type     TEXT NOT NULL,         -- 'Flat' / 'House'
    bedrooms          SMALLINT NOT NULL,
    label             TEXT,                  -- '2 bed flat'
    UNIQUE (dwelling_type, bedrooms)
);

-- Train stations, for the second map layer in reporting
CREATE TABLE IF NOT EXISTS dw.dim_station (
    station_key  SERIAL PRIMARY KEY,
    station_id   TEXT NOT NULL UNIQUE,       -- GTFS parent stop_id, e.g. vic:rail:FSS
    station_name TEXT NOT NULL,
    network      TEXT NOT NULL,              -- Metro / V/Line
    latitude     NUMERIC(9,6) NOT NULL,
    longitude    NUMERIC(9,6) NOT NULL
);

-- ABS Wage Price Index (Victoria), used to index Census 2021 income forward
CREATE TABLE IF NOT EXISTS dw.ref_wage_index (
    quarter_end DATE PRIMARY KEY,
    wpi_index   NUMERIC(8,2) NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS dw.etl_batch_seq;

CREATE TABLE IF NOT EXISTS dw.etl_run_log (
    log_id     BIGSERIAL PRIMARY KEY,
    batch_id   INT NOT NULL,
    step       TEXT NOT NULL,                -- download / load / quality_check / model
    source     TEXT,
    file_name  TEXT,
    quarter    TEXT,
    checksum   TEXT,
    row_count  INT,
    status     TEXT NOT NULL,                -- success / skipped / failed
    detail     TEXT,
    logged_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_etl_run_log_batch ON dw.etl_run_log (batch_id);
