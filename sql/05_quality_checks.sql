-- Data quality checks, run after every load.
-- Each check starts with "-- check: <name>" and returns one integer column
-- `failures`; anything above 0 fails the run. {placeholders} are filled from
-- the `quality` section of config/sources.yaml.

-- check: staging_duplicate_keys
SELECT count(*) AS failures FROM (
    SELECT suburb_group, quarter_end, dwelling_type, bedrooms
    FROM staging.rental
    GROUP BY 1, 2, 3, 4
    HAVING count(*) > 1
) d;

-- check: rent_out_of_bounds
SELECT count(*) AS failures
FROM dw.fact_rental_bonds
WHERE median_weekly_rent IS NOT NULL
  AND (median_weekly_rent < {rent_min} OR median_weekly_rent > {rent_max});

-- check: negative_bond_counts
SELECT count(*) AS failures FROM dw.fact_rental_bonds WHERE new_bonds_count < 0;

-- check: fact_rows_without_dimensions
SELECT count(*) AS failures
FROM dw.fact_rental_bonds f
LEFT JOIN dw.dim_suburb s USING (suburb_key)
LEFT JOIN dw.dim_date d USING (date_key)
LEFT JOIN dw.dim_property_type p USING (property_type_key)
WHERE s.suburb_key IS NULL OR d.date_key IS NULL OR p.property_type_key IS NULL;

-- check: staging_rows_not_loaded
SELECT count(*) AS failures
FROM staging.rental r
LEFT JOIN dw.dim_suburb s ON s.suburb_group_name = r.suburb_group
LEFT JOIN dw.dim_date d ON d.quarter_end = r.quarter_end
LEFT JOIN dw.dim_property_type p ON p.dwelling_type = r.dwelling_type AND p.bedrooms = r.bedrooms
LEFT JOIN dw.fact_rental_bonds f
       ON f.suburb_key = s.suburb_key AND f.date_key = d.date_key AND f.property_type_key = p.property_type_key
WHERE f.suburb_key IS NULL;

-- check: row_count_change_vs_previous_quarter
-- Non-null rents in the latest quarter vs the one before, as a fraction
WITH counts AS (
    SELECT d.quarter_end, count(f.median_weekly_rent) AS n
    FROM dw.fact_rental_bonds f JOIN dw.dim_date d USING (date_key)
    GROUP BY d.quarter_end
    ORDER BY d.quarter_end DESC
    LIMIT 2
)
SELECT CASE
    WHEN count(*) < 2 THEN 0
    WHEN abs(max(n) FILTER (WHERE quarter_end = (SELECT max(quarter_end) FROM counts))::numeric
             / NULLIF(max(n) FILTER (WHERE quarter_end = (SELECT min(quarter_end) FROM counts)), 0) - 1)
         > {row_count_tolerance} THEN 1
    ELSE 0 END AS failures
FROM counts;

-- check: series_not_contiguous
-- Every suburb/type series must have one row per quarter between its first and last quarter
SELECT count(*) AS failures FROM (
    SELECT f.suburb_key, f.property_type_key
    FROM dw.fact_rental_bonds f JOIN dw.dim_date d USING (date_key)
    GROUP BY 1, 2
    HAVING count(*) <> ((EXTRACT(YEAR FROM max(d.quarter_end)) - EXTRACT(YEAR FROM min(d.quarter_end))) * 4
                       + (EXTRACT(QUARTER FROM max(d.quarter_end)) - EXTRACT(QUARTER FROM min(d.quarter_end))) + 1)
) s;

-- check: suburbs_without_sal_mapping
SELECT count(*) AS failures
FROM dw.dim_suburb s
WHERE NOT EXISTS (SELECT 1 FROM dw.bridge_suburb_sal b WHERE b.suburb_key = s.suburb_key);

-- check: suburbs_missing_income_or_distance
SELECT count(*) AS failures
FROM dw.dim_suburb
WHERE median_weekly_hh_income IS NULL OR distance_to_station_km IS NULL;

-- check: wage_index_missing_census_base
SELECT CASE WHEN EXISTS (SELECT 1 FROM dw.ref_wage_index WHERE quarter_end = DATE '2021-09-30')
            THEN 0 ELSE 1 END AS failures;
