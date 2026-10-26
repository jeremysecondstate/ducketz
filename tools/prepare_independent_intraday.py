"""Prepare a local 15m binding; no activation, broker calls or ledger mutation.

This one-time setup may read the previous local account fingerprint and legacy
15m terminal evidence. The new runtime never reads those paths. Existing local
history remains intact; no mixed ownership/fills are copied into the new epoch.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from ml.intraday.local_execution import VERSION, validate_binding, load_local_binding
from ml.intraday.runtime import local_symbols


def prepare(*, datastore, profile, watchlist, account_config):
    root = Path(datastore)
    actor, symbols = local_symbols(profile, watchlist, root)
    machine = {"Atlas": "pc-original", "Scout": "pc-new"}[actor]
    prior = json.loads(Path(account_config).read_text(encoding="utf-8"))
    if prior.get("machine_id") != machine or set(prior["participants"][machine]) != set(symbols):
        raise ValueError("Existing local account identity/symbols do not match this PC")
    legacy = root / "state/independent-stock-trader/holdings.sqlite3"
    own_terminal = 0
    if legacy.exists():
        with sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_streams'").fetchone():
                rows = db.execute("""SELECT r.status FROM reservations r JOIN intraday_streams s
                    ON s.kind='reservation' AND s.id=r.id WHERE s.stream=?""", (actor.lower()+"-15m",)).fetchall()
                if any(row[0] not in {"FILLED", "CANCELLED", "REJECTED"} for row in rows):
                    raise ValueError("Legacy local 15m has unresolved orders; preserve and recover their exact identities first")
                own_terminal = len(rows)
    old_authority = root / "state/account-gameplan/authority.sqlite3"
    if old_authority.exists():
        with sqlite3.connect(old_authority.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            pending = db.execute("""SELECT COUNT(*) FROM reservations
                WHERE json_extract(request,'$.stream')=?
                AND status NOT IN ('FILLED','CANCELLED','REJECTED','NOT_SUBMITTED')""",
                (actor.lower()+"-15m",)).fetchone()[0]
            if pending:
                raise ValueError("Legacy local 15m authority has unresolved orders; retain its exact acceptance receipts")
    value = {"schema_version": VERSION, "actor": actor, "machine": machine, "symbols": list(symbols),
             "account_fingerprint": prior["account_fingerprint"], "manual_start_only": True,
             "legacy_history_policy": "PRESERVE_ORIGINALS_NEW_BROKER_CAPACITY_EPOCH"}
    validate_binding(value)
    return value, {"legacy_local_terminal_orders": own_terminal, "orders_placed": 0,
                   "broker_calls": 0, "existing_ledgers_modified": False,
                   "opening_capacity": "OBSERVE_BROKER_ON_EXPLICIT_MANUAL_START"}


def prepare_existing(*, datastore, profile, watchlist):
    """Idempotent nightly preparation of this PC's existing independent binding.

    No legacy migration/scan, other trader, process or account connection is
    involved. This result is independent of nightly Gameplan synthesis.
    """
    actor, symbols = local_symbols(profile, watchlist, datastore)
    binding = load_local_binding(datastore)
    if binding.actor != actor or set(binding.symbols) != set(symbols):
        raise ValueError("Independent 15m binding differs from local profile")
    return {"schema_version":"independent-15m-nightly-preparation-v1",
            "status":"PREPARED_MANUAL_START_ONLY", "actor":actor, "stream":binding.stream,
            "symbols":list(symbols), "binding_sha256":binding.fingerprint,
            "manual_start_only":True, "gameplan_dependency":False, "peer_dependency":False,
            "workers_started":0, "orders_placed":0, "broker_calls":0,
            "existing_ledgers_modified":False,
            "live_checks":"OWN_BROKER_CAPACITY_QUOTES_AND_ORDER_STATUS_AT_MANUAL_START"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--account-config", type=Path)
    parser.add_argument("--nightly", action="store_true", help="Prepare only an existing independent binding; no legacy reads")
    parser.add_argument("--output", type=Path, help="New proposal/report file; equal existing bytes are idempotent")
    args = parser.parse_args(argv)
    if args.nightly:
        if args.account_config is not None:
            parser.error("--nightly cannot read legacy account configuration")
        result = prepare_existing(datastore=args.datastore, profile=args.profile, watchlist=args.watchlist)
        if args.output:
            raw = json.dumps(result, sort_keys=True, indent=2) + "\n"
            if args.output.exists():
                if args.output.read_text(encoding="utf-8") != raw:
                    raise ValueError("Existing nightly report differs; use a new run output")
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as file:
                    file.write(raw)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.account_config is None or args.output is None:
        parser.error("One-time setup requires --account-config and --output")
    binding, evidence = prepare(datastore=args.datastore, profile=args.profile,
        watchlist=args.watchlist, account_config=args.account_config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        file.write(json.dumps(binding, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": "PREPARED_MANUAL_START_ONLY", "output": str(args.output), **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
