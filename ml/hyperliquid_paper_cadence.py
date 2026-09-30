"""Durable experiment-duration ladder; never starts workers or edits schedules.

An assessment belongs to the ending experiment's committed duration. Its
decision is consumed exactly once, after a verified native rollover completes.
The current run can be adopted as an unscored carry-in without resetting it.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from filelock import FileLock

from ml.hyperliquid_paper_review import assert_not_excluded, read_json, safe_path, seed_info, write_json

VERSION = 1
LEGACY_RULE = "win-plus-one-hold-v1"
CURRENT_RULE = "win-plus-one-loss-minus-two-floor-one-v2"
LATE_SECONDS = 300
ACCOUNTS = {"alex", "jeremy", "clearpond"}
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}")


def utc():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Expected a timezone-aware timestamp string")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Expected a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Expected a finite numeric value, not a boolean")
    return float(value)


def hours(value):
    if type(value) is not int or value < 1:
        raise ValueError("Experiment duration must be an integer of at least one hour")
    return value


def rule_version(value):
    version = value.get("cadence_rule_version", LEGACY_RULE)
    if version not in (LEGACY_RULE, CURRENT_RULE):
        raise ValueError("Unsupported cadence rule version")
    return version


def policy(version):
    if version == LEGACY_RULE:
        return {"on_win": "add_one_hour", "on_loss_tie_or_unscored": "retain_current_hours",
                "late_allowance_seconds": LATE_SECONDS}
    if version != CURRENT_RULE:
        raise ValueError("Unsupported cadence rule version")
    return {"on_win": "add_one_hour", "on_loss": "subtract_two_hours_floor_one",
            "on_tie_or_unscored": "retain_current_hours", "minimum_evaluation_hours": 1,
            "late_allowance_seconds": LATE_SECONDS}


def next_hours(duration, outcome, version):
    duration = hours(duration)
    if version not in (LEGACY_RULE, CURRENT_RULE):
        raise ValueError("Unsupported cadence rule version")
    if outcome == "win":
        return duration + 1
    if outcome == "loss" and version == CURRENT_RULE:
        return max(1, duration - 2)
    return duration


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_active(active):
    if not isinstance(active, dict) or not isinstance(active.get("experiment_id"), str) or not IDENTIFIER.fullmatch(active["experiment_id"]):
        raise ValueError("Invalid active experiment identity")
    duration = hours(active.get("evaluation_hours"))
    if rule_version(active) == LEGACY_RULE and duration < 2:
        raise ValueError("Legacy cadence requires at least two hours")
    if type(active.get("carry_in_unscored")) is not bool:
        raise ValueError("Carry-in status must be a strict boolean")
    if stamp(active["due_at_utc"]) != stamp(active["seed_at_utc"]) + timedelta(hours=duration):
        raise ValueError("Deadline does not match the committed seed and duration")


def classify(active, comparison):
    """Classify one native closing comparison without changing any files."""
    validate_active(active)
    duration = active["evaluation_hours"]
    if comparison.get("experiment_id") != active["experiment_id"] or comparison.get("seed_at_utc") != active["seed_at_utc"]:
        raise ValueError("Comparison belongs to a different experiment or seed")
    if comparison.get("experiment_sha256") != active.get("experiment_sha256"):
        raise ValueError("Comparison experiment provenance differs from the committed baseline")
    # No unavailable-result fallback may authorize an early rollover when the
    # committed Paper endpoint itself is missing or malformed.
    try:
        observed = stamp(comparison["paper"]["observed_at_utc"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("A valid committed Paper endpoint is required before finalizing") from error
    due = stamp(active["due_at_utc"])
    result = {"outcome": "unavailable", "finalizable": True, "winning": False,
              "ending_evaluation_hours": duration, "next_evaluation_hours": duration,
              "reason": "comparison_not_reliably_rankable"}
    try:
        if observed < due:
            return {**result, "outcome": "early", "finalizable": False,
                    "reason": "committed_paper_observation_has_not_reached_deadline"}
        if active["carry_in_unscored"]:
            return {**result, "outcome": "carry_in_unscored",
                    "reason": "experiment_predates_the_committed_duration_policy"}
        actual = comparison["actual"]
        completed = stamp(actual["completed_at_utc"])
        marks = stamp(comparison["common_marks"]["observed_at_utc"])
        if max(observed, completed, marks) > due + timedelta(seconds=LATE_SECONDS):
            return {**result, "outcome": "late_unscored",
                    "reason": "closing_observation_exceeds_five_minute_lateness_allowance"}
        if min(completed, marks) < due or completed < marks:
            raise ValueError("Actual endpoint does not cover the committed deadline")
        summary = comparison["comparison"]
        required_flags = ("performance_comparable", "mirror_baseline_verified", "zero_external_flows_verified")
        if any(summary.get(flag) is not True for flag in required_flags):
            raise ValueError("Native comparison is not fully comparable")
        if summary.get("paper_beating_actual") is not True and summary.get("paper_beating_actual") is not False:
            raise ValueError("Native winning flag is not a boolean")
        skew = abs((completed - observed).total_seconds())
        if skew > 120 or not math.isclose(number(summary["observation_skew_seconds"]), skew, abs_tol=1e-6):
            raise ValueError("Observation skew exceeds the native comparison limit")
        if set(actual["accounts"]) != ACCOUNTS or set(active["public_owner_sha256"]) != ACCOUNTS:
            raise ValueError("Account universe is incomplete")
        seed_ms = int(stamp(active["seed_at_utc"]).timestamp() * 1000)
        for account, row in actual["accounts"].items():
            flow = row["flows"]
            account_observed = stamp(row["observed_at_utc"])
            end_ms = int(account_observed.timestamp() * 1000)
            if not due <= account_observed <= completed:
                raise ValueError("Actual account observation lies outside the closing window")
            if (flow.get("range_complete") is not True or flow.get("zero_external_flows_verified") is not True
                    or type(flow.get("event_count")) is not int or flow["event_count"] != 0
                    or number(flow.get("net_external_flow_usd")) != 0
                    or type(flow.get("start_time_ms")) is not int or flow["start_time_ms"] != seed_ms
                    or type(flow.get("end_time_ms")) is not int or flow["end_time_ms"] != end_ms):
                raise ValueError("Cash-flow history is incomplete, nonempty or from another interval")
            matching = [read for read in comparison["source_reads"]
                        if read.get("account") == account and read.get("request", {}).get("type") == "userNonFundingLedgerUpdates"]
            if len(matching) != 1:
                raise ValueError("Expected one native cash-flow evidence response per account")
            evidence = matching[0]
            request = evidence["request"]
            if (evidence.get("response") != [] or evidence.get("error_type")
                    or request.get("user") != "wallet-sha256:" + active["public_owner_sha256"][account]
                    or request.get("startTime") != seed_ms or request.get("endTime") != end_ms):
                raise ValueError("Cash-flow source evidence does not match the mirrored account")
        baseline = number(comparison["paper"]["opening_equity"])
        paper_equity = number(comparison["paper"]["common_mark_equity"])
        actual_equity = number(actual["common_mark_equity"])
        edge = number(summary["common_mark_equity_edge"])
        excess = number(summary["excess_return_fraction"])
        if baseline <= 0 or not math.isclose(baseline, number(active["opening_equity"]), rel_tol=1e-11, abs_tol=1e-7):
            raise ValueError("Opening equity differs from the committed experiment")
        if (not math.isclose(edge, paper_equity - actual_equity, rel_tol=1e-11, abs_tol=1e-7)
                or not math.isclose(excess, edge / baseline, rel_tol=1e-11, abs_tol=1e-12)
                or summary["paper_beating_actual"] != (edge > 0)):
            raise ValueError("Native winning flag and common-mark arithmetic disagree")
        outcome = "win" if edge > 0 and excess > 0 else "tie" if edge == 0 else "loss"
        return {**result, "outcome": outcome, "winning": outcome == "win",
                "next_evaluation_hours": next_hours(duration, outcome, rule_version(active)),
                "reason": "verified_common_mark_account_comparison",
                "common_mark_equity_edge": edge, "excess_return_fraction": excess}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        return {**result, "reason": str(error)}


class Cadence:
    def __init__(self, root):
        self.root = safe_path(root)
        if not self.root.is_dir():
            raise ValueError("An existing datastore is required")
        self.operations = self.path(self.root / "_operations")
        self.state_path = self.path(self.operations / "paper-improvement-cadence.json")
        self.receipts = self.path(self.operations / "paper-improvement-cadence" / "assessments")
        self.amendments = self.path(self.operations / "paper-improvement-cadence" / "policy-amendments")

    def path(self, path):
        path = safe_path(path)
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Cadence inputs and evidence must remain within the datastore")
        return path

    def lock(self):
        if not self.operations.is_dir():
            raise ValueError("Native operation evidence is required before initializing cadence")
        return FileLock(str(self.path(self.operations / ".paper-cadence.lock")), timeout=0)

    def state(self):
        value = read_json(self.state_path)
        if type(value.get("schema_version")) is not int or value["schema_version"] != VERSION:
            raise ValueError("Unsupported cadence state version")
        validate_active(value["active"])
        if rule_version(value) != rule_version(value["active"]) or value.get("policy") != policy(rule_version(value)):
            raise ValueError("State, active experiment and cadence policy disagree")
        if hours(value.get("current_evaluation_hours")) != value["active"]["evaluation_hours"]:
            raise ValueError("Cadence duration and active experiment disagree")
        for amendment in value.get("policy_amendments", []):
            if file_digest(self.path(amendment["path"])) != amendment["sha256"]:
                raise ValueError("Committed policy amendment changed")
        return value

    def baseline(self):
        record = read_json(self.path(self.operations / "paper-current-accepted.json"))
        experiment = read_json(self.path(self.root / "_paper/experiment.json"))
        if record != experiment or record.get("status") != "running" or record.get("analysis_eligible") is not True:
            raise ValueError("Current experiment is not a completed accepted running baseline")
        identity = record.get("experiment_id")
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity):
            raise ValueError("Invalid accepted experiment identity")
        assert_not_excluded(self.root, record)
        evidence = self.path(record["evidence_directory"])
        if evidence != self.operations / "paper-improvement" / identity:
            raise ValueError("Accepted evidence directory does not match experiment identity")
        operation = read_json(self.path(evidence / "operation.json"))
        guard = read_json(self.path(self.operations / "paper-maintenance.json"))
        deployment = read_json(self.path(evidence / "deployment.json"))
        if (operation.get("phase") != "completed" or operation.get("cycle_id") != identity
                or guard.get("status") != "completed" or guard.get("cycle_id") != identity
                or deployment != record):
            raise ValueError("Native rollover has not completed for this baseline")
        current = seed_info(self.path(self.root / "_paper"))
        if current["seed"]["timestamp_utc"] != record["seed_at_utc"] or current["immutable_opening_hashes"] != record["immutable_opening_hashes"]:
            raise ValueError("Accepted immutable opening no longer matches the ledger")
        opening = read_json(self.path(record["opening_verification_path"]))
        if opening.get("result") != "passed" or opening.get("seed_at_utc") != record["seed_at_utc"] or opening.get("immutable_opening_hashes") != current["immutable_opening_hashes"]:
            raise ValueError("Opening verification is incomplete or belongs to another seed")
        if set(opening.get("public_owner_sha256", {})) != ACCOUNTS or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in opening["public_owner_sha256"].values()):
            raise ValueError("Verified opening account identities are incomplete")
        recipe = {}
        for name in ("hyperliquid-markets.json", "hyperliquid-models.json", "hyperliquid-paper.json"):
            source = self.path(evidence / "accepted-source/configs" / name)
            if file_digest(source) != record["config_sha256"][name]:
                raise ValueError("Accepted recipe provenance changed")
            recipe[name] = read_json(source)
        return record, {"config_sha256": {name: record["config_sha256"][name] for name in recipe},
                        "interval": recipe["hyperliquid-markets.json"]["interval"],
                        "symbols": recipe["hyperliquid-markets.json"]["symbols"],
                        "horizons_bars": recipe["hyperliquid-models.json"]["horizons_bars"],
                        "paper_policy_id": record["opening_policy_id"]}, opening["public_owner_sha256"]

    def active(self, record, recipe, owners, duration, *, carry_in, version=CURRENT_RULE):
        duration = hours(duration)
        active = {"experiment_id": record["experiment_id"], "seed_at_utc": record["seed_at_utc"],
                "experiment_sha256": file_digest(self.path(self.root / "_paper/experiment.json")),
                "evaluation_hours": duration,
                "due_at_utc": (stamp(record["seed_at_utc"]) + timedelta(hours=duration)).isoformat(),
                "carry_in_unscored": carry_in, "opening_equity": number(record["opening_equity"]),
                "immutable_opening_hashes": record["immutable_opening_hashes"],
                "public_owner_sha256": owners, "recipe": recipe,
                "baseline_evidence_directory": record["evidence_directory"]}
        # Missing version means legacy in immutable historical receipts.
        if version != LEGACY_RULE:
            active["cadence_rule_version"] = version
        validate_active(active)
        return active

    def status(self):
        state = self.state()
        pending = state.get("pending_assessment")
        planned_hours = None
        if pending is not None:
            receipt_path = self.path(pending["path"])
            if file_digest(receipt_path) != pending["sha256"]:
                raise ValueError("Pending assessment changed")
            planned_hours = hours(read_json(receipt_path)["decision"]["next_evaluation_hours"])
        return {"state_path": str(self.state_path), "cadence_rule_version": rule_version(state),
                "current_evaluation_hours": state["current_evaluation_hours"],
                "active": state["active"], "pending_assessment": state.get("pending_assessment"),
                "policy_amendments": state.get("policy_amendments", []),
                "next_due_at_utc": state["active"]["due_at_utc"],
                "planned_successor_evaluation_hours": planned_hours,
                "schedule_reanchor_deadline_at_utc": (stamp(state["active"]["seed_at_utc"]) + timedelta(seconds=180)).isoformat(),
                "schedule_action": "Bind the app wake to next_due_at_utc after rollover; confirm with the app tool. This helper never edits schedules."}

    def init(self):
        with self.lock():
            record, recipe, owners = self.baseline()
            if self.state_path.exists():
                state = self.state()
                if state["active"]["experiment_id"] != record["experiment_id"] or state["active"]["seed_at_utc"] != record["seed_at_utc"]:
                    raise ValueError("Existing cadence belongs to another run; use advance after its assessment")
                return self.status()
            value = {"schema_version": VERSION, "initialized_at_utc": utc(),
                     "cadence_rule_version": CURRENT_RULE, "policy": policy(CURRENT_RULE),
                     "current_evaluation_hours": 2,
                     "active": self.active(record, recipe, owners, 2, carry_in=True),
                     "pending_assessment": None, "history": []}
            write_json(self.state_path, value)
            return self.status()

    def assess(self, comparison_path):
        with self.lock():
            state = self.state()
            path = self.path(comparison_path)
            comparison_bytes = path.read_bytes()
            comparison = json.loads(comparison_bytes)
            sha = hashlib.sha256(comparison_bytes).hexdigest()
            decision = classify(state["active"], comparison)
            if not decision["finalizable"]:
                return {"decision": decision, **self.status()}
            receipt_path = self.path(self.receipts / (state["active"]["experiment_id"] + ".json"))
            self.receipts.mkdir(parents=True, exist_ok=True)
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                if receipt["comparison_sha256"] != sha or receipt["ending_state"] != state["active"] or receipt["decision"] != decision:
                    raise ValueError("Ending experiment already has a different immutable assessment")
            else:
                receipt = {"schema_version": VERSION, "assessed_at_utc": utc(),
                           "comparison_path": str(path), "comparison_sha256": sha,
                           "ending_state": deepcopy(state["active"]), "ending_state_sha256": digest(state["active"]),
                           "decision": decision}
                write_json(receipt_path, receipt, exclusive=True)
            pending = {"path": str(receipt_path), "sha256": file_digest(receipt_path)}
            if state.get("pending_assessment") not in (None, pending):
                raise ValueError("Another assessment is already pending")
            state["pending_assessment"] = pending
            write_json(self.state_path, state)
            return {"decision": decision, **self.status()}

    def advance(self):
        with self.lock():
            state = self.state()
            record, recipe, owners = self.baseline()
            pending = state.get("pending_assessment")
            if pending is None:
                if (state["history"] and state["active"]["experiment_id"] == record["experiment_id"]
                        and state["active"]["seed_at_utc"] == record["seed_at_utc"]):
                    return self.status()
                raise ValueError("A finalized assessment is required before advance")
            path = self.path(pending["path"])
            if file_digest(path) != pending["sha256"]:
                raise ValueError("Pending assessment changed")
            receipt = read_json(path)
            if receipt["ending_state"] != state["active"] or receipt["ending_state_sha256"] != digest(state["active"]):
                raise ValueError("Assessment no longer matches the ending experiment")
            if file_digest(self.path(receipt["comparison_path"])) != receipt["comparison_sha256"]:
                raise ValueError("Assessed comparison changed")
            ending_id = state["active"]["experiment_id"]
            if record["experiment_id"] == ending_id or record.get("superseded_experiment_id") != ending_id:
                raise ValueError("Completed replacement must directly supersede the assessed experiment")
            if stamp(record["seed_at_utc"]) <= stamp(state["active"]["seed_at_utc"]):
                raise ValueError("Replacement opening must be newer than the ending experiment")
            duration = hours(receipt["decision"]["next_evaluation_hours"])
            if classify(receipt["ending_state"], read_json(self.path(receipt["comparison_path"]))) != receipt["decision"]:
                raise ValueError("Assessment decision does not match its committed rule and comparison")
            active = self.active(record, recipe, owners, duration, carry_in=False, version=rule_version(state))
            state["history"].append({"ending_experiment_id": ending_id,
                                     "successor_experiment_id": active["experiment_id"],
                                     "assessment": pending, "decision": receipt["decision"], "advanced_at_utc": utc()})
            state.update(active=active, current_evaluation_hours=duration, pending_assessment=None)
            write_json(self.state_path, state)
            return self.status()

    def adopt_loss_penalty(self, change_id, expected_ending_id, expected_active_id):
        """Apply the latest consumed legacy loss once, retaining its original score.

        This explicit policy amendment shortens the existing accepted successor;
        it never creates an experiment, changes workers, or edits old receipts.
        A receipt-first interrupted commit is recovered using the same change ID.
        """
        if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
               for value in (change_id, expected_ending_id, expected_active_id)):
            raise ValueError("Valid change, ending and active identities are required")
        with self.lock(), FileLock(str(self.path(self.operations / ".paper-review.lock")), timeout=0):
            state = self.state()
            if state.get("pending_assessment") is not None:
                raise ValueError("Cannot amend a round with a pending assessment")
            record, recipe, owners = self.baseline()
            if (state["active"]["experiment_id"] != expected_active_id
                    or record["experiment_id"] != expected_active_id
                    or record.get("superseded_experiment_id") != expected_ending_id):
                raise ValueError("Expected identities do not match the accepted successor")
            expected_active = self.active(record, recipe, owners, state["current_evaluation_hours"],
                                          carry_in=False, version=rule_version(state))
            if state["active"] != expected_active:
                raise ValueError("Active cadence differs from the accepted baseline")
            if not state.get("history"):
                raise ValueError("A consumed ending assessment is required")
            latest = state["history"][-1]
            if (latest["ending_experiment_id"] != expected_ending_id
                    or latest["successor_experiment_id"] != expected_active_id):
                raise ValueError("Latest consumed assessment has different identities")
            assert_not_excluded(self.root, {"experiment_id": expected_ending_id})
            assessed_path = self.path(latest["assessment"]["path"])
            if file_digest(assessed_path) != latest["assessment"]["sha256"]:
                raise ValueError("Consumed assessment changed")
            assessed = read_json(assessed_path)
            ending = assessed["ending_state"]
            if (ending["experiment_id"] != expected_ending_id
                    or assessed["ending_state_sha256"] != digest(ending)
                    or latest["decision"] != assessed["decision"]):
                raise ValueError("Consumed assessment does not match retained history")
            comparison_path = self.path(assessed["comparison_path"])
            if file_digest(comparison_path) != assessed["comparison_sha256"]:
                raise ValueError("Assessed comparison changed")
            if (rule_version(ending) != LEGACY_RULE
                    or classify(ending, read_json(comparison_path)) != assessed["decision"]
                    or assessed["decision"]["outcome"] != "loss"):
                raise ValueError("Amendment requires a verified consumed legacy LOSS")
            receipt_path = self.path(self.amendments / (change_id + ".json"))
            committed = state.get("policy_amendments", [])
            if rule_version(state) == CURRENT_RULE:
                if len(committed) != 1 or committed[0]["path"] != str(receipt_path):
                    raise ValueError("Loss penalty was already adopted with another change ID")
                receipt = read_json(receipt_path)
                expected_after = deepcopy(receipt["before_state"])
                expected_after.update(active=receipt["after_active"],
                                      current_evaluation_hours=receipt["after_active"]["evaluation_hours"],
                                      cadence_rule_version=CURRENT_RULE, policy=policy(CURRENT_RULE),
                                      policy_amendments=committed)
                if (receipt["change_id"] != change_id or receipt["ending_experiment_id"] != expected_ending_id
                        or receipt["active_experiment_id"] != expected_active_id or expected_after != state):
                    raise ValueError("Committed amendment does not match the active state")
                return {"policy_amendment": committed[0], **self.status()}
            if committed:
                raise ValueError("Legacy state has unexpected policy amendments")
            if state["current_evaluation_hours"] != assessed["decision"]["next_evaluation_hours"]:
                raise ValueError("Current duration differs from the consumed legacy decision")
            duration = next_hours(ending["evaluation_hours"], "loss", CURRENT_RULE)
            active = self.active(record, recipe, owners, duration, carry_in=False, version=CURRENT_RULE)
            now = utc()
            if stamp(now) >= stamp(active["due_at_utc"]):
                raise ValueError("Shortened deadline is already due; cannot amend retrospectively")
            receipt = {"schema_version": VERSION, "change_id": change_id,
                       "ending_experiment_id": expected_ending_id, "active_experiment_id": expected_active_id,
                       "from_rule_version": LEGACY_RULE, "to_rule_version": CURRENT_RULE,
                       "amended_at_utc": now, "reason": "user_authorized_latest_loss_penalty",
                       "consumed_assessment": deepcopy(latest["assessment"]),
                       "comparison_path": str(comparison_path), "comparison_sha256": assessed["comparison_sha256"],
                       "before_state": deepcopy(state), "before_state_sha256": file_digest(self.state_path),
                       "after_active": active}
            self.amendments.mkdir(parents=True, exist_ok=True)
            if receipt_path.exists():
                retained = read_json(receipt_path)
                receipt["amended_at_utc"] = retained.get("amended_at_utc")
                if (retained != receipt or stamp(receipt["amended_at_utc"]) > stamp(now)
                        or stamp(receipt["amended_at_utc"]) >= stamp(active["due_at_utc"])):
                    raise ValueError("Existing policy amendment does not match this transition")
            else:
                write_json(receipt_path, receipt, exclusive=True)
            reference = {"path": str(receipt_path), "sha256": file_digest(receipt_path)}
            state.update(active=active, current_evaluation_hours=duration,
                         cadence_rule_version=CURRENT_RULE, policy=policy(CURRENT_RULE),
                         policy_amendments=[reference])
            write_json(self.state_path, state)
            return {"policy_amendment": reference, **self.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "status", "assess", "advance", "adopt-loss-penalty"))
    parser.add_argument("--root", type=Path, default=Path("C:/DATASTORE/hyperliquid"))
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--change-id")
    parser.add_argument("--expected-ending-id")
    parser.add_argument("--expected-active-id")
    args = parser.parse_args(argv)
    if args.command == "assess" and args.comparison is None:
        parser.error("assess requires --comparison")
    cadence = Cadence(args.root)
    if args.command == "adopt-loss-penalty":
        if not all((args.change_id, args.expected_ending_id, args.expected_active_id)):
            parser.error("adopt-loss-penalty requires --change-id, --expected-ending-id and --expected-active-id")
        result = cadence.adopt_loss_penalty(args.change_id, args.expected_ending_id, args.expected_active_id)
    else:
        result = cadence.assess(args.comparison) if args.command == "assess" else getattr(cadence, args.command)()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
