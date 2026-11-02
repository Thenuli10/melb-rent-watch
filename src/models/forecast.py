"""Next-quarter median rent forecast for every suburb group x property type.

- Baselines: naive (last quarter), seasonal naive (same quarter last year),
  drift (last quarter x (1 + median of the last four QoQ growth rates)).
- Main model: one LightGBM model over the panel of all series.
- Target: log of next-quarter rent. Trees cannot extrapolate beyond the levels
  seen in training, and rents are at record highs, so the model learns the log
  growth log(rent[t+1]) - log(rent[t]) and adds it back to log(rent[t]).
  That is the same target, re-expressed relative to the current level.
- Validation: expanding-window backtest over the last BACKTEST_QUARTERS quarters;
  each fold trains only on targets that were known before the forecast origin.
- Uncertainty: LightGBM quantile models at 0.1 and 0.9 (80% interval). Baselines
  use empirical 10th/90th percentiles of their backtest log errors.
- The published forecast comes from whichever model has the lowest backtest
  MAPE, so a model that can't beat the baselines is never published over them.
"""
from __future__ import annotations

import warnings
from datetime import date

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.common import REPORTS_DIR, connect, get_engine, log

BACKTEST_QUARTERS = 8
MIN_TRAIN_YEAR = 2004  # skip the first years so every row has a full set of lags
LGB_PARAMS = dict(n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=40,
                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)
CATEGORICALS = ["dwelling_type", "region"]
FEATURES = ["log_rent", "lag_growth_1", "lag_growth_2", "lag_growth_3", "growth_yoy", "log_rent_vs_roll4",
            "log_bonds", "bonds_change", "bedrooms", "dwelling_type", "distance_km", "log_income", "region",
            "target_quarter_of_year"]

QUERY = """
SELECT f.suburb_key, f.property_type_key, f.date_key, d.quarter_end,
       f.median_weekly_rent AS rent, f.new_bonds_count AS bonds,
       s.region, s.distance_to_station_km AS distance_km, s.median_weekly_hh_income AS income,
       p.dwelling_type, p.bedrooms
FROM dw.fact_rental_bonds f
JOIN dw.dim_date d USING (date_key)
JOIN dw.dim_suburb s USING (suburb_key)
JOIN dw.dim_property_type p USING (property_type_key)
ORDER BY f.suburb_key, f.property_type_key, d.quarter_end
"""


def load_panel() -> pd.DataFrame:
    df = pd.read_sql(QUERY, get_engine(), parse_dates=["quarter_end"])
    for c in ("rent", "distance_km", "income"):
        df[c] = df[c].astype(float)
    return df


def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    """One row per series x origin quarter t, with features known at t and the target at t+1."""
    df = panel.sort_values(["suburb_key", "property_type_key", "quarter_end"]).copy()
    g = df.groupby(["suburb_key", "property_type_key"], sort=False)

    df["log_rent"] = np.log(df["rent"])
    lr = g["log_rent"]
    df["lag_growth_1"] = df["log_rent"] - lr.shift(1)
    df["lag_growth_2"] = lr.shift(1) - lr.shift(2)
    df["lag_growth_3"] = lr.shift(2) - lr.shift(3)
    df["growth_yoy"] = df["log_rent"] - lr.shift(4)
    df["log_rent_vs_roll4"] = df["log_rent"] - np.log(g["rent"].transform(lambda s: s.rolling(4).mean()))
    df["log_bonds"] = np.log1p(df["bonds"].astype(float))
    df["bonds_change"] = df["log_bonds"] - g["log_bonds"].shift(1)
    df["log_income"] = np.log(df["income"])
    df["target_quarter_of_year"] = (df["quarter_end"] + pd.offsets.QuarterEnd(1)).dt.quarter

    # Baseline inputs
    df["rent_seasonal"] = g["rent"].shift(3)  # same quarter last year, relative to the target t+1
    growth = df["rent"] / g["rent"].shift(1) - 1
    df["median_growth_4q"] = growth.groupby([df["suburb_key"], df["property_type_key"]]).transform(
        lambda s: s.rolling(4).median())

    # Target
    df["target_quarter"] = df["quarter_end"] + pd.offsets.QuarterEnd(1)
    df["rent_next"] = g["rent"].shift(-1)
    df["y"] = np.log(df["rent_next"]) - df["log_rent"]

    for c in CATEGORICALS:
        df[c] = df[c].astype("category")
    return df


def baseline_predictions(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "naive": df["rent"],
        "seasonal_naive": df["rent_seasonal"],
        "drift": df["rent"] * (1 + df["median_growth_4q"]),
    }


def _fit_lgb(train: pd.DataFrame, **extra) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(**LGB_PARAMS, **extra)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(train[FEATURES], train["y"], categorical_feature=CATEGORICALS)
    return model


def _usable(df: pd.DataFrame) -> pd.Series:
    return (df["quarter_end"].dt.year >= MIN_TRAIN_YEAR) & df["rent"].notna() & df["lag_growth_3"].notna() \
        & df["growth_yoy"].notna()


def backtest(df: pd.DataFrame) -> pd.DataFrame:
    """Expanding-window backtest. Returns one row per series x test quarter with every model's prediction."""
    usable = _usable(df) & df["y"].notna()
    targets = sorted(df.loc[usable, "target_quarter"].unique())[-BACKTEST_QUARTERS:]
    folds = []
    for tq in targets:
        train = df[usable & (df["target_quarter"] < tq)]
        test = df[usable & (df["target_quarter"] == tq)].copy()
        model = _fit_lgb(train)
        test["lightgbm"] = test["rent"] * np.exp(model.predict(test[FEATURES]))
        for name, pred in baseline_predictions(test).items():
            test[name] = pred
        folds.append(test)
        log.info("Backtest fold %s: train %s rows, test %s rows", pd.Timestamp(tq).date(), len(train), len(test))
    return pd.concat(folds, ignore_index=True)


MODELS = ["naive", "seasonal_naive", "drift", "lightgbm"]


def metrics(bt: pd.DataFrame) -> pd.DataFrame:
    rows = []
    segments = [("overall", pd.Series("all", index=bt.index)), ("region", bt["region"].astype(str)),
                ("property_type", bt["bedrooms"].astype(str) + " bed " + bt["dwelling_type"].astype(str).str.lower())]
    for model in MODELS:
        ok = bt[model].notna() & bt["rent_next"].notna()
        err = (bt[model] - bt["rent_next"]).abs()
        ape = err / bt["rent_next"]
        for seg_type, seg in segments:
            for seg_value, idx in seg[ok].groupby(seg[ok]).groups.items():
                rows.append({"model_name": model, "segment_type": seg_type, "segment": seg_value,
                             "mape": 100 * ape[idx].mean(), "mae": err[idx].mean(), "n_predictions": len(idx)})
    return pd.DataFrame(rows)


def forecast_next(df: pd.DataFrame, selected: str, bt: pd.DataFrame) -> pd.DataFrame:
    """Forecast the quarter after the latest one for every series with a current rent."""
    latest = df["quarter_end"].max()
    origin = df[(df["quarter_end"] == latest) & df["rent"].notna()].copy()

    if selected == "lightgbm":
        train = df[_usable(df) & df["y"].notna()]
        point = _fit_lgb(train)
        lo = _fit_lgb(train, objective="quantile", alpha=0.1)
        hi = _fit_lgb(train, objective="quantile", alpha=0.9)
        X = origin[FEATURES]
        origin["predicted_rent"] = origin["rent"] * np.exp(point.predict(X))
        origin["lower_80"] = origin["rent"] * np.exp(lo.predict(X))
        origin["upper_80"] = origin["rent"] * np.exp(hi.predict(X))
        # Quantile models are fitted separately and can cross the point forecast
        origin["lower_80"] = origin[["lower_80", "predicted_rent"]].min(axis=1)
        origin["upper_80"] = origin[["upper_80", "predicted_rent"]].max(axis=1)
    else:
        origin["predicted_rent"] = baseline_predictions(origin)[selected]
        # Fall back to naive where a baseline has no input (e.g. too few quarters)
        origin["predicted_rent"] = origin["predicted_rent"].fillna(origin["rent"])
        log_err = np.log(bt["rent_next"] / bt[selected]).dropna()
        q10, q90 = log_err.quantile([0.1, 0.9])
        origin["lower_80"] = origin["predicted_rent"] * np.exp(q10)
        origin["upper_80"] = origin["predicted_rent"] * np.exp(q90)

    origin["forecast_quarter"] = latest + pd.offsets.QuarterEnd(1)
    origin["date_key"] = origin["forecast_quarter"].dt.strftime("%Y%m%d").astype(int)
    return origin[["suburb_key", "date_key", "property_type_key", "predicted_rent", "lower_80", "upper_80"]]


def save(fc: pd.DataFrame, mt: pd.DataFrame, version: str, selected: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM dw.fact_rent_forecast WHERE model_version = %s", (version,))
        cur.executemany(
            """INSERT INTO dw.fact_rent_forecast
               (suburb_key, date_key, property_type_key, predicted_rent, lower_80, upper_80, model_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(int(r.suburb_key), int(r.date_key), int(r.property_type_key), round(float(r.predicted_rent), 2),
              round(float(r.lower_80), 2), round(float(r.upper_80), 2), version) for r in fc.itertuples()])
        cur.execute("DELETE FROM dw.model_backtest_metrics WHERE model_version = %s", (version,))
        cur.executemany(
            """INSERT INTO dw.model_backtest_metrics
               (model_version, model_name, segment_type, segment, mape, mae, n_predictions, is_selected)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [(version, r.model_name, r.segment_type, str(r.segment), round(float(r.mape), 3),
              round(float(r.mae), 2), int(r.n_predictions), r.model_name == selected) for r in mt.itertuples()])


def write_report(mt: pd.DataFrame, selected: str, version: str, fc: pd.DataFrame) -> None:
    overall = mt[mt["segment_type"] == "overall"].sort_values("mape")
    by_type = mt[mt["segment_type"] == "property_type"].pivot(index="segment", columns="model_name", values="mape")
    by_region = mt[mt["segment_type"] == "region"].pivot(index="segment", columns="model_name", values="mape")
    naive = overall.set_index("model_name").loc["naive", "mape"]
    best = overall.set_index("model_name").loc[selected, "mape"]
    text = "\n".join([
        f"# Next-quarter rent forecast ({version})",
        "",
        f"Backtest: expanding window, last {BACKTEST_QUARTERS} quarters, one-quarter-ahead.",
        f"Published model: **{selected}** (MAPE {best:.2f}% vs naive {naive:.2f}%).",
        "",
        "## Overall accuracy",
        overall[["model_name", "mape", "mae", "n_predictions"]].round(3).to_string(index=False),
        "",
        "## MAPE by property type",
        by_type[MODELS].round(2).to_string(),
        "",
        "## MAPE by region",
        by_region[MODELS].round(2).to_string(),
        "",
        f"Forecast rows written: {len(fc)} for quarter ending {fc['date_key'].iloc[0]}",
        "",
        "Note: DFFH rents are moving annual medians (leases signed in the past 12 months), so the series is",
        "smooth and a naive forecast is hard to beat. Intervals are 80% (10th to 90th percentile).",
    ])
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "forecast_summary.md").write_text(text, encoding="utf-8")


def run() -> str:
    panel = load_panel()
    df = build_features(panel)
    bt = backtest(df)
    mt = metrics(bt)
    overall = mt[mt["segment_type"] == "overall"].set_index("model_name")["mape"]
    selected = overall.idxmin()
    latest = df["quarter_end"].max()
    version = f"{selected}_v1_{latest:%Y}Q{latest.quarter}_{date.today():%Y%m%d}"
    fc = forecast_next(df, selected, bt)
    save(fc, mt, version, selected)
    write_report(mt, selected, version, fc)
    detail = (f"{version}: selected {selected}; MAPE " +
              ", ".join(f"{m}={overall[m]:.2f}%" for m in MODELS) + f"; {len(fc)} forecasts")
    log.info("Forecast: %s", detail)
    return detail


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    print(run())
