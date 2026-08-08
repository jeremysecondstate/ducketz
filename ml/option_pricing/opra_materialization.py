from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.decision_time import latest_completed_bar_clock
from ml.option_pricing.causal import build_causal_samples, completed_bar_close
from ml.option_pricing.opra import (
    OPRA_RECEIPT_NAME,
    normalize_cbbo_records,
    normalize_definition_records,
    point_in_time_definition_asof,
    read_opra_import,
    select_historical_source_target,
)
from ml.option_pricing.policies import ContractSelectionPolicy


@dataclass(frozen=True)
class ClosedOpraLockboxInventory:
    """Receipt-derived lockbox metadata that contains no target quote values."""

    target_snapshot_fors: tuple[pd.Timestamp, ...]
    route_cluster_counts: Mapping[tuple[str, str], int]
    route_request_symbol_counts: Mapping[tuple[str, str], int]
    output_count: int
    outputs: tuple[Mapping[str, object], ...] = ()
    target_values_read: bool = False

    @property
    def cluster_count(self) -> int:
        return len(self.target_snapshot_fors)

    @property
    def start(self) -> pd.Timestamp | None:
        return self.target_snapshot_fors[0] if self.target_snapshot_fors else None

    @property
    def end(self) -> pd.Timestamp | None:
        return self.target_snapshot_fors[-1] if self.target_snapshot_fors else None


@dataclass(frozen=True)
class OpraMaterialization:
    samples: pd.DataFrame
    source_files: tuple[Path, ...]
    errors: Mapping[str, str]
    closed_lockbox: ClosedOpraLockboxInventory


def materialize_committed_opra_history(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy | None = None,
    target_snapshot_fors: Sequence[object] | None = None,
    allowed_cbbo_paths: Sequence[Path] | None = None,
    allowed_definition_paths: Sequence[Path] | None = None,
) -> tuple[pd.DataFrame, tuple[Path, ...], Mapping[str, str]]:
    """Materialize verified OPRA evidence as explicitly OFFLINE causal rows."""

    root = Path(datastore_root).resolve()
    evidence_root = root / "ml" / "option-pricing-evidence" / "opra"
    clean_symbols = {
        str(value).strip().upper() for value in symbols if str(value).strip()
    }
    selected_targets = (
        {
            pd.Timestamp(value).tz_localize("UTC")
            if pd.Timestamp(value).tzinfo is None
            else pd.Timestamp(value).tz_convert("UTC")
            for value in target_snapshot_fors
        }
        if target_snapshot_fors is not None
        else None
    )
    selected_cbbo = (
        {Path(path).resolve() for path in allowed_cbbo_paths}
        if allowed_cbbo_paths is not None
        else None
    )
    selected_definitions = (
        {Path(path).resolve() for path in allowed_definition_paths}
        if allowed_definition_paths is not None
        else None
    )
    if not evidence_root.is_dir():
        return pd.DataFrame(), (), {}
    definitions: list[pd.DataFrame] = []
    cbbo_imports: list[tuple[Path, Mapping[str, object]]] = []
    source_files: list[Path] = []
    errors: dict[str, str] = {}
    for receipt_path in sorted(evidence_root.glob(f"*/{OPRA_RECEIPT_NAME}")):
        try:
            verified = read_opra_import(receipt_path.parent, datastore_root=root)
            manifest = verified["manifest"]
            phase = str(manifest.get("phase"))
            if phase == "definitions":
                for name in manifest.get("outputs", {}):
                    path = receipt_path.parent / str(name)
                    if (
                        selected_definitions is not None
                        and path.resolve() not in selected_definitions
                    ):
                        continue
                    definitions.append(normalize_definition_records(_read_dbn(path)))
                    source_files.append(path)
            elif phase == "cbbo":
                cbbo_imports.append((receipt_path.parent, manifest))
            source_files.extend((receipt_path.parent / "manifest.json", receipt_path))
        except Exception as exc:
            errors[str(receipt_path.parent)] = f"{type(exc).__name__}: {exc}"
    if not definitions or not cbbo_imports:
        return pd.DataFrame(), tuple(dict.fromkeys(source_files)), errors
    definition_frame = pd.concat(definitions, ignore_index=True, sort=False)

    samples: list[pd.DataFrame] = []
    for directory, manifest in cbbo_imports:
        request_by_output = {
            str(request.get("output_name")): request
            for request in manifest.get("requests", [])
            if isinstance(request, Mapping)
        }
        for name in manifest.get("outputs", {}):
            request = request_by_output.get(str(name))
            route_name = f"{directory.name}/{name}"
            if request is None:
                errors[route_name] = "OpraImportError: CBBO output has no request metadata"
                continue
            parsed = _request_target(request)
            if parsed is None:
                errors[route_name] = "OpraImportError: CBBO request purpose has no target"
                continue
            symbol, target = parsed
            if symbol not in clean_symbols:
                continue
            path = directory / str(name)
            if selected_targets is not None and target not in selected_targets:
                continue
            if selected_cbbo is not None and path.resolve() not in selected_cbbo:
                continue
            try:
                cbbo = normalize_cbbo_records(_read_dbn(path))
                source_quotes, target_quotes = select_historical_source_target(
                    cbbo,
                    target_snapshot_for=target,
                )
                if source_quotes.empty or target_quotes.empty:
                    raise ValueError("Required backward source or forward target CBBO is missing")
                source_time = pd.Timestamp(source_quotes["quote_timestamp"].max())
                target_observed_at = pd.Timestamp(target_quotes["quote_timestamp"].max())
                definitions_asof = point_in_time_definition_asof(
                    definition_frame.loc[
                        definition_frame["symbol"].astype("string").str.upper().eq(symbol)
                    ],
                    source_time,
                )
                if definitions_asof.empty:
                    raise ValueError("No point-in-time definition existed by source surface")
                source_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=source_time)
                target_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=target)
                if pd.Timestamp(target_clock.decision_timestamp) != target:
                    raise ValueError("No exact completed underlying bar at OPRA target")
                source_underlying = completed_bar_close(source_clock)
                target_underlying = completed_bar_close(target_clock)
                source_contracts = _opra_contract_frame(
                    source_quotes,
                    definitions_asof,
                    underlying_price=source_underlying,
                    target_snapshot_for=target,
                )
                target_contracts = _opra_contract_frame(
                    target_quotes,
                    definitions_asof,
                    underlying_price=target_underlying,
                    target_snapshot_for=target,
                )
                frame = build_causal_samples(
                    source_contracts,
                    target_contracts=target_contracts,
                    target_underlying_price=target_underlying,
                    source_snapshot_for=source_time,
                    source_available_at=source_time,
                    target_snapshot_for=target,
                    source_provider="databento-opra",
                    prediction_mode="OFFLINE",
                    observed_available_at=target_observed_at,
                    contract_policy=contract_policy,
                    rate_observations=rate_observations,
                )
                samples.append(frame)
                source_files.extend((path, source_clock.source_file, target_clock.source_file))
            except Exception as exc:
                errors[route_name] = f"{type(exc).__name__}: {exc}"
    return (
        pd.concat(samples, ignore_index=True, sort=False) if samples else pd.DataFrame(),
        tuple(dict.fromkeys(source_files)),
        errors,
    )


def materialize_committed_opra_history_v2(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    closed_lockbox_clusters: int,
    eligibility_policy_hash: str,
    contract_policy: ContractSelectionPolicy | None = None,
) -> OpraMaterialization:
    """Materialize only pre-lockbox OPRA targets.

    The final configured target clusters are selected exclusively from verified
    import request metadata. Their DBN payloads are never decoded by this path.
    This lets the normal runtime prove that a lockbox exists while keeping target
    quotes inaccessible until the separately authorized one-time evaluator runs.
    """

    if int(closed_lockbox_clusters) < 1:
        raise ValueError("closed_lockbox_clusters must be positive")
    root = Path(datastore_root).resolve()
    evidence_root = root / "ml" / "option-pricing-evidence" / "opra"
    clean_symbols = {
        str(value).strip().upper() for value in symbols if str(value).strip()
    }
    empty_lockbox = ClosedOpraLockboxInventory((), {}, {}, 0)
    if not evidence_root.is_dir():
        return OpraMaterialization(pd.DataFrame(), (), {}, empty_lockbox)

    definitions: list[pd.DataFrame] = []
    definition_files: list[Path] = []
    metadata_files: list[Path] = []
    cbbo_outputs: list[
        tuple[
            Path,
            str,
            Mapping[str, object],
            str,
            pd.Timestamp,
            Mapping[str, object],
        ]
    ] = []
    errors: dict[str, str] = {}
    for receipt_path in sorted(evidence_root.glob(f"*/{OPRA_RECEIPT_NAME}")):
        try:
            verified = read_opra_import(receipt_path.parent, datastore_root=root)
            manifest = verified["manifest"]
            phase = str(manifest.get("phase"))
            if phase == "definitions":
                metadata_files.extend(
                    (receipt_path.parent / "manifest.json", receipt_path)
                )
                for name in manifest.get("outputs", {}):
                    path = receipt_path.parent / str(name)
                    definitions.append(normalize_definition_records(_read_dbn(path)))
                    definition_files.append(path)
                continue
            if phase != "cbbo":
                continue
            policy_reference = manifest.get("eligibility_policy")
            policy_reference = (
                policy_reference if isinstance(policy_reference, Mapping) else {}
            )
            if policy_reference.get("policy_hash") != eligibility_policy_hash:
                continue
            metadata_files.extend(
                (receipt_path.parent / "manifest.json", receipt_path)
            )
            request_by_output = {
                str(request.get("output_name")): request
                for request in manifest.get("requests", [])
                if isinstance(request, Mapping)
            }
            for name in manifest.get("outputs", {}):
                request = request_by_output.get(str(name))
                route_name = f"{receipt_path.parent.name}/{name}"
                if request is None:
                    errors[route_name] = (
                        "OpraImportError: CBBO output has no request metadata"
                    )
                    continue
                parsed = _request_target(request)
                if parsed is None:
                    errors[route_name] = (
                        "OpraImportError: CBBO request purpose has no target"
                    )
                    continue
                symbol, target = parsed
                if symbol in clean_symbols:
                    output_metadata = manifest.get("outputs", {}).get(str(name), {})
                    output_metadata = (
                        output_metadata
                        if isinstance(output_metadata, Mapping)
                        else {}
                    )
                    cbbo_outputs.append(
                        (
                            receipt_path.parent,
                            str(name),
                            request,
                            symbol,
                            target,
                            output_metadata,
                        )
                    )
        except Exception as exc:
            errors[str(receipt_path.parent)] = f"{type(exc).__name__}: {exc}"

    all_targets = tuple(
        sorted({target for _, _, _, _, target, _ in cbbo_outputs})
    )
    locked_targets = tuple(all_targets[-int(closed_lockbox_clusters) :])
    locked_set = set(locked_targets)
    route_targets: dict[tuple[str, str], set[pd.Timestamp]] = {}
    route_symbols: dict[tuple[str, str], int] = {}
    locked_output_count = 0
    locked_outputs: list[Mapping[str, object]] = []
    for directory, name, request, symbol, target, output_metadata in cbbo_outputs:
        if target not in locked_set:
            continue
        locked_output_count += 1
        call_puts = _request_call_puts(request)
        locked_outputs.append(
            {
                "path": (directory / name).relative_to(root).as_posix(),
                "size": output_metadata.get("size"),
                "checksum_sha256": output_metadata.get("checksum_sha256"),
                "symbol": symbol,
                "target_snapshot_for": target.isoformat(),
                "call_put_routes": sorted(call_puts),
                "request": dict(request),
                "import_manifest_path": (
                    directory / "manifest.json"
                ).relative_to(root).as_posix(),
                "import_receipt_path": (
                    directory / OPRA_RECEIPT_NAME
                ).relative_to(root).as_posix(),
            }
        )
        for call_put in call_puts:
            route = (symbol, call_put)
            route_targets.setdefault(route, set()).add(target)
            route_symbols[route] = route_symbols.get(route, 0) + sum(
                1
                for raw in request.get("symbols", ())
                if _raw_symbol_call_put(raw) == call_put
            )
    lockbox = ClosedOpraLockboxInventory(
        target_snapshot_fors=locked_targets,
        route_cluster_counts={
            route: len(targets) for route, targets in sorted(route_targets.items())
        },
        route_request_symbol_counts=dict(sorted(route_symbols.items())),
        output_count=locked_output_count,
        outputs=tuple(locked_outputs),
    )

    if not definitions:
        return OpraMaterialization(
            pd.DataFrame(),
            tuple(dict.fromkeys(metadata_files)),
            errors,
            lockbox,
        )
    definition_frame = pd.concat(definitions, ignore_index=True, sort=False)
    samples: list[pd.DataFrame] = []
    consumed_files = [*metadata_files, *definition_files]
    for directory, name, request, symbol, target, _ in cbbo_outputs:
        # This conditional is deliberately before _read_dbn. Locked target
        # values cannot enter fitting, calibration, assessment, or reporting.
        if target in locked_set:
            continue
        route_name = f"{directory.name}/{name}"
        path = directory / name
        try:
            cbbo = normalize_cbbo_records(_read_dbn(path))
            source_quotes, target_quotes = select_historical_source_target(
                cbbo,
                target_snapshot_for=target,
            )
            if source_quotes.empty or target_quotes.empty:
                raise ValueError(
                    "Required backward source or forward target CBBO is missing"
                )
            source_time = pd.Timestamp(source_quotes["quote_timestamp"].max())
            target_observed_at = pd.Timestamp(target_quotes["quote_timestamp"].max())
            definitions_asof = point_in_time_definition_asof(
                definition_frame.loc[
                    definition_frame["symbol"]
                    .astype("string")
                    .str.upper()
                    .eq(symbol)
                ],
                source_time,
            )
            if definitions_asof.empty:
                raise ValueError("No point-in-time definition existed by source surface")
            source_clock = latest_completed_bar_clock(
                root, symbol=symbol, as_of=source_time
            )
            target_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=target)
            if pd.Timestamp(target_clock.decision_timestamp) != target:
                raise ValueError("No exact completed underlying bar at OPRA target")
            source_underlying = completed_bar_close(source_clock)
            target_underlying = completed_bar_close(target_clock)
            source_contracts = _opra_contract_frame(
                source_quotes,
                definitions_asof,
                underlying_price=source_underlying,
                target_snapshot_for=target,
            )
            target_contracts = _opra_contract_frame(
                target_quotes,
                definitions_asof,
                underlying_price=target_underlying,
                target_snapshot_for=target,
            )
            frame = build_causal_samples(
                source_contracts,
                target_contracts=target_contracts,
                target_underlying_price=target_underlying,
                source_snapshot_for=source_time,
                source_available_at=source_time,
                target_snapshot_for=target,
                source_provider="databento-opra",
                prediction_mode="OFFLINE",
                observed_available_at=target_observed_at,
                contract_policy=contract_policy,
                rate_observations=rate_observations,
            )
            samples.append(frame)
            consumed_files.extend((path, source_clock.source_file, target_clock.source_file))
        except Exception as exc:
            errors[route_name] = f"{type(exc).__name__}: {exc}"
    return OpraMaterialization(
        pd.concat(samples, ignore_index=True, sort=False)
        if samples
        else pd.DataFrame(),
        tuple(dict.fromkeys(consumed_files)),
        errors,
        lockbox,
    )


def _opra_contract_frame(
    quotes: pd.DataFrame,
    definitions: pd.DataFrame,
    *,
    underlying_price: float,
    target_snapshot_for: pd.Timestamp,
) -> pd.DataFrame:
    merged = quotes.merge(
        definitions,
        on="contract_symbol",
        how="inner",
        validate="one_to_one",
        suffixes=("", "_definition"),
    )
    output = pd.DataFrame(
        {
            "symbol": merged["symbol"].astype("string").str.upper(),
            "contract_symbol": merged["contract_symbol"],
            "call_put": merged["call_put"].astype("string").str.upper(),
            "expiration_date": merged["expiration_date"],
            "strike": merged["strike"],
            "underlying_price": float(underlying_price),
            "bid": merged["bid"],
            "ask": merged["ask"],
            "multiplier": merged["multiplier"],
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "interest_rate": float("nan"),
            "dividend_yield": float("nan"),
            "implied_volatility": float("nan"),
            "quote_timestamp": merged["quote_timestamp"],
            "quote_staleness_seconds": (
                target_snapshot_for
                - pd.to_datetime(merged["quote_timestamp"], utc=True, errors="coerce")
            ).dt.total_seconds().clip(lower=0),
        }
    )
    return output


def _request_target(request: Mapping[str, object]) -> tuple[str, pd.Timestamp] | None:
    purpose = str(request.get("purpose") or "")
    parts = purpose.split(":", 2)
    if len(parts) != 3 or parts[0] != "SOURCE_BACKWARD_TARGET_FORWARD":
        return None
    timestamp = pd.to_datetime(parts[2], utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return parts[1].strip().upper(), pd.Timestamp(timestamp)


def _raw_symbol_call_put(raw_symbol: object) -> str | None:
    match = re.match(
        r"^[A-Z.]{1,6}\s*\d{6}([CP])", str(raw_symbol).strip().upper()
    )
    if match is None:
        return None
    return "CALL" if match.group(1) == "C" else "PUT"


def _request_call_puts(request: Mapping[str, object]) -> set[str]:
    return {
        value
        for value in (_raw_symbol_call_put(raw) for raw in request.get("symbols", ()))
        if value is not None
    }


def _read_dbn(path: Path) -> pd.DataFrame:
    import databento as db

    return db.DBNStore.from_file(path).to_df(
        price_type="fixed",
        pretty_ts=False,
        map_symbols=True,
    ).reset_index()


__all__ = [
    "ClosedOpraLockboxInventory",
    "OpraMaterialization",
    "materialize_committed_opra_history",
    "materialize_committed_opra_history_v2",
]
