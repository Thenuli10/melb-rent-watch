-- Analytics views: the only objects Power BI and Excel read.
-- Dropped and recreated each run so column changes apply cleanly.

DROP VIEW IF EXISTS analytics.v_affordability CASCADE;
DROP VIEW IF EXISTS analytics.v_rent_growth CASCADE;
DROP VIEW IF EXISTS analytics.v_rent_forecast CASCADE;
DROP VIEW IF EXISTS analytics.v_suburbs CASCADE;
DROP VIEW IF EXISTS analytics.v_suburb_sal CASCADE;
DROP VIEW IF EXISTS analytics.v_stations CASCADE;
DROP VIEW IF EXISTS analytics.v_model_metrics CASCADE;
DROP VIEW IF EXISTS analytics.v_regression_coefficients CASCADE;
DROP VIEW IF EXISTS analytics.v_refresh_info CASCADE;


-- Quarter-on-quarter growth with window functions.
-- The fact table holds every quarter for every series (suppressed cells are
-- NULL rows, not missing rows), so LAG over the ordered partition always
-- compares adjacent quarters. quality check `series_not_contiguous` enforces this.
CREATE VIEW analytics.v_rent_growth AS
SELECT
    f.suburb_key,
    f.property_type_key,
    f.date_key,
    s.suburb_group_name,
    s.region,
    s.lga_name,
    p.label                       AS property_type,
    p.dwelling_type,
    p.bedrooms,
    d.quarter_end,
    d.quarter_label,
    d.financial_year,
    f.median_weekly_rent,
    f.new_bonds_count,
    (f.new_bonds_count >= 10)     AS is_reliable,   -- quality.min_bond_count in config/sources.yaml
    LAG(f.median_weekly_rent) OVER w                       AS prev_q_rent,
    ROUND(100.0 * (f.median_weekly_rent
          / NULLIF(LAG(f.median_weekly_rent) OVER w, 0) - 1), 2) AS qoq_growth_pct,
    ROUND(100.0 * (f.median_weekly_rent
          / NULLIF(LAG(f.median_weekly_rent, 4) OVER w, 0) - 1), 2) AS yoy_growth_pct,
    ROUND(AVG(f.median_weekly_rent) OVER (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW), 2)
                                                            AS rolling_4q_avg_rent,
    CASE WHEN f.median_weekly_rent IS NOT NULL THEN
        RANK() OVER (PARTITION BY d.quarter_end, p.property_type_key
                     ORDER BY f.median_weekly_rent)  -- NULLs sort last, so they don't shift the ranks
    END                                                     AS cheapest_rank
FROM dw.fact_rental_bonds f
JOIN dw.dim_suburb s        USING (suburb_key)
JOIN dw.dim_date d          USING (date_key)
JOIN dw.dim_property_type p USING (property_type_key)
WINDOW w AS (PARTITION BY f.suburb_key, f.property_type_key ORDER BY d.quarter_end);


-- Affordability: rent as a share of local household income.
-- Census income is from August 2021; it is indexed to each quarter with the
-- Victorian Wage Price Index (base = Sep 2021 quarter). For quarters newer than
-- the latest WPI release, the latest available index is used.
CREATE VIEW analytics.v_affordability AS
WITH base AS (
    SELECT wpi_index AS base_wpi FROM dw.ref_wage_index WHERE quarter_end = DATE '2021-09-30'
)
SELECT
    g.*,
    s.median_weekly_hh_income                              AS income_census_2021,
    ROUND(s.median_weekly_hh_income * w.wpi_index / base.base_wpi, 2) AS median_weekly_hh_income,
    w.wpi_index,
    s.nearest_station,
    s.distance_to_station_km,
    s.distance_band,
    s.latitude,
    s.longitude,
    ROUND(100.0 * g.median_weekly_rent
          / NULLIF(s.median_weekly_hh_income * w.wpi_index / base.base_wpi, 0), 1) AS rent_to_income_pct,
    CASE
        WHEN g.median_weekly_rent IS NULL THEN NULL
        WHEN g.median_weekly_rent / NULLIF(s.median_weekly_hh_income * w.wpi_index / base.base_wpi, 0) < 0.25
            THEN 'Affordable'
        WHEN g.median_weekly_rent / NULLIF(s.median_weekly_hh_income * w.wpi_index / base.base_wpi, 0) < 0.30
            THEN 'Moderate'
        ELSE 'Rental stress (30%+)'
    END AS affordability_band
FROM analytics.v_rent_growth g
JOIN dw.dim_suburb s USING (suburb_key)
CROSS JOIN base
CROSS JOIN LATERAL (
    SELECT wpi_index FROM dw.ref_wage_index r
    WHERE r.quarter_end <= g.quarter_end
    ORDER BY r.quarter_end DESC
    LIMIT 1
) w;


-- Actual history plus the latest model's forecasts, for actual-vs-forecast charts
CREATE VIEW analytics.v_rent_forecast AS
WITH latest AS (
    SELECT model_version FROM dw.fact_rent_forecast
    GROUP BY model_version ORDER BY max(created_at) DESC LIMIT 1
)
SELECT
    s.suburb_group_name, s.region, p.label AS property_type, p.dwelling_type, p.bedrooms,
    d.quarter_end, d.quarter_label,
    f.median_weekly_rent AS actual_rent,
    NULL::NUMERIC        AS predicted_rent,
    NULL::NUMERIC        AS lower_80,
    NULL::NUMERIC        AS upper_80,
    NULL::TEXT           AS model_version,
    FALSE                AS is_forecast
FROM dw.fact_rental_bonds f
JOIN dw.dim_suburb s USING (suburb_key)
JOIN dw.dim_date d USING (date_key)
JOIN dw.dim_property_type p USING (property_type_key)
UNION ALL
SELECT
    s.suburb_group_name, s.region, p.label, p.dwelling_type, p.bedrooms,
    d.quarter_end, d.quarter_label,
    NULL, fc.predicted_rent, fc.lower_80, fc.upper_80, fc.model_version, TRUE
FROM dw.fact_rent_forecast fc
JOIN latest USING (model_version)
JOIN dw.dim_suburb s USING (suburb_key)
JOIN dw.dim_date d USING (date_key)
JOIN dw.dim_property_type p USING (property_type_key);


CREATE VIEW analytics.v_suburbs AS
SELECT suburb_key, suburb_group_name, region, lga_name, latitude, longitude,
       nearest_station, distance_to_station_km, distance_band,
       median_weekly_hh_income AS income_census_2021, income_reference_year,
       population_2021, sal_count
FROM dw.dim_suburb;


-- Suburb group -> ABS SAL, e.g. for a shape map keyed on SAL codes
CREATE VIEW analytics.v_suburb_sal AS
SELECT s.suburb_group_name, b.sal_code, b.sal_name, b.match_method,
       b.population_2021, b.median_weekly_hh_income, b.nearest_station, b.distance_to_station_km
FROM dw.bridge_suburb_sal b
JOIN dw.dim_suburb s USING (suburb_key);


CREATE VIEW analytics.v_stations AS
SELECT station_id, station_name, network, latitude, longitude FROM dw.dim_station;


CREATE VIEW analytics.v_model_metrics AS
SELECT * FROM dw.model_backtest_metrics
WHERE model_version = (SELECT model_version FROM dw.model_backtest_metrics
                       ORDER BY created_at DESC LIMIT 1);


CREATE VIEW analytics.v_regression_coefficients AS
SELECT * FROM dw.model_regression_coefficients
WHERE model_version = (SELECT model_version FROM dw.model_regression_coefficients
                       ORDER BY created_at DESC LIMIT 1);


CREATE VIEW analytics.v_refresh_info AS
SELECT
    (SELECT max(d.quarter_end) FROM dw.fact_rental_bonds f JOIN dw.dim_date d USING (date_key)) AS latest_quarter,
    (SELECT max(logged_at) FROM dw.etl_run_log WHERE step = 'load' AND status = 'success')      AS last_successful_load,
    (SELECT max(quarter_end) FROM dw.ref_wage_index)                                            AS latest_wpi_quarter;
