import numpy as np
import pandas as pd
import pytest

from scripts.train_model import add_lag_features, add_time_features, wape


def test_wape_matches_hand_computation():
    y_true = np.array([10.0, 0.0, 5.0])
    y_pred = np.array([8.0, 2.0, 5.0])
    # |10-8| + |0-2| + |5-5| = 4, sum(y_true) = 15
    assert wape(y_true, y_pred) == pytest.approx(4 / 15)


def test_wape_handles_all_zero_actuals():
    # denominator is floored at 1e-9, so this must not raise or return inf/nan
    result = wape(np.array([0.0, 0.0]), np.array([1.0, 2.0]))
    assert result > 0
    assert np.isfinite(result)


def test_add_time_features_known_timestamp():
    # 2024-01-15 is a Monday and a holiday (MLK Day) in HOLIDAYS_2024_Q1
    df = pd.DataFrame({"pickup_hour": [pd.Timestamp("2024-01-15 14:00:00")]})
    out = add_time_features(df)
    row = out.iloc[0]
    assert row["hour"] == 14
    assert row["dow"] == 0  # Monday
    assert row["is_weekend"] == 0
    assert row["is_holiday"] == 1


def test_add_time_features_weekend_not_holiday():
    # 2024-03-23 is a Saturday, not in the holiday set
    df = pd.DataFrame({"pickup_hour": [pd.Timestamp("2024-03-23 09:00:00")]})
    out = add_time_features(df)
    row = out.iloc[0]
    assert row["dow"] == 5  # Saturday
    assert row["is_weekend"] == 1
    assert row["is_holiday"] == 0


def test_add_lag_features_no_leakage():
    # Two zones, 10 consecutive hours of a known, increasing trip_count sequence.
    hours = pd.date_range("2024-01-01", periods=10, freq="h")
    df = pd.concat(
        [
            pd.DataFrame({"zone_id": 1, "pickup_hour": hours, "trip_count": range(10)}),
            pd.DataFrame({"zone_id": 2, "pickup_hour": hours, "trip_count": range(100, 110)}),
        ],
        ignore_index=True,
    )
    out = add_lag_features(df).sort_values(["zone_id", "pickup_hour"]).reset_index(drop=True)

    zone1 = out[out["zone_id"] == 1].reset_index(drop=True)
    # lag_1h at hour index i should equal trip_count at i-1, and NaN at i=0
    assert zone1.loc[0, "lag_1h"] != zone1.loc[0, "lag_1h"]  # NaN
    assert zone1.loc[5, "lag_1h"] == 4
    # rolling mean at a given row must only reflect *past* values (via lag_1h),
    # never the current or future trip_count -- this is the leakage check.
    assert zone1.loc[3, "roll_mean_24h"] == zone1.loc[:2, "trip_count"].mean()

    # zones must not leak into each other's lag features
    zone2 = out[out["zone_id"] == 2].reset_index(drop=True)
    assert zone2.loc[0, "lag_1h"] != zone2.loc[0, "lag_1h"]  # NaN, not zone1's last value
