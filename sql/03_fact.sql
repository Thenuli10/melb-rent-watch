-- Facts. Grain of fact_rental_bonds: suburb group x quarter x property type.
-- DFFH figures are moving annual medians (leases in the 12 months to quarter end).

CREATE TABLE IF NOT EXISTS dw.fact_rental_bonds (
    suburb_key         INT REFERENCES dw.dim_suburb,
    date_key           INT REFERENCES dw.dim_date,
    property_type_key  INT REFERENCES dw.dim_property_type,
    median_weekly_rent NUMERIC(8,2),         -- NULL when DFFH suppresses the cell
    new_bonds_count    INT,
    load_batch_id      INT,
    PRIMARY KEY (suburb_key, date_key, property_type_key)
);

-- Supports the window function partitions in analytics.v_rent_growth
CREATE INDEX IF NOT EXISTS ix_fact_rental_bonds_series
    ON dw.fact_rental_bonds (property_type_key, suburb_key, date_key);

-- Model outputs
CREATE TABLE IF NOT EXISTS dw.fact_rent_forecast (
    suburb_key         INT REFERENCES dw.dim_suburb,
    date_key           INT REFERENCES dw.dim_date,   -- forecast quarter
    property_type_key  INT REFERENCES dw.dim_property_type,
    predicted_rent     NUMERIC(8,2),
    lower_80           NUMERIC(8,2),
    upper_80           NUMERIC(8,2),
    model_version      TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (suburb_key, date_key, property_type_key, model_version)
);

-- Backtest accuracy per model, for the methodology page
CREATE TABLE IF NOT EXISTS dw.model_backtest_metrics (
    model_version TEXT NOT NULL,
    model_name    TEXT NOT NULL,             -- naive / seasonal_naive / drift / lightgbm
    segment_type  TEXT NOT NULL,             -- overall / region / property_type
    segment       TEXT NOT NULL,
    mape          NUMERIC(8,3),
    mae           NUMERIC(10,2),
    n_predictions INT,
    is_selected   BOOLEAN DEFAULT FALSE,     -- the model whose forecasts were published
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (model_version, model_name, segment_type, segment)
);

-- Regression coefficients, for the methodology page and the "train premium" question
CREATE TABLE IF NOT EXISTS dw.model_regression_coefficients (
    model_version TEXT NOT NULL,
    term          TEXT NOT NULL,
    coefficient   NUMERIC(12,6),
    std_error_hc3 NUMERIC(12,6),
    p_value       NUMERIC(12,6),
    conf_low      NUMERIC(12,6),
    conf_high     NUMERIC(12,6),
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (model_version, term)
);
