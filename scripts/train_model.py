"""Feature-engineer the hourly demand panel, train a gradient-boosting model,
benchmark it against a historical-mean baseline, and write everything the API
needs to serve predictions for the held-out test window."""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
MODEL_DIR = ROOT / "backend" / "model_artifacts"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Held-out test/serving window: last 2 weeks of the 3-month dataset.
TEST_START = "2024-03-18"
HOLIDAYS_2024_Q1 = {"2024-01-01", "2024-01-15", "2024-02-19"}  # New Year's, MLK Day, Presidents' Day


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["hour"] = df["pickup_hour"].dt.hour
    df["dow"] = df["pickup_hour"].dt.dayofweek  # 0=Mon
    df["day"] = df["pickup_hour"].dt.day
    df["month"] = df["pickup_hour"].dt.month
    df["is_weekend"] = (df["dow"] >= 5).astype(int)
    df["is_holiday"] = df["pickup_hour"].dt.strftime("%Y-%m-%d").isin(HOLIDAYS_2024_Q1).astype(int)
    return df


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["zone_id", "pickup_hour"]).copy()
    grp = df.groupby("zone_id")["trip_count"]
    df["lag_1h"] = grp.shift(1)
    df["lag_24h"] = grp.shift(24)
    df["lag_168h"] = grp.shift(168)  # same hour, previous week
    # rolling means computed on already-shifted lag_1h series to avoid leaking the target
    df["roll_mean_24h"] = (
        df.groupby("zone_id")["lag_1h"].transform(lambda s: s.rolling(24, min_periods=1).mean())
    )
    df["roll_mean_168h"] = (
        df.groupby("zone_id")["lag_1h"].transform(lambda s: s.rolling(168, min_periods=1).mean())
    )
    return df


def add_weather_features(df: pd.DataFrame) -> pd.DataFrame:
    weather = pd.read_csv(RAW_DIR / "weather.csv", parse_dates=["pickup_hour"])
    return df.merge(weather, on="pickup_hour", how="left")


FEATURE_COLS = [
    "zone_id", "hour", "dow", "day", "month", "is_weekend", "is_holiday",
    "lag_1h", "lag_24h", "lag_168h", "roll_mean_24h", "roll_mean_168h",
    "temp_c", "precip_mm", "snow_cm", "wind_kmh",
]
CATEGORICAL_COLS = ["hour", "dow", "month"]  # zone_id kept numeric: cardinality (263) exceeds
# HistGradientBoostingRegressor's native categorical limit (255); trees still split on it fine.


def wape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.abs(y_true - y_pred).sum() / max(y_true.sum(), 1e-9))


def main() -> None:
    panel = pd.read_parquet(PROCESSED_DIR / "hourly_demand.parquet")
    panel = add_time_features(panel)
    panel = add_lag_features(panel)
    panel = add_weather_features(panel)

    # Rows without full 168h history can't be used for training (first 7 days per zone).
    train_ready = panel.dropna(subset=["lag_168h"]).copy()

    train_df = train_ready[train_ready["pickup_hour"] < TEST_START].copy()
    test_df = train_ready[train_ready["pickup_hour"] >= TEST_START].copy()
    print(f"train rows: {len(train_df)}, test rows: {len(test_df)}")

    X_train, y_train = train_df[FEATURE_COLS], train_df["trip_count"].values
    X_test, y_test = test_df[FEATURE_COLS], test_df["trip_count"].values

    cat_mask = [c in CATEGORICAL_COLS for c in FEATURE_COLS]
    model = HistGradientBoostingRegressor(
        loss="poisson",
        max_iter=400,
        learning_rate=0.06,
        max_depth=8,
        l2_regularization=0.5,
        categorical_features=cat_mask,
        random_state=42,
    )
    model.fit(X_train, y_train)
    pred = np.clip(model.predict(X_test), 0, None)

    # Baseline: historical mean trip_count by (zone_id, dow, hour), learned on train only.
    baseline_lookup = (
        train_df.groupby(["zone_id", "dow", "hour"])["trip_count"].mean().rename("baseline_pred")
    )
    test_df = test_df.join(baseline_lookup, on=["zone_id", "dow", "hour"])
    test_df["baseline_pred"] = test_df["baseline_pred"].fillna(train_df["trip_count"].mean())
    baseline_pred = test_df["baseline_pred"].values

    model_mae = float(np.abs(y_test - pred).mean())
    baseline_mae = float(np.abs(y_test - baseline_pred).mean())
    model_wape = wape(y_test, pred)
    baseline_wape = wape(y_test, baseline_pred)

    metrics = {
        "test_start": TEST_START,
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "model_mae": model_mae,
        "baseline_mae": baseline_mae,
        "model_wape": model_wape,
        "baseline_wape": baseline_wape,
        "mae_improvement_pct": (baseline_mae - model_mae) / baseline_mae * 100,
    }
    print(json.dumps(metrics, indent=2))

    # Per-zone error breakdown (for the metrics UI panel)
    test_df["model_pred"] = pred
    test_df["abs_err_model"] = np.abs(test_df["trip_count"] - test_df["model_pred"])
    test_df["abs_err_baseline"] = np.abs(test_df["trip_count"] - test_df["baseline_pred"])
    per_zone = (
        test_df.groupby("zone_id")
        .agg(
            actual_mean=("trip_count", "mean"),
            model_mae=("abs_err_model", "mean"),
            baseline_mae=("abs_err_baseline", "mean"),
        )
        .reset_index()
    )

    # Persist everything the API needs.
    joblib.dump(model, MODEL_DIR / "model.joblib")
    (MODEL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    per_zone.to_parquet(MODEL_DIR / "per_zone_metrics.parquet", index=False)

    test_df[["zone_id", "pickup_hour", "trip_count", "model_pred", "baseline_pred"]].to_parquet(
        PROCESSED_DIR / "predictions.parquet", index=False
    )
    print("wrote model.joblib, metrics.json, per_zone_metrics.parquet, predictions.parquet")


if __name__ == "__main__":
    main()
