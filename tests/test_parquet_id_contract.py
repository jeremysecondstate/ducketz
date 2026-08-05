from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datafetching import bar_schema
from datafetching.ids import validate_raw_provider_id_columns
from datafetching.parquet_store import ParquetStore
from ml import parquet_contracts

PRODUCTION_ROOTS = (
    "app",
    "datafetching",
    "fundamentals",
    "ml",
    "options",
    "signals",
    "technicals",
)
_HEX_HASH = re.compile(
    r"^(?:[a-z][a-z0-9_-]*[_:-])?[0-9a-f]{32,}$",
    re.IGNORECASE,
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def explicit_parquet_schemas() -> dict[str, pa.Schema]:
    schemas: dict[str, pa.Schema] = {}
    for module in (bar_schema, parquet_contracts):
        for name, value in vars(module).items():
            if isinstance(value, pa.Schema):
                schemas[f"{module.__name__}.{name}"] = value
    return schemas


def assert_parquet_contract(path: Path) -> None:
    schema = pq.read_schema(path)
    assert schema.names.count("id") == 1, path
    assert schema.names[0] == "id", path
    assert (
        pa.types.is_string(schema.field("id").type)
        or pa.types.is_large_string(schema.field("id").type)
    ), path

    lowered_parts = tuple(part.lower() for part in path.parts)
    raw_indexes = [
        index for index, part in enumerate(lowered_parts) if part == "raw"
    ]
    raw = bool(raw_indexes)
    if raw:
        raw_source = (
            lowered_parts[raw_indexes[-1] - 1]
            if raw_indexes[-1] > 0
            else "unknown"
        )
        validate_raw_provider_id_columns(
            pd.DataFrame(columns=schema.names),
            source=raw_source,
        )
    else:
        forbidden = parquet_contracts.forbidden_identity_columns(schema.names)
        assert forbidden == [], f"{path}: {forbidden}"

    frame = pd.read_parquet(path, columns=["id"])
    if frame.empty:
        return
    values = frame["id"].astype("string")
    assert values.notna().all(), path
    assert values.str.strip().ne("").all(), path
    assert values.is_unique, path
    assert values.str.len().le(256).all(), path
    assert not values.map(lambda value: bool(_HEX_HASH.fullmatch(str(value)))).any()
    assert not values.map(lambda value: bool(_UUID.fullmatch(str(value)))).any()


def test_every_explicit_parquet_schema_has_one_readable_id() -> None:
    schemas = explicit_parquet_schemas()
    assert len(schemas) >= 6
    for name, schema in schemas.items():
        assert schema.names.count("id") == 1, name
        assert schema.names[0] == "id", name
        assert (
            pa.types.is_string(schema.field("id").type)
            or pa.types.is_large_string(schema.field("id").type)
        ), name
        assert parquet_contracts.forbidden_identity_columns(schema.names) == [], name
        assert not set(schema.names).intersection(
            parquet_contracts.CONTROL_PLANE_COLUMN_NAMES
        ), name


def test_loop_b_sample_schema_discards_workflow_only_columns() -> None:
    assert not set(parquet_contracts.SAMPLE_BASE_SCHEMA.names).intersection(
        parquet_contracts.NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS
    )
    assert {
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "label_available_at",
    }.issubset(parquet_contracts.SAMPLE_BASE_SCHEMA.names)

    for column in sorted(
        parquet_contracts.NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS
    ):
        with pytest.raises(ValueError, match="workflow-only columns"):
            parquet_contracts.sample_schema((column,))


@pytest.mark.parametrize(
    "column",
    sorted(parquet_contracts.CONTROL_PLANE_COLUMN_NAMES),
)
def test_loop_b_feature_columns_cannot_smuggle_control_state(column: str) -> None:
    with pytest.raises(ValueError, match="control-plane columns"):
        parquet_contracts.sample_schema((column,))


def test_four_horizon_ids_are_readable_and_cannot_collide() -> None:
    decision = pd.Timestamp("2026-07-30T15:05:00Z")
    frame = parquet_contracts.frame_with_readable_id(
        pd.DataFrame(
            {
                "symbol": "GOOG",
                "horizon": ["1h", "4h", "1d", "1w"],
                "decision_timestamp": decision,
            }
        ),
        key_columns=("symbol", "horizon", "decision_timestamp"),
    )

    assert (
        parquet_contracts.PREDICTION_SCHEMA.field("horizon").type
        == pa.string()
    )
    assert (
        parquet_contracts.INTELLIGENCE_SCHEMA.field("horizon").type
        == pa.string()
    )
    assert frame["id"].tolist() == [
        f"GOOG|{horizon}|2026-07-30T15:05:00Z"
        for horizon in ("1h", "4h", "1d", "1w")
    ]
    assert frame["id"].is_unique


def test_explicit_arrow_schemas_are_centralized_and_cannot_evade_guard() -> None:
    allowed = {
        Path("datafetching/bar_schema.py"),
        Path("ml/parquet_contracts.py"),
    }
    offenders: list[str] = []
    for root_name in PRODUCTION_ROOTS:
        for path in Path(root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "schema"
                    and path not in allowed
                ):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_loop_cycle_control_state_is_limited_to_causal_runtime_boundaries() -> None:
    # Options reads the last COMPLETE cutoff without taking the Loop A cycle
    # lock; it does not persist cycle fields into option Parquets.
    importers: set[Path] = set()
    for root_name in PRODUCTION_ROOTS:
        for path in Path(root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.ImportFrom)
                and node.module == "datafetching.loop_a_cycle"
                for node in ast.walk(tree)
            ):
                importers.add(path)
    assert importers == {
        Path("datafetching/options_runtime.py"),
        Path("datafetching/orchestrate.py"),
        Path("ml/prediction_runtime.py"),
    }


def test_readable_id_validator_rejects_hash_uuid_and_extra_ids() -> None:
    for value in (
        "a" * 64,
        "obs_" + "b" * 64,
        "sha256:" + "c" * 64,
        "NVDA|" + "d" * 64,
        "550e8400-e29b-41d4-a716-446655440000",
    ):
        with pytest.raises(ValueError, match="readable natural keys"):
            parquet_contracts.validate_readable_ids(pd.DataFrame({"id": [value]}))
    with pytest.raises(ValueError, match="forbidden identity columns"):
        parquet_contracts.validate_readable_ids(
            pd.DataFrame({"id": ["NVDA|2026-07-29T15:00:00Z"], "sample_id": ["x"]})
        )
    with pytest.raises(ValueError, match="forbidden identity columns"):
        parquet_contracts.validate_readable_ids(
            pd.DataFrame(
                {
                    "id": ["NVDA|2026-07-29T15:00:00Z"],
                    "checksum_sha256": ["e" * 64],
                    "content_identity": ["obsolete"],
                    "publicationId": ["obsolete"],
                    "lineageHash": ["obsolete"],
                    "eventGUID": ["obsolete"],
                }
            )
        )


def test_raw_provider_identifiers_are_preserved_without_an_allowlist(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    databento_path = store.save_raw_frame(
        source="databento",
        category="bars",
        symbol="NVDA",
        endpoint="ohlcv-1h",
        timeframe="1h",
        frame=pd.DataFrame(
            {
                "instrument_id": [11667, 11667],
                "publisher_id": [2, 2],
                "ts_event": [
                    pd.Timestamp("2026-07-29T14:00:00Z"),
                    pd.Timestamp("2026-07-29T15:00:00Z"),
                ],
                "open": [175.0, 176.0],
                "close": [176.0, 177.0],
            }
        ),
    )
    fred_path = store.save_raw_payload(
        source="fred",
        category="macro",
        symbol="GDP",
        endpoint="GDP",
        dataset_key="GDP",
        payload="DATE,GDP\n2026-01-01,1.0\n",
        metadata={"series": "GDP"},
        pool="macro",
    )
    assert databento_path is not None
    assert fred_path is not None
    assert_parquet_contract(databento_path)
    assert_parquet_contract(fred_path)
    assert {"instrument_id", "publisher_id"}.issubset(
        pd.read_parquet(databento_path).columns
    )
    fred_columns = pd.read_parquet(fred_path).columns
    assert "series" in fred_columns
    assert "series_id" not in fred_columns


def test_recursive_datastore_contract_helper_checks_every_parquet(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calculated" / "values.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "id": ["NVDA|2026-07-29T15:00:00Z"],
            "symbol": ["NVDA"],
            "timestamp": [pd.Timestamp("2026-07-29T15:00:00Z")],
        }
    ).to_parquet(path, index=False)
    for parquet in tmp_path.rglob("*.parquet"):
        assert_parquet_contract(parquet)
