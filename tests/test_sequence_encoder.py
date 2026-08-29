from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from ml.artifacts import write_manifest
from ml.sequence_encoder.consumer import (
    load_sequence_distributions,
    safe_load_sequence_distributions,
)
from ml.sequence_encoder.contracts import (
    EMBEDDING_COLUMNS,
    SEQUENCE_ENCODER_POLICY_VERSION,
    SEQUENCE_FEATURE_COLUMNS,
    SequenceEncoderConfig,
)
from ml.sequence_encoder.dataset import (
    RobustSequenceScaler,
    WindowedExamples,
    build_windowed_examples,
    chronological_partitions,
)
from ml.sequence_encoder.publication import publish_sequence_run
from ml.sequence_encoder.surface import (
    loop_b_supervised_labels,
    materialize_hourly_surface_states,
)
from ml.sequence_encoder.training import (
    calibrated_prediction,
    train_sequence_ensemble,
)


def test_surface_materialization_consolidates_duplicate_publishers_causally(
    tmp_path: Path,
) -> None:
    symbol = "AAPL"
    timestamps = pd.to_datetime(
        ["2026-08-03T13:30:00Z", "2026-08-03T14:30:00Z"], utc=True
    )
    stock_path = (
        tmp_path
        / "stocks"
        / symbol
        / "bars"
        / "1h"
        / "databento"
        / "normalized"
        / f"{symbol}_source_1825d_1h_ohlcv-1h_1h.parquet"
    )
    stock_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [99.0, 100.0],
            "high": [101.0, 103.0],
            "low": [98.0, 99.0],
            "close": [100.0, 102.0],
            "volume": [1_000.0, 1_200.0],
        }
    ).to_parquet(stock_path, index=False)

    option_path = (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "ohlcv-1h"
        / "AAPL.OPT"
        / "dates"
        / "2026-08-03"
        / "segments"
        / "full-day"
        / "normalized.parquet"
    )
    option_path.parent.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    for timestamp in timestamps:
        rows.extend(
            [
                {
                    "ts_event": timestamp,
                    "symbol": "AAPL260821C00100000",
                    "open": 4.0,
                    "high": 5.5,
                    "low": 3.5,
                    "close": 5.0,
                    "volume": 10.0,
                },
                {
                    "ts_event": timestamp,
                    "symbol": "AAPL260821C00100000",
                    "open": 4.2,
                    "high": 5.7,
                    "low": 3.6,
                    "close": 5.2,
                    "volume": 30.0,
                },
                {
                    "ts_event": timestamp,
                    "symbol": "AAPL260821P00100000",
                    "open": 3.0,
                    "high": 4.0,
                    "low": 2.5,
                    "close": 3.5,
                    "volume": 20.0,
                },
            ]
        )
    pd.DataFrame(rows).to_parquet(option_path, index=False)

    states, sources = materialize_hourly_surface_states(
        tmp_path,
        symbols=(symbol,),
        information_cutoff="2026-08-03T15:30:00Z",
    )

    assert len(states) == 2
    assert set(sources) == {stock_path, option_path}
    assert states["source_raw_row_count"].tolist() == [3, 3]
    assert states["source_contract_count"].tolist() == [2, 2]
    assert np.allclose(states["option_call_contract_fraction"], 0.5)
    assert np.allclose(states["option_call_volume_fraction"], 2.0 / 3.0)
    assert states["information_available_at"].tolist() == list(
        timestamps + pd.Timedelta(hours=1)
    )
    assert math.isnan(float(states.iloc[0]["stock_log_return_1h"]))
    assert math.isclose(
        float(states.iloc[1]["stock_log_return_1h"]),
        math.log(102.0 / 100.0),
    )


def test_loop_b_labels_equal_weight_shared_decision_clusters() -> None:
    decision = pd.Timestamp("2026-08-03T15:35:00Z")
    target_start = pd.Timestamp("2026-08-03T16:30:00Z")
    target_end = pd.Timestamp("2026-08-03T17:30:00Z")
    samples = pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT"],
            "horizon": ["1h", "1h"],
            "decision_timestamp": [decision, decision],
            "information_available_at": [decision, decision],
            "bar_end_timestamp": [decision - pd.Timedelta(minutes=5)] * 2,
            "target_window_start": [target_start, target_start],
            "target_window_end": [target_end, target_end],
            "label_available_at": [target_end, target_end],
            "label_status": ["COMPLETE", "COMPLETE"],
            "target_cost_adjusted_positive": [1.0, 0.0],
            "forward_cost_adjusted_return": [0.02, -0.01],
        }
    )

    labels = loop_b_supervised_labels(samples, horizons=("1h",))

    assert labels["decision_cluster_size"].tolist() == [2, 2]
    assert labels["decision_weight"].tolist() == [0.5, 0.5]
    assert math.isclose(float(labels["decision_weight"].sum()), 1.0)


def test_partitions_and_windows_are_chronological_and_causal() -> None:
    config = SequenceEncoderConfig(
        window_length=2,
        num_layers=1,
        ensemble_size=1,
        pretrain_epochs=1,
        supervised_epochs=1,
        batch_size=4,
        minimum_train_clusters=2,
        calibration_clusters=1,
        assessment_clusters=1,
        embargo_hours=0,
        horizons=("1h",),
    )
    cluster_starts = pd.date_range("2026-07-01", periods=4, freq="2D", tz="UTC")
    labels = pd.DataFrame(
        {
            "symbol": ["AAPL"] * 4,
            "horizon": ["1h"] * 4,
            "decision_timestamp": cluster_starts - pd.Timedelta(hours=2),
            "bar_end_timestamp": cluster_starts - pd.Timedelta(hours=2),
            "target_window_start": cluster_starts,
            "target_window_end": cluster_starts + pd.Timedelta(hours=1),
            "target_cost_adjusted_positive": [0.0, 1.0, 0.0, 1.0],
            "forward_cost_adjusted_return": [-0.01, 0.02, -0.02, 0.03],
            "decision_weight": [1.0] * 4,
        }
    )
    partitions = chronological_partitions(labels, config=config)["1h"]
    assert partitions.train_clusters == 2
    assert partitions.calibration_clusters == 1
    assert partitions.assessment_clusters == 1
    assert (
        partitions.train["target_window_end"].max()
        < partitions.calibration["target_window_start"].min()
        < partitions.assessment["target_window_start"].min()
    )

    times = pd.date_range("2026-06-30T20:00:00Z", periods=3, freq="h")
    states = pd.DataFrame(
        {
            "symbol": ["AAPL"] * 3,
            "bar_timestamp": times,
            "information_available_at": times + pd.Timedelta(hours=1),
            **{
                name: [float(index), float(index + 1), float(index + 2)]
                for index, name in enumerate(SEQUENCE_FEATURE_COLUMNS)
            },
        }
    )
    scaler = RobustSequenceScaler.fit(states)
    one_label = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "horizon": ["1h"],
            "decision_timestamp": [times[1] + pd.Timedelta(hours=1)],
            "bar_end_timestamp": [times[1]],
            "target_window_start": [times[1] + pd.Timedelta(hours=2)],
            "target_window_end": [times[1] + pd.Timedelta(hours=3)],
            "target_cost_adjusted_positive": [1.0],
            "forward_cost_adjusted_return": [0.01],
            "decision_weight": [1.0],
        }
    )
    examples = build_windowed_examples(
        states,
        one_label,
        scaler=scaler,
        config=config,
        symbol_vocabulary={"AAPL": 1},
        horizon_vocabulary={"1h": 0},
    )
    assert len(examples) == 1
    assert examples.windows.shape == (1, 2, len(SEQUENCE_FEATURE_COLUMNS) * 2)
    assert examples.metadata.iloc[0]["sequence_window_end"] == times[1]
    assert (
        examples.metadata.iloc[0]["information_available_at"]
        <= one_label.iloc[0]["decision_timestamp"]
    )


def test_tiny_sequence_ensemble_emits_calibrated_uncertainty() -> None:
    config = SequenceEncoderConfig(
        window_length=2,
        num_layers=1,
        dropout=0.0,
        ensemble_size=1,
        pretrain_epochs=1,
        supervised_epochs=1,
        batch_size=4,
        minimum_train_clusters=1,
        calibration_clusters=1,
        assessment_clusters=1,
        embargo_hours=0,
        horizons=("1h",),
    )
    rng = np.random.default_rng(7)
    width = len(SEQUENCE_FEATURE_COLUMNS) * 2

    def examples(rows: int, offset: int) -> WindowedExamples:
        starts = pd.date_range(
            "2026-01-01", periods=rows, freq="h", tz="UTC"
        ) + pd.Timedelta(hours=offset)
        return WindowedExamples(
            windows=rng.normal(size=(rows, 2, width)).astype(np.float32),
            symbol_ids=np.ones(rows, dtype=np.int64),
            horizon_ids=np.zeros(rows, dtype=np.int64),
            direction_targets=(np.arange(rows) % 2).astype(np.float32),
            return_targets=rng.normal(scale=0.01, size=rows).astype(np.float32),
            sample_weights=np.ones(rows, dtype=np.float32),
            metadata=pd.DataFrame(
                {
                    "horizon": ["1h"] * rows,
                    "target_window_start": starts,
                    "target_window_end": starts + pd.Timedelta(hours=1),
                }
            ),
        )

    train = examples(8, 0)
    calibration = examples(6, 100)
    pretrain_x = rng.normal(size=(5, 2, width)).astype(np.float32)
    pretrain_y = rng.normal(size=(5, len(SEQUENCE_FEATURE_COLUMNS))).astype(
        np.float32
    )
    ensemble = train_sequence_ensemble(
        pretrain_windows=pretrain_x,
        pretrain_targets=pretrain_y,
        train=train,
        calibration=calibration,
        config=config,
        symbol_count=1,
        horizon_vocabulary={"1h": 0},
    )
    prediction = calibrated_prediction(
        ensemble,
        calibration,
        horizon_vocabulary={"1h": 0},
        batch_size=4,
    )

    assert np.isfinite(prediction["calibrated_probability_up"]).all()
    assert ((prediction["calibrated_probability_up"] >= 0.0) & (
        prediction["calibrated_probability_up"] <= 1.0
    )).all()
    assert (prediction["total_uncertainty"] > 0.0).all()
    assert prediction["embedding"].shape == (6, len(EMBEDDING_COLUMNS))


def test_shadow_publication_is_causal_and_never_grants_action_authority(
    tmp_path: Path,
) -> None:
    run = tmp_path / "ml" / "sequence-encoder-runs" / "20260803T160000Z"
    run.mkdir(parents=True)
    decision = pd.Timestamp("2026-08-03T15:35:00Z")
    distributions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "horizon": ["1h"],
            "decision_timestamp": [decision],
            "information_available_at": [decision],
            "prediction_created_at": [pd.Timestamp("2026-08-03T15:40:00Z")],
            "prediction_status": ["SHADOW_READY"],
            "automated_action_allowed": [False],
        }
    )
    distributions.to_parquet(run / "distributions.parquet", index=False)
    source = {
        "run_path": "ml/runs/20260803T153500Z",
        "run_timestamp": "2026-08-03T15:35:00Z",
    }
    write_manifest(
        run,
        run_timestamp="2026-08-03T16:00:00Z",
        input_files=(),
        output_files=("distributions.parquet",),
        configuration={
            "policy_version": SEQUENCE_ENCODER_POLICY_VERSION,
            "authority": "SHADOW_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "source_loop_b": source,
        },
        datastore_root=tmp_path,
    )
    publish_sequence_run(
        tmp_path,
        run_directory=run,
        published_at="2026-08-03T16:00:00Z",
        source_loop_b=source,
    )
    routes = distributions.loc[:, ["symbol", "horizon", "decision_timestamp"]]

    future = load_sequence_distributions(
        tmp_path,
        routes=routes,
        consumer="LOOP_B",
        as_of="2026-08-03T15:59:59Z",
    )
    ready = load_sequence_distributions(
        tmp_path,
        routes=routes,
        consumer="OPTIONS_STRATEGY",
        as_of="2026-08-03T16:01:00Z",
    )

    assert future.status == "UNAVAILABLE"
    assert ready.status == "READY_SHADOW"
    assert ready.matched_routes == 1
    assert ready.details["authority"] == "SHADOW_ONLY"
    assert not bool(ready.distributions["automated_action_allowed"].any())

    distributions.assign(automated_action_allowed=True).to_parquet(
        run / "distributions.parquet", index=False
    )
    invalid = safe_load_sequence_distributions(
        tmp_path,
        routes=routes,
        consumer="LOOP_B",
        as_of="2026-08-03T16:02:00Z",
    )
    assert invalid.status == "INVALID_SHADOW"
    assert invalid.details["authority"] == "NONE"

