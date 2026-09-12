"""Native CLI used by the Sunday Codex research task."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from datafetching.parquet_store import resolve_datastore_dir

from .policy import VERSION
from .providers import fetch_companies, fetch_prices
from .storage import ResearchStore, atomic_write, digest, encode
from .tracking import evaluate, latest_session
from .workflow import build_publication, render_report, week_key


def read_json(path: Path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "companies", "refresh", "validate", "publish", "status", "verify"))
    parser.add_argument("--datastore-target", choices=("pc", "local"), default="pc")
    parser.add_argument("--datastore-dir", type=Path)
    parser.add_argument("--week", help="Sunday edition date; defaults to the coming/current Sunday")
    parser.add_argument("--symbols", nargs="+", default=[])
    parser.add_argument("--input", type=Path, help="Analyst dossier JSON; defaults to the week's draft")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    now = pd.Timestamp.now(tz="UTC")
    local = now.tz_convert("America/Los_Angeles").date()
    upcoming = local + pd.Timedelta(days=(6 - local.weekday()) % 7)
    week = week_key(args.week or upcoming.isoformat())
    store = ResearchStore(resolve_datastore_dir(root_dir=args.datastore_dir, target=args.datastore_target))
    draft = store.root / "drafts" / week
    company_index, price_index = draft / "companies.json", draft / "prices.json"
    input_path = args.input or draft / "research.json"
    history = store.history()
    calls = store.calls(history)
    due = [c for c in calls if "12m" not in store.frozen(c["call_id"], history)]
    last = latest_session(now)
    if args.command in {"status", "verify"}:
        print(json.dumps({"verified": True, "root": str(store.root), "policy_version": VERSION,
                          "editions": len(history), "recommendations": len(calls), "unresolved_calls": len(due),
                          "latest_report": history[-1]["report_path"] if history else None,
                          "latest_completed_session": last}, indent=2))
        return 0
    draft.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        brief = {"week": week, "prepared_at": now.isoformat(), "latest_completed_session": last,
                 "policy_version": VERSION, "all_calls": calls, "due_calls": due,
                 "latest_report": history[-1]["report_path"] if history else None,
                 "previous_screening": history[-1]["dossier"]["screening"] if history else [],
                 "contract": str(Path(__file__).resolve().parents[2] / "docs/opportunity-research/WORKFLOW.md")}
        atomic_write(draft / "brief.json", encode(brief))
        if not input_path.exists():
            atomic_write(input_path, encode({"week": week, "research_cutoff": now.isoformat(), "summary": "",
                "screening": [], "candidates": [], "updates": [], "limitations": [],
                "no_new_recommendations_reason": ""}))
        print(json.dumps({"brief": str(draft / "brief.json"), "dossier": str(input_path),
                          "due_calls": len(due), "already_published": any(e["week"] == week for e in history)}, indent=2))
    elif args.command == "companies":
        if not args.symbols:
            parser.error("companies requires --symbols")
        refs = read_json(company_index, {})
        refs.update(fetch_companies(store, args.symbols, (now - pd.Timedelta(days=60)).date().isoformat(), last))
        atomic_write(company_index, encode(refs))
        summaries = {}
        for ticker in args.symbols:
            company = store.read_snapshot(refs[ticker])
            summaries[ticker] = {"snapshot": str(store.root / refs[ticker]["path"]),
                                 "datasets": {k: {"rows": len(v["rows"]), "error": v["error"]}
                                              for k, v in company["datasets"].items()}}
        print(json.dumps(summaries, indent=2))
    elif args.command == "refresh":
        tickers = sorted({ticker for c in due for ticker in (c["symbol"], c["sector_benchmark"], "SPY")})
        start = min([c["windows"]["entry_session"] for c in due] + [(now - pd.Timedelta(days=60)).date().isoformat()])
        refs = fetch_prices(store, tickers, start, last)
        atomic_write(price_index, encode(refs))
        loaded = {s: {**store.read_snapshot(r), **r} for s, r in refs.items()}
        results = [evaluate(c, loaded, now, store.frozen(c["call_id"], history)) for c in calls]
        atomic_write(draft / "tracking.json", encode(results))
        print(json.dumps({"tracking": str(draft / "tracking.json"), "calls": len(calls),
                          "provider_errors": [s for s, r in loaded.items() if r.get("error")],
                          "snapshots": len(refs)}, indent=2))
    else:
        dossier = read_json(input_path)
        if dossier is None:
            raise ValueError("Prepare and complete the analyst dossier first")
        if dossier["week"] != week:
            raise ValueError("Dossier and command refer to different weeks")
        companies, prices = read_json(company_index, {}), read_json(price_index, {})
        identity = digest(encode({"dossier": dossier, "company_refs": companies, "price_refs": prices}))
        existing = next((e for e in history if e["week"] == week), None)
        if existing:
            if existing["input_sha256"] != identity:
                raise ValueError("This edition is immutable; record new evidence in the next edition")
            if args.command == "publish":
                store.export_ledger(history)
            print(json.dumps({"already_published": True, "report": existing["report_path"]}, indent=2))
            return 0
        publication = build_publication(store, dossier, companies, prices, now)
        report = render_report(publication)
        if args.command == "validate":
            path = draft / "preview.md"
            atomic_write(path, report.encode("utf-8"))
        else:
            path = store.publish(publication, report)
        print(json.dumps({"valid": True, "published": args.command == "publish", "report": str(path),
                          "new_recommendations": len(publication["new_calls"])}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as exc:
        # No provider exception text reaches this boundary; providers redact at the source.
        raise SystemExit(f"Research workflow stopped: {exc}") from None
