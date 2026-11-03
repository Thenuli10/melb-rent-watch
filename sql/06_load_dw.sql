-- Move staging into the star schema. Runs in one transaction after staging is
-- loaded. The batch id is passed in as the session setting melb.batch_id.

-- Property types
INSERT INTO dw.dim_property_type (dwelling_type, bedrooms, label)
SELECT DISTINCT dwelling_type, bedrooms, bedrooms || ' bed ' || lower(dwelling_type)
FROM staging.rental
ON CONFLICT (dwelling_type, bedrooms) DO NOTHING;

-- Date spine: every quarter from the first rental quarter to four quarters past
-- the latest, so forecasts always have a date to reference
INSERT INTO dw.dim_date (date_key, quarter_end, year, quarter, quarter_label, financial_year)
SELECT
    to_char(q, 'YYYYMMDD')::int,
    q,
    EXTRACT(YEAR FROM q),
    EXTRACT(QUARTER FROM q),
    EXTRACT(YEAR FROM q) || ' Q' || EXTRACT(QUARTER FROM q),
    'FY' || (EXTRACT(YEAR FROM q) - CASE WHEN EXTRACT(MONTH FROM q) <= 6 THEN 1 ELSE 0 END)
         || '-' || right((EXTRACT(YEAR FROM q) + CASE WHEN EXTRACT(MONTH FROM q) <= 6 THEN 0 ELSE 1 END)::text, 2)
FROM (
    SELECT (date_trunc('quarter', m) + interval '3 months' - interval '1 day')::date AS q
    FROM generate_series(
        (SELECT min(quarter_end) FROM staging.rental),
        (SELECT max(quarter_end) FROM staging.rental) + interval '12 months',
        interval '3 months') AS m
) quarters
ON CONFLICT (date_key) DO NOTHING;

-- Suburbs (type 1: attributes are overwritten)
INSERT INTO dw.dim_suburb AS t (
    suburb_group_name, lga_name, region, latitude, longitude, nearest_station,
    distance_to_station_km, distance_band, median_weekly_hh_income, income_reference_year,
    population_2021, sal_count, updated_at)
SELECT suburb_group_name, lga_name, region, latitude, longitude, nearest_station,
       distance_to_station_km, distance_band, median_weekly_hh_income, income_reference_year,
       population_2021, sal_count, now()
FROM staging.suburb
ON CONFLICT (suburb_group_name) DO UPDATE SET
    lga_name = EXCLUDED.lga_name,
    region = EXCLUDED.region,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    nearest_station = EXCLUDED.nearest_station,
    distance_to_station_km = EXCLUDED.distance_to_station_km,
    distance_band = EXCLUDED.distance_band,
    median_weekly_hh_income = EXCLUDED.median_weekly_hh_income,
    income_reference_year = EXCLUDED.income_reference_year,
    population_2021 = EXCLUDED.population_2021,
    sal_count = EXCLUDED.sal_count,
    updated_at = now();

-- Suburb group -> SAL bridge, rebuilt for the groups in this load
DELETE FROM dw.bridge_suburb_sal b
USING dw.dim_suburb s, staging.suburb st
WHERE b.suburb_key = s.suburb_key AND s.suburb_group_name = st.suburb_group_name;

INSERT INTO dw.bridge_suburb_sal (
    suburb_key, sal_code, sal_name, match_method, match_score, population_2021,
    median_weekly_hh_income, nearest_station, distance_to_station_km)
SELECT s.suburb_key, x.sal_code, x.sal_name, x.match_method, x.match_score, x.population_2021,
       x.median_weekly_hh_income, x.nearest_station, x.distance_to_station_km
FROM staging.suburb_sal x
JOIN dw.dim_suburb s ON s.suburb_group_name = x.suburb_group;

-- Stations
INSERT INTO dw.dim_station (station_id, station_name, network, latitude, longitude)
SELECT station_id, station_name, network, latitude, longitude FROM staging.station
ON CONFLICT (station_id) DO UPDATE SET
    station_name = EXCLUDED.station_name,
    network = EXCLUDED.network,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude;

-- Benchmark incomes
INSERT INTO dw.ref_benchmark_income (benchmark, median_weekly_hh_income, reference_year)
SELECT benchmark, median_weekly_hh_income, reference_year FROM staging.benchmark_income
ON CONFLICT (benchmark) DO UPDATE SET
    median_weekly_hh_income = EXCLUDED.median_weekly_hh_income,
    reference_year = EXCLUDED.reference_year;

-- Wage index (revisions overwrite)
INSERT INTO dw.ref_wage_index (quarter_end, wpi_index)
SELECT quarter_end, wpi_index FROM staging.wage_index
ON CONFLICT (quarter_end) DO UPDATE SET wpi_index = EXCLUDED.wpi_index;

-- Fact: upsert, so reloading a quarter replaces it (DFFH revises recent quarters)
INSERT INTO dw.fact_rental_bonds AS t (
    suburb_key, date_key, property_type_key, median_weekly_rent, new_bonds_count, load_batch_id)
SELECT s.suburb_key, d.date_key, p.property_type_key, r.median_rent, r.new_bonds,
       current_setting('melb.batch_id')::int
FROM staging.rental r
JOIN dw.dim_suburb s        ON s.suburb_group_name = r.suburb_group
JOIN dw.dim_date d          ON d.quarter_end = r.quarter_end
JOIN dw.dim_property_type p ON p.dwelling_type = r.dwelling_type AND p.bedrooms = r.bedrooms
ON CONFLICT (suburb_key, date_key, property_type_key) DO UPDATE SET
    median_weekly_rent = EXCLUDED.median_weekly_rent,
    new_bonds_count = EXCLUDED.new_bonds_count,
    load_batch_id = EXCLUDED.load_batch_id
WHERE t.median_weekly_rent IS DISTINCT FROM EXCLUDED.median_weekly_rent
   OR t.new_bonds_count IS DISTINCT FROM EXCLUDED.new_bonds_count;
