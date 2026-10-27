"""Fresh generation controller/worker adapter; never a standalone initial start."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def bootstrap(generation, *, full=False):
    generation = Path(generation).resolve()
    source = generation / "source"
    # -I -S disables both PYTHONPATH and the production editable installation.
    # Only the sealed runtime supplies dependencies; bindings are read separately.
    sys.path.insert(0, str(source))
    from tools.trader_installation import verify
    selected = verify(generation.parent.parent, generation.name, generation.name, full=full)
    sys.path[:] = [str(source), *selected["stdlib_paths"], selected["site_packages"]]
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    # Certificates use the same installed runtime package without executing .pth
    # files (which may otherwise install editable finders or mutable path hooks).
    if (Path(selected["site_packages"]) / "pip_system_certs").is_dir():
        import pip_system_certs.wrapt_requests
        pip_system_certs.wrapt_requests.inject_truststore()
    return selected


def run(renewal):
    envelope = renewal.envelope
    repository, root = Path(envelope["repository"]), Path(envelope["datastore"])
    role = renewal.request["role"]
    if renewal.stopped():
        return {"status": "STOPPED"}
    # Local membership stays bound to the original application checkout, never
    # to a source snapshot's portable example watchlist.
    os.environ["DUCKETS_PRODUCTION_WATCHLIST"] = envelope["watchlist"]
    from dotenv import load_dotenv
    load_dotenv(repository / ".env", override=False)
    from app.services.schwab_token_store import configure_local_repository
    configure_local_repository(repository)
    if envelope["trader"] == "gameplan":
        from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
        if role == "initialize":
            from tools.native_ownership_cutover import ensure_gameplan_account_ready
            from ml.account_gameplan.config import assert_coordinator, load_account_config, verify_cutover
            from tools.trader_session_host import guarded_initial_control
            result = ensure_gameplan_account_ready(root, sizing_policy=GAMEPLAN_SIZING_POLICY,
                                                  repository=repository, renewal=renewal)
            if not isinstance(result, dict) or result.get("ready") is not True or result.get("status") != "ACCOUNT_READY":
                raise RuntimeError("Manual Gameplan account setup is not ready")
            config = load_account_config(root)
            assert_coordinator(config, GAMEPLAN_SIZING_POLICY)
            if config is not None:
                verify_cutover(root, config)
            if renewal.stopped():
                return {"status": "STOPPED"}
            # Initial manual-activation logic runs exactly once, never on renewal.
            for relative, key in (("controls/stock-trader/operator-intent.txt", "CONFIRM_ACTIVE_TRADING"),
                                  ("controls/gameplan-stock-trader/operator-intent.txt", "CONFIRM_GAMEPLAN_STOCK_TRADING")):
                if not guarded_initial_control(envelope, relative, (key + "=TRUE" + os.linesep).encode("utf-8")):
                    return {"status": "STOPPED"}
            return {"session": envelope["session"], "roles": ["execution"]}
        if role != "execution":
            raise ValueError("Unknown Gameplan continuation role")
        from ml.stock_trader.independent_session import run_independent_stock_session
        renewal.wait_due()
        return run_independent_stock_session(root,execute=True,wait_for_open=True,
                                            sizing_policy=GAMEPLAN_SIZING_POLICY,renewal=renewal)
    from ml.intraday.live_session import initialize_renewal_session, run_renewal_lane
    profile, watchlist = Path(envelope["profile"]), Path(envelope["watchlist"])
    if role == "initialize":
        from ml.intraday.runtime import ResearchConfig, local_symbols
        from ml.intraday.resource_policy import manual_symbol_workers
        actor, _ = local_symbols(profile, watchlist, root)
        options = dict(envelope["options"])
        options["symbol_workers"] = manual_symbol_workers(root,actor=actor,explicit=options["symbol_workers"])
        session = initialize_renewal_session(repository=repository,datastore=root,profile=profile,
                                            watchlist=watchlist,config=ResearchConfig(**options))
        return {"session": session, "roles": ["execution","forecast","scoring","candles","market_inputs"]}
    return run_renewal_lane(role,repository=repository,datastore=root,profile=profile,
                           watchlist=watchlist,renewal=renewal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_only and args.request is not None:
        parser.error("Verification cannot also run a session request")
    if not args.verify_only and args.request is None:
        parser.error("A living session request is required")
    selected = bootstrap(args.generation, full=args.verify_only)
    if args.verify_only:
        print(json.dumps({"verified": True, "generation": selected["generation"],
                          "runtime_id": selected["runtime_id"]}, sort_keys=True))
        return 0
    from tools.trader_session_host import bind_kernel_lifetime
    bind_kernel_lifetime()
    from ml.trader_renewal import Renewal, PlannedRenewal, PLANNED_EXIT
    renewal = Renewal(args.request)
    renewal.claim()
    os.environ["DUCKETZ_TRADER_RENEWAL_REQUEST"] = str(args.request)
    try:
        result = run(renewal)
        renewal.finish(result)
        return 0
    except PlannedRenewal:
        # No leases/threads may remain past this boundary. OS process exit is
        # acknowledged before the anchor admits the next generation.
        return PLANNED_EXIT
    except (KeyboardInterrupt, InterruptedError):
        if not renewal.stopped():
            raise
        renewal.finish({"status":"STOPPED"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
