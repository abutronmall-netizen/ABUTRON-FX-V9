#!/usr/bin/env python3
"""Offline/read-only check of a sanitized ABUTRON owner evidence summary.

This diagnostic does NOT change server configuration, CRM flags, risk policy,
MT5 attachments, order permissions, or broker evidence. Missing/ambiguous
historical source data is not equivalent to a certificate.

Expected input is an already-sanitized owner-evidence-auto-status JSON file;
NEVER pass original broker statements into a public CI job.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class ContractError(ValueError):
    """Malformed or untrusted report format."""


def evaluate_evidence(report: dict) -> dict:
    """Pure analysis. No side effects or implied grant of execution authority."""
    if not isinstance(report, dict):
        raise ContractError("REPORT_NOT_OBJECT")
    if report.get("schema") != "abutron.owner_evidence_automated_status.ro.v1":
        raise ContractError("REPORT_SCHEMA_UNEXPECTED")

    health = report.get("source_health")
    if not isinstance(health, dict) or not health:
        raise ContractError("SOURCE_HEALTH_UNVERIFIED")
    healthy = all(state == "FRESH_TRUSTED" for state in health.values())

    history = report.get("lifetime_history") or {}
    equity = report.get("historical_equity") or {}
    acks = report.get("broker_ack") or {}
    if not all(isinstance(x, dict) for x in (history, equity, acks)):
        raise ContractError("EVIDENCE_SECTION_UNTRUSTED")

    rows = report.get("per_account_evidence")
    if not isinstance(rows, list) or not rows:
        raise ContractError("ACCOUNT_INVENTORY_MISSING")

    # Account inventory must remain visible even if some native sources go stale.
    masked = [a.get("account_masked") for a in rows if isinstance(a, dict)]
    if len(masked) != len(rows) or len(set(masked)) != len(masked):
        raise ContractError("ACCOUNT_INVENTORY_NOT_UNIQUE")

    reasons: list[str] = []
    if not healthy:
        reasons.append("EVIDENCE_SOURCE_MISSING_OR_STALE")
    if equity.get("full_daystart_equity_certified") is not True:
        reasons.append("EXACT_BROKER_DAYSTART_EQUITY_NOT_CERTIFIED")
    if equity.get("true_historical_equity_peak_certified") is not True:
        reasons.append("TRUE_UNOBSERVED_EQUITY_PEAK_NOT_CERTIFIED")
    if history.get("full_broker_lifetime_history_certified") is not True:
        reasons.append("INCEPTION_TO_NOW_BROKER_LIFETIME_NOT_CERTIFIED")
    if acks.get("complete_lifetime_ack_certification") is not True:
        reasons.append("LIFETIME_BROKER_ACK_OR_EA_ORIGINAL_NOT_CERTIFIED")

    # Account-level evidence cannot be overridden by an aggregate TRUE.
    account_fields = (
        "true_historical_daystart_equity_certified",
        "true_intraminute_equity_peak_certified",
        "full_broker_lifetime_history_certified",
        "complete_original_acks_certified",
    )
    missing_per_account = {
        field: sum(a.get(field) is not True for a in rows)
        for field in account_fields
    }
    if any(missing_per_account.values()):
        reasons.append("ONE_OR_MORE_ACCOUNT_HISTORICAL_PROOFS_MISSING")

    # An epoch-zero query, a sampled high-water, and broker-matched ACKs are
    # important evidence, but cannot substitute for original lifetime proof.
    return {
        "historical_certification_complete": not reasons,
        "current_native_account_sets_observed": report.get("native_account_sets"),
        "account_rows": len(rows),
        "all_source_summaries_current": healthy,
        "broker_epoch_zero_query_observed": (
            history.get("live_9392_full_range_historyselect_zero_proven") is True
        ),
        "sampled_equity_is_exact_historical_peak": False,
        "missing_per_account_certifications": missing_per_account,
        "outstanding_evidence_codes": sorted(set(reasons)),
        "new_order_authorization_granted": False,
        "live_trading_modified": False,
    }


def selftest() -> None:
    def account(suffix: str, all_true: bool = False) -> dict:
        return {
            "account_masked": "••••" + suffix,
            "true_historical_daystart_equity_certified": all_true,
            "true_intraminute_equity_peak_certified": all_true,
            "full_broker_lifetime_history_certified": all_true,
            "complete_original_acks_certified": all_true,
        }

    sample = {
        "schema": "abutron.owner_evidence_automated_status.ro.v1",
        "source_health": {"native_history": "FRESH_TRUSTED"},
        "native_account_sets": 8,
        "per_account_evidence": [account(str(1000 + i)) for i in range(8)],
        "historical_equity": {
            "full_daystart_equity_certified": False,
            "true_historical_equity_peak_certified": False,
        },
        "lifetime_history": {
            "live_9392_full_range_historyselect_zero_proven": True,
            "full_broker_lifetime_history_certified": False,
        },
        "broker_ack": {"complete_lifetime_ack_certification": False},
    }
    v = evaluate_evidence(sample)
    assert not v["historical_certification_complete"]
    assert v["broker_epoch_zero_query_observed"] is True
    assert v["sampled_equity_is_exact_historical_peak"] is False
    assert len(v["outstanding_evidence_codes"]) >= 4

    sample["historical_equity"] = {
        "full_daystart_equity_certified": True,
        "true_historical_equity_peak_certified": True,
    }
    sample["lifetime_history"]["full_broker_lifetime_history_certified"] = True
    sample["broker_ack"]["complete_lifetime_ack_certification"] = True
    assert not evaluate_evidence(sample)["historical_certification_complete"]

    sample["per_account_evidence"] = [account(str(1000+i), True) for i in range(8)]
    assert evaluate_evidence(sample)["historical_certification_complete"]
    sample["source_health"]["native_history"] = "UNVERIFIED_ValueError"
    assert not evaluate_evidence(sample)["historical_certification_complete"]
    try:
        evaluate_evidence({"schema": "unknown", "source_health": {"x": "FRESH_TRUSTED"}})
    except ContractError:
        pass
    else:
        raise AssertionError("Unknown report schema accepted")
    print("evidence-contract selftest: PASS (no missing source can silently certify)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Path to sanitized owner evidence JSON")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0
    if not args.report:
        parser.error("Specify --report or --selftest")
    try:
        if args.report.is_symlink() or not args.report.is_file():
            raise ContractError("EVIDENCE_REPORT_NOT_REGULAR_FILE")
        if args.report.stat().st_size > 2_000_000:
            raise ContractError("EVIDENCE_REPORT_TOO_LARGE")
        report = json.loads(args.report.read_text(encoding="utf-8"))
        result = evaluate_evidence(report)
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({"status": "EVIDENCE_UNTRUSTED", "error": type(exc).__name__}))
        return 3

    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["historical_certification_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
