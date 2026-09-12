"""Validate analyst dossiers, compute the report, and publish one weekly edition."""
from __future__ import annotations

from datetime import date
import statistics

import pandas as pd

from .policy import VERSION, STATUSES, WEIGHTS, assess, sources_valid, text, timestamp, symbol
from .storage import ResearchStore, digest, encode
from .tracking import evaluate, latest_session, windows


def week_key(value: str) -> str:
    day = date.fromisoformat(value)
    if day.weekday() != 6:
        raise ValueError("The edition key must be a Sunday date")
    return day.isoformat()


def price_evidence(candidate: dict, company: dict, last: str) -> None:
    if company["symbol"] != candidate["symbol"]:
        raise ValueError("Company snapshot belongs to another symbol")
    data = company["datasets"]
    profiles = data["profile"]["rows"]
    matching = [p for p in profiles if p.get("symbol") == candidate["symbol"]]
    if len(matching) != 1:
        raise ValueError("Missing unambiguous company identity")
    profile = matching[0]
    cik = str(profile.get("cik", "")).zfill(10)
    if (not candidate["security_id"].startswith(f"CIK{cik}:")
            or profile.get("currency") != "USD" or profile.get("isEtf") or profile.get("isFund")
            or profile.get("exchange") not in {"NASDAQ", "NYSE", "AMEX"}):
        raise ValueError("Company identity is not an eligible US-listed USD equity")
    rows = [r for r in data["daily_prices"]["rows"] if r.get("date", "") <= last]
    latest = [r for r in rows if r.get("date") == last]
    if len(latest) != 1 or float(latest[0]["close"]) <= 0:
        raise ValueError("Missing exact reference-session close")
    if abs(candidate["reference_price"] / float(latest[0]["close"]) - 1) > 0.001:
        raise ValueError("Reference price disagrees with the archived daily close")
    recent = sorted(rows, key=lambda r: r["date"])[-20:]
    if len(recent) != 20 or len({r["date"] for r in recent}) != 20:
        raise ValueError("Need twenty distinct observed sessions for liquidity")
    liquidity = statistics.median(float(r["close"]) * float(r["volume"]) for r in recent)
    if abs(candidate["median_daily_dollar_volume_20d"] - liquidity) > max(1, liquidity * 0.001):
        raise ValueError("Liquidity must match the archived twenty-session median")


def build_publication(store: ResearchStore, dossier: dict, company_refs: dict,
                      price_refs: dict, now: object) -> dict:
    current = timestamp(now)
    cutoff = timestamp(dossier["research_cutoff"])
    if cutoff > current or current - cutoff > pd.Timedelta(hours=24):
        raise ValueError("Research cutoff must be within the last 24 hours")
    week = week_key(dossier["week"])
    local_day = current.tz_convert("America/Los_Angeles").date()
    if not date.fromisoformat(week) <= local_day < date.fromisoformat(week) + pd.Timedelta(days=7):
        raise ValueError("Publish only in the edition's own Sunday-Saturday week")
    text(dossier["summary"], "weekly summary")
    history = store.history()
    calls = store.calls(history)
    old = {c["security_id"]: c for c in calls}
    last = latest_session(current)
    references = {}

    def load(reference):
        payload = store.read_snapshot(reference)
        if timestamp(payload["fetched_at"]) > cutoff:
            raise ValueError("Snapshot was fetched after the research cutoff")
        references[reference["sha256"]] = reference
        return payload

    prices = {ticker: {**load(reference), **reference} for ticker, reference in price_refs.items()}
    for ticker, snapshot in prices.items():
        if snapshot["symbol"] != ticker:
            raise ValueError("Price snapshot identity mismatch")
    scores, new_calls, identities = {}, [], set()
    for candidate in dossier["candidates"]:
        identity = candidate["security_id"]
        if identity in identities:
            raise ValueError("Duplicate company/share class in this edition")
        identities.add(identity)
        assessment = assess(candidate, cutoff, last)
        scores[identity] = assessment
        if candidate["symbol"] not in company_refs:
            raise ValueError("Every full memo needs an archived FMP company snapshot")
        company = load(company_refs[candidate["symbol"]])
        if candidate["decision"] == "recommend":
            price_evidence(candidate, company, last)
        if identity in old:
            if (candidate["symbol"] != old[identity]["symbol"] or
                    candidate["sector_benchmark"] != old[identity]["sector_benchmark"]):
                raise ValueError("Existing call identity and benchmark are frozen; resolve corporate actions explicitly")
        elif candidate["decision"] == "recommend":
            new_calls.append({"call_id": f"{week}/{identity}", "security_id": identity,
                              "symbol": candidate["symbol"], "track": candidate["track"],
                              "sector_benchmark": candidate["sector_benchmark"], "published_at": current.isoformat(),
                              "windows": windows(current), "original": candidate, "assessment": assessment})
    if len(new_calls) > 4:
        raise ValueError("At most four new recommendations per weekly edition")
    if not new_calls:
        text(dossier["no_new_recommendations_reason"], "reason for no new recommendations")
    screening = dossier["screening"]
    seen = set()
    for item in screening:
        ticker = symbol(item["symbol"])
        if ticker in seen or item["track"] not in {"established", "emerging"}:
            raise ValueError("Screening entries must be unique and use a recognized track")
        seen.add(ticker)
        text(item["reason"], "screening disposition")
        if item["disposition"] not in {"investigate", "watch", "pass"}:
            raise ValueError("Invalid screening disposition")
    if not {c["symbol"] for c in dossier["candidates"]}.issubset(seen):
        raise ValueError("All full memos must appear in the screening record")
    if not isinstance(dossier["limitations"], list):
        raise ValueError("Limitations must be an explicit list")
    if (len(screening) < 12 or len(dossier["candidates"]) < 2 or
            {i["track"] for i in screening} != {"established", "emerging"}) and not dossier["limitations"]:
        raise ValueError("Incomplete coverage needs an explicit limitation")
    due_ids = {c["call_id"] for c in calls if "12m" not in store.frozen(c["call_id"], history)}
    all_ids = {c["call_id"] for c in calls}
    reviewed = set()
    for update in dossier["updates"]:
        identity = update["call_id"]
        if identity not in all_ids or identity in reviewed or update["status"] not in STATUSES:
            raise ValueError("Invalid or duplicate prior-call update")
        reviewed.add(identity)
        text(update["thesis_update"], "thesis update")
        text(update["catalyst_update"], "catalyst update")
        sources_valid(update["sources"], cutoff)
        if not update["sources"]:
            raise ValueError("Prior-call checks need dated evidence, including unchanged conclusions")
    if not due_ids.issubset(reviewed):
        raise ValueError("Every unresolved prior call must be reviewed, including withdrawn calls")
    tracked = [evaluate(c, prices, current, store.frozen(c["call_id"], history)) for c in calls + new_calls]
    return {"policy_version": VERSION, "week": week, "published_at": current.isoformat(),
            "input_sha256": digest(encode({"dossier": dossier, "company_refs": company_refs, "price_refs": price_refs})),
            "previous_publication_sha256": history[-1]["publication_sha256"] if history else None,
            "dossier": dossier, "assessments": scores, "new_calls": new_calls, "updates": dossier["updates"],
            "tracking": tracked, "snapshot_refs": list(references.values())}


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1%}"


def render_report(publication: dict) -> str:
    dossier = publication["dossier"]
    lines = [f"# Weekly Opportunity Research - {publication['week']}", "",
             f"Published: {publication['published_at']} | Research cutoff: {dossier['research_cutoff']}", "",
             dossier["summary"], "", "## Previous recommendations", "",
             "Hypothetical total returns use dividend-adjusted prices from the next regular-session open. "
             "No transaction costs or taxes are assumed. Mature six- and twelve-month results remain frozen. "
             "Drawdown uses daily closing wealth; it does not measure intraday losses.", "",
             "| Company | Entry session | Past week | Since inception* | vs. SPY | vs. sector | Drawdown | Six months | Twelve months |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in publication["tracking"]:
        inception = row["windows"]["since_inception"]
        stock = inception.get("series", {}).get("stock", {})
        value = percent(stock.get("total_return")) if inception["status"] == "EVALUATED" else inception["status"]
        def mature(label):
            item = row["windows"][label]
            return percent(item["series"]["stock"]["total_return"]) if item["status"] == "EVALUATED" else item["status"]
        lines.append(f"| {row['symbol']} | {row['entry_session']} | {percent(row['week'].get('total_return'))} | "
                     f"{value} | {percent(inception.get('excess_vs_spy'))} | {percent(inception.get('excess_vs_sector'))} | "
                     f"{percent(stock.get('max_close_drawdown'))} | {mature('6m')} | {mature('12m')} |")
    if not publication["tracking"]:
        lines += ["", "No recommendations have been published yet."]
    lines += ["", "*Tracking runs through the fixed twelve-month endpoint. Returns are research-call outcomes, not brokerage P/L."]
    for horizon in ("6m", "12m"):
        values = [r["windows"][horizon] for r in publication["tracking"]]
        complete = [v for v in values if v["status"] == "EVALUATED"]
        missing = sum(v["status"] == "MISSING_DATA" for v in values)
        pending = len(values) - len(complete) - missing
        lines += ["", f"{horizon}: {len(complete)} evaluated; {pending} pending; {missing} missing data."]
        if complete:
            mean = statistics.mean(v["excess_vs_spy"] for v in complete)
            wins = sum(v["excess_vs_spy"] > 0 for v in complete)
            lines += [f"Mean excess return vs. SPY: {percent(mean)}; beat SPY: {wins}/{len(complete)}. "
                      "These overlapping calls are not independent observations or a portfolio backtest."]
    for update in dossier["updates"]:
        lines += ["", f"### {update['call_id']} - {update['status']}", "", update["thesis_update"],
                  "", update["catalyst_update"]]
        lines += [f"- [{s['title']}]({s['url']}) - {s['supports']}" for s in update["sources"]]
    lines += ["", "## This week's investigations", ""]
    if dossier.get("no_new_recommendations_reason"):
        lines += [dossier["no_new_recommendations_reason"], ""]
    for candidate in dossier["candidates"]:
        assessment = publication["assessments"][candidate["security_id"]]
        lines += [f"### {candidate['company_name']} ({candidate['symbol']}) - {candidate['decision'].upper()}", "",
                  f"{candidate['track'].title()} | Reference close ${candidate['reference_price']:,.2f} "
                  f"on {candidate['reference_session']} | Score {assessment['score']:.0f}/100", "",
                  "Scores are versioned analyst judgments; they are not calibrated probabilities.", ""]
        for field, label in (("business", "Business"), ("product_evidence", "Product evidence"),
                             ("mispricing", "Why the price may be wrong"), ("reverse_valuation", "What today's price requires"),
                             ("financing", "Financing and dilution"), ("accounting_adjustments", "Accounting and reinvestment"),
                             ("countercase", "Strongest opposing case"), ("invalidation", "What would break the thesis")):
            lines += [f"**{label}:** {candidate[field]}", ""]
        values = assessment["fair_values"]
        if values is not None:
            lines += ["| Scenario | Estimated value today | Six-month price | Twelve-month price |",
                      "|---|---|---|---|"]
            for name in ("bear", "base", "bull"):
                targets = candidate["price_targets"]
                six = f"${targets['6m'][name]:,.2f}" if targets else "Unavailable"
                twelve = f"${targets['12m'][name]:,.2f}" if targets else "Unavailable"
                lines += [f"| {name.title()} | ${values[name]:,.2f} | {six} | {twelve} |"]
            lines += ["", f"Discount to base estimated value: {assessment['discount_to_base_value']:.1%}.", ""]
            for name in ("bear", "base", "bull"):
                lines += [f"**{name.title()} valuation assumptions:** {candidate['valuation'][name]['assumptions']}", ""]
        else:
            lines += ["Present valuation is unresolved. This company cannot qualify as a recommendation.", ""]
        if candidate["price_targets"]:
            for horizon in ("6m", "12m"):
                lines += [f"**{horizon} price rationale:** {candidate['price_targets'][horizon]['rationale']}", ""]
        lines += [f"**Benchmark:** {candidate['sector_benchmark']}. {candidate['benchmark_rationale']}", ""]
        for dimension in WEIGHTS:
            score = candidate["scores"][dimension]
            lines += [f"- {dimension.replace('_', ' ').title()}: {score['rating']}/5. {score['rationale']}"]
        if assessment["failed_gates"]:
            lines += ["", "Recommendation barriers: " + ", ".join(assessment["failed_gates"]) + "."]
        lines += ["", "**Catalysts:**", ""]
        for catalyst in candidate["catalysts"]:
            lines += [f"- {catalyst['due_date']}: {catalyst['milestone']} "
                      f"Success test: {catalyst['success_test']} If it disappoints: {catalyst['failure_consequence']}"]
        lines += ["", "**Evidence:**", ""]
        for source in candidate["sources"]:
            lines += [f"- [{source['id']}: {source['title']}]({source['url']}) "
                      f"({source['kind']}; accessed {source['accessed_at']}). {source['supports']}"]
        lines += [""]
    lines += ["## Discovery record", "", "| Company | Track | Disposition | Reason |", "|---|---|---|---|"]
    for item in dossier["screening"]:
        reason = item["reason"].replace("|", "\\|").replace("\n", " ")
        lines += [f"| {item['symbol']} | {item['track']} | {item['disposition']} | {reason} |"]
    lines += ["", "## Coverage and method", "", f"Policy: {VERSION}. New recommendations: {len(publication['new_calls'])}."]
    lines += [f"- {item}" for item in dossier["limitations"]] or ["No additional coverage limitations reported."]
    lines += ["", "Original assumptions, numerical valuation inputs, source snapshots, and outcomes are retained "
              "in the adjacent publication.json and checksum receipt.", ""]
    return "\n".join(lines)
