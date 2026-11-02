import numpy as np
import pandas as pd

from src.models import forecast


def synthetic_panel(n_quarters: int = 40) -> pd.DataFrame:
    quarters = pd.date_range("2010-03-31", periods=n_quarters, freq="QE")
    rows = []
    for sk in (1, 2):
        for pk, (dwelling, beds) in enumerate([("Flat", 1), ("House", 3)], start=1):
            rent = 300.0 * sk * pk
            for q in quarters:
                rent *= 1.01
                rows.append({"suburb_key": sk, "property_type_key": pk, "date_key": int(q.strftime("%Y%m%d")),
                             "quarter_end": q, "rent": round(rent, 2), "bonds": 100, "region": "Inner Melbourne",
                             "distance_km": 0.5 * sk, "income": 2000.0, "dwelling_type": dwelling,
                             "bedrooms": beds})
    return pd.DataFrame(rows)


def test_target_is_next_quarter_log_growth():
    df = forecast.build_features(synthetic_panel())
    s = df[(df["suburb_key"] == 1) & (df["property_type_key"] == 1)].reset_index(drop=True)
    assert np.isclose(s.loc[5, "rent_next"], s.loc[6, "rent"])
    assert np.isclose(s.loc[5, "y"], np.log(s.loc[6, "rent"] / s.loc[5, "rent"]))
    assert s.loc[5, "target_quarter"] == s.loc[6, "quarter_end"]
    assert pd.isna(s["y"].iloc[-1])  # no target for the latest quarter


def test_features_use_only_past_data():
    df = forecast.build_features(synthetic_panel())
    s = df[(df["suburb_key"] == 1) & (df["property_type_key"] == 1)].reset_index(drop=True)
    assert np.isclose(s.loc[5, "lag_growth_1"], np.log(s.loc[5, "rent"] / s.loc[4, "rent"]))
    assert np.isclose(s.loc[5, "growth_yoy"], np.log(s.loc[5, "rent"] / s.loc[1, "rent"]))
    assert s.loc[5, "rent_seasonal"] == s.loc[2, "rent"]  # target t+1 = Q of index 6; same Q last year = index 2


def test_drift_baseline_tracks_constant_growth():
    df = forecast.build_features(synthetic_panel())
    preds = forecast.baseline_predictions(df)
    ok = preds["drift"].notna() & df["rent_next"].notna()
    ape = ((preds["drift"] - df["rent_next"]).abs() / df["rent_next"])[ok]
    assert ape.max() < 0.001
