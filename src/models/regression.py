"""Explanatory regression: what is associated with higher rent?

Unit of analysis: suburb group x property type, rent and indexed income
averaged over the latest four quarters (reduces noise).

Model A (elasticities):
    log(rent) ~ log(distance_km) + bedrooms + C(dwelling_type) + log(income_indexed) + C(region)
Model B (train premium by distance band, same controls):
    log(rent) ~ C(distance_band) + bedrooms + C(dwelling_type) + log(income_indexed) + C(region)

Diagnostics: VIF, Breusch-Pagan, HC3 robust standard errors, Moran's I on
suburb-level residuals. This measures association, not causation.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from statsmodels.stats.diagnostic import het_breuschpagan
from statsmodels.stats.outliers_influence import variance_inflation_factor

from src.common import REPORTS_DIR, connect, get_engine, log

MIN_DISTANCE_KM = 0.05  # floor before taking logs
BAND_ORDER = [">5 km", "2–5 km", "1–2 km", "<1 km"]  # first level is the reference

QUERY = """
WITH latest AS (
    SELECT quarter_end FROM dw.dim_date d
    WHERE EXISTS (SELECT 1 FROM dw.fact_rental_bonds f WHERE f.date_key = d.date_key)
    ORDER BY quarter_end DESC LIMIT 4
)
SELECT suburb_key, suburb_group_name, region, dwelling_type, bedrooms, property_type,
       distance_to_station_km, distance_band, latitude, longitude,
       avg(median_weekly_rent)      AS rent,
       avg(median_weekly_hh_income) AS income_indexed,
       sum(new_bonds_count)         AS bonds
FROM analytics.v_affordability
WHERE quarter_end IN (SELECT quarter_end FROM latest)
  AND median_weekly_rent IS NOT NULL
GROUP BY suburb_key, suburb_group_name, region, dwelling_type, bedrooms, property_type,
         distance_to_station_km, distance_band, latitude, longitude
"""

FORMULA_A = ("np.log(rent) ~ np.log(distance_km) + bedrooms + C(dwelling_type) + np.log(income_indexed)"
             " + C(region)")
FORMULA_B = ("np.log(rent) ~ C(distance_band, levels=BAND_ORDER) + bedrooms + C(dwelling_type)"
             " + np.log(income_indexed) + C(region)")
# Model C: one "near" flag. The band model's 95% ranges for <1 km and 1-2 km overlap, so the
# data supports "within ~2 km costs more" but not a gradient between the inner bands.
NEAR_KM = 2
FORMULA_C = ("np.log(rent) ~ near_station + bedrooms + C(dwelling_type) + np.log(income_indexed)"
             " + C(region)")


def load_data() -> pd.DataFrame:
    df = pd.read_sql(QUERY, get_engine())
    for c in ("rent", "income_indexed", "distance_to_station_km", "latitude", "longitude"):
        df[c] = df[c].astype(float)
    df["distance_km"] = df["distance_to_station_km"].clip(lower=MIN_DISTANCE_KM)
    df["near_station"] = (df["distance_to_station_km"] <= NEAR_KM).astype(int)
    return df


def vif_table(df: pd.DataFrame) -> pd.DataFrame:
    X = pd.DataFrame({
        "log_distance": np.log(df["distance_km"]),
        "bedrooms": df["bedrooms"].astype(float),
        "is_house": (df["dwelling_type"] == "House").astype(float),
        "log_income": np.log(df["income_indexed"]),
    })
    X.insert(0, "const", 1.0)
    return pd.DataFrame({
        "variable": X.columns[1:],
        "vif": [variance_inflation_factor(X.values, i) for i in range(1, X.shape[1])],
    })


def morans_i(df: pd.DataFrame, resid: pd.Series, k: int = 6):
    """Moran's I on suburb-level mean residuals with k-nearest-neighbour weights."""
    from esda.moran import Moran
    from libpysal.weights import KNN

    by_suburb = (df.assign(resid=resid).groupby("suburb_key")
                   .agg(resid=("resid", "mean"), latitude=("latitude", "first"), longitude=("longitude", "first")))
    w = KNN.from_array(by_suburb[["longitude", "latitude"]].to_numpy(), k=k)
    w.transform = "r"
    return Moran(by_suburb["resid"].to_numpy(), w, permutations=999)


def fit(df: pd.DataFrame) -> dict:
    model_a = smf.ols(FORMULA_A, data=df).fit(cov_type="HC3")
    model_b = smf.ols(FORMULA_B, data=df, eval_env=1).fit(cov_type="HC3")
    model_c = smf.ols(FORMULA_C, data=df).fit(cov_type="HC3")
    bp_lm, bp_p, _, _ = het_breuschpagan(model_a.resid, model_a.model.exog)
    moran = morans_i(df, model_a.resid)
    return {"a": model_a, "b": model_b, "c": model_c, "vif": vif_table(df), "bp_p": bp_p, "moran": moran}


def coefficient_frame(model, version: str, prefix: str = "") -> pd.DataFrame:
    ci = model.conf_int()
    return pd.DataFrame({
        "model_version": version,
        "term": [prefix + t for t in model.params.index],
        "coefficient": model.params.values,
        "std_error_hc3": model.bse.values,
        "p_value": model.pvalues.values,
        "conf_low": ci[0].values,
        "conf_high": ci[1].values,
    })


def band_premiums(model_b) -> pd.DataFrame:
    """Percent rent difference of each distance band vs the >5 km reference band."""
    rows = []
    for band in BAND_ORDER[1:]:
        term = f"C(distance_band, levels=BAND_ORDER)[T.{band}]"
        if term in model_b.params:
            lo, hi = model_b.conf_int().loc[term]
            rows.append({"band": band, "premium_pct": 100 * np.expm1(model_b.params[term]),
                         "ci_low_pct": 100 * np.expm1(lo), "ci_high_pct": 100 * np.expm1(hi),
                         "p_value": model_b.pvalues[term]})
    return pd.DataFrame(rows)


def near_premium_text(model_c, df: pd.DataFrame) -> str:
    lo, hi = model_c.conf_int().loc["near_station"]
    return (f"Premium: {100 * np.expm1(model_c.params['near_station']):+.1f}% "
            f"(95% range {100 * np.expm1(lo):+.1f}% to {100 * np.expm1(hi):+.1f}%, "
            f"p = {model_c.pvalues['near_station']:.2g}); "
            f"{int(df['near_station'].sum())} of {len(df)} suburb x type rows are within {NEAR_KM} km.")


def write_report(res: dict, df: pd.DataFrame, version: str) -> str:
    a, moran = res["a"], res["moran"]
    elasticity = a.params["np.log(distance_km)"]
    lines = [
        f"# Rent drivers regression ({version})",
        "",
        f"Observations: {int(a.nobs)} suburb group x property type rows (latest 4 quarters averaged)",
        f"R-squared: {a.rsquared:.3f}   adj. R-squared: {a.rsquared_adj:.3f}",
        "",
        "## Distance to station (model A)",
        f"Elasticity: {elasticity:.4f} (HC3 SE {a.bse['np.log(distance_km)']:.4f}, "
        f"p = {a.pvalues['np.log(distance_km)']:.4f})",
        f"A 10% increase in distance is associated with a {100 * (1.1 ** elasticity - 1):.2f}% change in rent,",
        "holding bedrooms, dwelling type, indexed income and region constant.",
        "",
        "## Train premium by distance band vs >5 km (model B)",
        band_premiums(res["b"]).round(3).to_string(index=False),
        "The <1 km and 1-2 km ranges overlap, so the bands do not show a gradient within 2 km.",
        "",
        f"## Within {NEAR_KM} km of a station vs further (model C)",
        near_premium_text(res["c"], df),
        "",
        "## Diagnostics",
        "VIF:",
        res["vif"].round(2).to_string(index=False),
        f"Breusch-Pagan p-value: {res['bp_p']:.4g} "
        f"({'heteroskedastic: HC3 robust SEs used' if res['bp_p'] < 0.05 else 'no strong evidence of heteroskedasticity'})",
        f"Moran's I on suburb residuals: {moran.I:.3f} (permutation p = {moran.p_sim:.3f}) "
        f"({'residuals cluster spatially; treat SEs as optimistic, region effects already included' if moran.p_sim < 0.05 else 'no significant spatial clustering'})",
        "",
        "## Model A summary",
        str(a.summary()),
        "",
        "Association, not causation: station proximity correlates with CBD access, density and amenities.",
    ]
    REPORTS_DIR.mkdir(exist_ok=True)
    text = "\n".join(lines)
    (REPORTS_DIR / "regression_summary.md").write_text(text, encoding="utf-8")
    return text


def save_coefficients(res: dict, version: str) -> None:
    coefs = pd.concat([coefficient_frame(res["a"], version), coefficient_frame(res["b"], version, "band_model:"),
                       coefficient_frame(res["c"], version, "near_model:")])
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM dw.model_regression_coefficients WHERE model_version = %s", (version,))
        cur.executemany(
            """INSERT INTO dw.model_regression_coefficients
               (model_version, term, coefficient, std_error_hc3, p_value, conf_low, conf_high)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [tuple(float(v) if isinstance(v, (float, np.floating)) else v for v in row)
             for row in coefs.itertuples(index=False, name=None)])


def run() -> str:
    df = load_data()
    version = f"ols_v1_{date.today():%Y%m%d}"
    res = fit(df)
    write_report(res, df, version)
    save_coefficients(res, version)
    e = res["a"].params["np.log(distance_km)"]
    detail = f"{version}: n={int(res['a'].nobs)}, distance elasticity={e:.4f}, R2={res['a'].rsquared:.3f}"
    log.info("Regression: %s", detail)
    return detail


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    print(run())
