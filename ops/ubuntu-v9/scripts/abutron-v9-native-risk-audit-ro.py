#!/usr/bin/env python3
"""ABUTRON V9 broker-native risk evidence audit — NO ORDER AUTHORITY.

Continuously measures independently attested owner and account risk facts.
It does not enable orders, alter account controls, generate simulated prices,
or infer that a model decision can safely execute.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import stat
import tempfile
import time

OUT = pathlib.Path("/var/lib/abutron-v9-native-risk-audit/latest.json")
OWNER = pathlib.Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")
FP = {
    "0512": pathlib.Path("/var/lib/abutron-fp-execution/reconciliation/latest.json"),
    "1636": pathlib.Path("/var/lib/abutron-fp-execution/1636/reconciliation/latest.json"),
}
V9 = pathlib.Path("/var/lib/abutron-v9-brain/decisions/fp-demo-authoritative-latest.json")
V9_SECONDARY = pathlib.Path("/var/lib/abutron-v9-brain/decisions/fp-demo-other-accounts-latest.json")
OPERATOR = pathlib.Path("/var/lib/apex/operator-live/native-status.json")
TRAILING = pathlib.Path("/var/lib/abutron-native-trailing-proof/last.json")
NATIVE_DEMO_PREFLIGHT = pathlib.Path("/var/lib/abutron-v9-native-risk-audit/evidence-progress-latest.json")
EXPECTED_OWNER_CAP_PCT = 20.0
MAX_AGE = 240.0


def trusted_json(path, limit=140000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or
                s.st_mode & 0o022 or not 10 < s.st_size < limit):
            raise ValueError("UNTRUSTED_SOURCE_FILE")
        data = os.read(fd, limit)
        after = os.fstat(fd)
        if len(data) != s.st_size or (after.st_size, after.st_mtime_ns, after.st_ino) != (
                s.st_size, s.st_mtime_ns, s.st_ino):
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("JSON_NOT_OBJECT")
        return result
    finally:
        os.close(fd)


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("MISSING_SOURCE_UTC")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("UNZONED_SOURCE_TIME")
    age = time.time() - t.timestamp()
    if not -10 <= age <= MAX_AGE:
        raise ValueError("SOURCE_TIMESTAMP_STALE")
    return round(age, 1)


def strict_money(v):
    if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
        raise ValueError("INVALID_NATIVE_MONEY")
    return round(float(v), 2)


def verify_fp(suffix):
    p = trusted_json(FP[suffix])
    parse_time(p.get("generated_at_utc"))
    if (p.get("schema") is None or p.get("broker") != "FP Markets"
            or p.get("environment") != "DEMO"
            or p.get("account_masked") != "••••" + suffix):
        raise ValueError("FP_NATIVE_ACCOUNT_SCOPE_MISMATCH")
    if p.get("status") != "RECONCILED_READONLY":
        raise ValueError("FP_NATIVE_RECONCILIATION_NOT_CURRENT")
    if p.get("native_broker_open_risk_verified") is not True:
        raise ValueError("FP_BROKER_ORDER_CALC_PROFIT_UNVERIFIED")
    if type(p.get("broker_open_positions")) is not int or p["broker_open_positions"] < 0:
        raise ValueError("FP_NATIVE_POSITION_COUNT_INVALID")
    loss = strict_money(p.get("verified_open_gross_stop_risk_usd"))
    equity = p.get("broker_equity_usd")
    if type(equity) not in (float, int) or not math.isfinite(equity) or equity <= 0:
        raise ValueError("FP_NATIVE_EQUITY_INVALID")
    if loss > equity * 0.20:
        raise ValueError("FP_OPEN_LOSS_EXCEEDS_20_PERCENT")
    return {
        "account": suffix,
        "broker": "FP Markets",
        "mode": "DEMO",
        "broker_native_existing_position_risk_proven": True,
        "broker_positions": p["broker_open_positions"],
        "existing_position_usd_stop_risk": loss,
        "equity_usd": round(equity, 2),
        "current_stop_risk_pct_of_equity": round(100 * loss / equity, 4),
        "broker_sized_new_entry_verified": False,
        "broker_sized_new_entry_blocker": "NO_FRESH_ORDER_CALC_PROFIT_AT_PROPOSED_SNIPER_ENTRY_AND_COMPLETE_OWNER_HEADROOM",
    }


def build():
    report = {
        "schema": "abutron.v9.native_risk_preflight.readonly.v1",
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "NOT_CERTIFIED_FOR_NEW_ORDERS",
        "native_orders_sent": 0,
        "native_stop_modifications_sent": 0,
        "crm_changed": False,
        "live_trading_changed": False,
        "new_entry_authorized_by_this_audit": False,
        "new_entry_broker_native_lot_sizing": "NOT_YET_CERTIFIED",
        "trailing_stop_broker_modification": "NOT_YET_CERTIFIED",
        "accounts": {},
        "blockers": [],
    }
    blockers = report["blockers"]
    try:
        owner = trusted_json(OWNER)
        parse_time(owner.get("checked_utc"))
        report["owner_risk_report_status"] = owner.get("status")
        report["linked_accounts_in_owner_inventory"] = owner.get("linked_accounts")
        # Surface the signed DEMO 6207 proof without converting evidence
        # into any owner-risk or new-order certification.
        report["deriv_6207_signed_exposure_probe"] = {
            "status":"NOT_VERIFIED", "trading_authorized":False}
        owner_groups = owner.get("owners", {})
        if isinstance(owner_groups, dict):
            for group in owner_groups.values():
                if not isinstance(group, dict):
                    continue
                for connection in group.get("connections", []):
                    if (not isinstance(connection, dict)
                        or connection.get("broker") != "Deriv"
                        or connection.get("environment") != "DEMO"
                        or connection.get("account_masked") != "••••6207"):
                        continue
                    probe = connection.get("snapshot", {}).get(
                        "issue38_signed_exposure_probe", {})
                    if (isinstance(probe, dict)
                        and probe.get("status") == "SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY"
                        and probe.get("signature_verified") is True
                        and probe.get("dual_native_crosscheck_verified") is True
                        and probe.get("trading_authorized") is False
                        and probe.get("customer_wide_risk_certified") is False):
                        report["deriv_6207_signed_exposure_probe"] = {
                            "status":probe["status"],
                            "signature_verified":True,
                            "dual_native_crosscheck_verified":True,
                            "signed_snapshot_sha256":probe.get("signed_snapshot_sha256"),
                            "checked_utc":probe.get("checked_utc"),
                            "trading_authorized":False,
                            "customer_wide_risk_certified":False}
        # The owner publisher sets this aggregate to null during an
        # authentic source rollover. Preserve separate policy and account
        # evidence instead of suppressing the entire owner report.
        native_total = owner.get("native_current_open_stop_risk_usd")
        try:
            report["owner_native_open_risk_usd_observed"] = strict_money(native_total)
        except ValueError:
            report["owner_native_open_risk_usd_observed"] = None
            blockers.append("OWNER_CURRENT_NATIVE_STOP_RISK_NOT_VERIFIED")
        report["owner_exposure_certified"] = owner.get("customer_wide_risk_certified") is True
        report["owner_exposure_unknown_sets"] = owner.get("unknown_connected_position_sets")
        policy = owner.get("risk_caps_policy") or {}
        configured_cap = policy.get("aggregate_open_pct")
        configured_drawdown = policy.get("max_drawdown_pct")
        report["owner_ceiling_expected_pct"] = EXPECTED_OWNER_CAP_PCT
        report["configured_aggregate_open_pct"] = configured_cap
        report["configured_max_drawdown_pct"] = configured_drawdown
        # A stricter 2% aggregate *open stop-risk* cap does not contradict
        # the separately configured 20% owner drawdown ceiling. Never
        # loosen either cap automatically or conflate the two measures.
        report["owner_cap_policy_within_authorized_ceiling"] = (
            type(configured_cap) in (int, float)
            and math.isfinite(configured_cap)
            and 0 < configured_cap <= EXPECTED_OWNER_CAP_PCT
            and type(configured_drawdown) in (int, float)
            and math.isfinite(configured_drawdown)
            and 0 < configured_drawdown <= EXPECTED_OWNER_CAP_PCT
        )
        if not report["owner_cap_policy_within_authorized_ceiling"]:
            blockers.append("OWNER_RISK_POLICY_EXCEEDS_OR_LACKS_APPROVED_CEILING")
        if not report["owner_exposure_certified"]:
            blockers.append("OWNER_EXPOSURE_OR_DAILY_EQUITY_HISTORY_NOT_CERTIFIED")
        for value in owner.get("blockers", []):
            if isinstance(value, str) and len(value) <= 96:
                blockers.append(value)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as err:
        report["owner_exposure_certified"] = False
        blockers.append("OWNER_NATIVE_EVIDENCE_UNAVAILABLE")

    fp_sum = 0.0
    fp_verified = 0
    for suffix in FP:
        try:
            current = verify_fp(suffix)
            report["accounts"][suffix] = current
            fp_sum += current["existing_position_usd_stop_risk"]
            fp_verified += 1
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as err:
            report["accounts"][suffix] = {
                "account": suffix,
                "broker": "FP Markets", "mode": "DEMO",
                "broker_native_existing_position_risk_proven": False,
                "reason": str(err)[:96] if isinstance(err, ValueError) else "NATIVE_SOURCE_UNAVAILABLE",
                "broker_sized_new_entry_verified": False,
            }
            blockers.append("FP_" + suffix + "_NATIVE_STOP_RISK_NOT_CURRENT")

    report["fp_two_account_native_position_risk_usd"] = round(fp_sum, 2)
    report["fp_two_account_broker_risk_verified"] = fp_verified == 2
    try:
        v9 = trusted_json(V9)
        parse_time(v9.get("generated_utc"))
        model_accounts = v9.get("accounts") or {}
        report["fp_v9_49feature_13of13"] = (
            v9.get("model_features") == 49 and
            v9.get("orders_submitted") == 0 and
            all(model_accounts.get(k, {}).get("scope_certified") is True
                and model_accounts[k].get("symbols_current") == 13 for k in FP)
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report["fp_v9_49feature_13of13"] = False
    try:
        extra = trusted_json(V9_SECONDARY)
        parse_time(extra.get("generated_utc"))
        extra_accounts = extra.get("accounts") or {}
        report["fp_other_demo_inference_accounts"] = {
            k: {"symbols_current": v.get("symbols_current"),
                "scope_certified": v.get("scope_certified") is True}
            for k, v in extra_accounts.items()
            if k in ("1622", "2582") and isinstance(v, dict)
        }
        report["fp_four_demo_v9_model_13of13"] = (
            report.get("fp_v9_49feature_13of13") is True
            and extra.get("model_features") == 49
            and extra.get("model_sha256") == trusted_json(V9).get("model_sha256")
            and extra.get("orders_submitted") == 0
            and set(extra_accounts) == {"1622", "2582"}
            and all(extra_accounts[k].get("scope_certified") is True
                    and extra_accounts[k].get("symbols_current") == 13
                    and extra_accounts[k].get("environment") == "DEMO"
                    and extra_accounts[k].get("broker") == "FP Markets"
                    for k in ("1622", "2582"))
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report["fp_four_demo_v9_model_13of13"] = False
        report["fp_other_demo_inference_accounts"] = {}
    if not report["fp_four_demo_v9_model_13of13"]:
        blockers.append("FP_FOUR_DEMO_V9_AUTHENTICATED_INFERENCE_NOT_CURRENT")
    try:
        op = trusted_json(OPERATOR)
        parse_time(op.get("checked_at_utc"))
        audit = op.get("account_execution") or {}
        report["six_demo_fresh_broker_fills_proven"] = audit.get(
            "accounts_with_recent_broker_fills", 0) > 0
        report["all_demo_execution_proven"] = audit.get(
            "all_demo_accounts_fully_operational_verified") is True
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report["six_demo_fresh_broker_fills_proven"] = False
        report["all_demo_execution_proven"] = False

    # Preserve and present historical broker-native no-send preflights.
    # These are not current OrderCheck authorizations, fills, or sizing
    # certificates. NEVER remove a blocker based on this diagnostic.
    report["deriv_6207_historical_native_preflight"] = {
        "status":"NOT_CURRENT_OR_UNVERIFIED",
        "historical_only":True,
        "current_order_authority":False,
        "exact_sniper_entry_sizing_certified":False,
        "broker_deal_ack_certified":False}
    try:
        proof = trusted_json(NATIVE_DEMO_PREFLIGHT)
        parse_time(proof.get("checked_at_utc"))
        if (proof.get("schema") != "abutron.v9.independent_demo6207_preflight_progress.readonly.v1"
            or proof.get("account_masked") != "••••6207"
            or proof.get("broker") != "Deriv"
            or proof.get("environment") != "DEMO"
            or proof.get("historical_only", True) is not True
            or proof.get("status") != "HISTORICAL_NATIVE_NO_SEND_PREFLIGHT_RETAINED"
            or proof.get("historical_originals_integrity_complete") is not True
            or type(proof.get("historical_native_no_send_receipts_verified")) is not int
            or not 1 <= proof["historical_native_no_send_receipts_verified"] <= 10000
            or proof.get("current_ordercheck_authority") is not False
            or proof.get("exact_sniper_entry_sizing_certified") is not False
            or proof.get("new_demo_deal_broker_ack_certified") is not False
            or proof.get("trailing_stop_modification_broker_ack_certified") is not False
            or proof.get("orders_submitted") != 0
            or proof.get("live_modified") is not False
            or proof.get("crm_modified") is not False
            or proof.get("unverified_codes") != []):
            raise ValueError("DEMO_PREFLIGHT_NOT_FULLY_VERIFIED")
        report["deriv_6207_historical_native_preflight"] = {
            "status":"HISTORICAL_NATIVE_NO_SEND_PREFLIGHT_RETAINED",
            "historical_only":True,
            "verified_original_receipts":proof["historical_native_no_send_receipts_verified"],
            "symbols":proof.get("historical_native_no_send_symbols",[]),
            "latest_original_preflight_utc":proof.get("last_historical_preflight_utc"),
            "v100_one_lot_native_probe_only":proof.get("native_v100_one_lot_probe_verified") is True,
            "five_broker_minimum_volume_cost_probes_verified":(
                proof.get("native_minimum_volume_broker_costs_verified") is True
                and isinstance(proof.get("native_minimum_volume_by_symbol"),dict)
                and len(proof["native_minimum_volume_by_symbol"])==5
                and all(
                    isinstance(v,dict)
                    and v.get("native_broker_cost_observed_only") is True
                    and v.get("exact_new_order_sizing_certified") is False
                    for v in proof["native_minimum_volume_by_symbol"].values())),
            "observed_broker_minimum_volume_costs":
                proof.get("native_minimum_volume_by_symbol",{}),
            "minimum_volume_costs_are_not_exact_sniper_order_risk":True,
            "current_order_authority":False,
            "exact_sniper_entry_sizing_certified":False,
            "broker_deal_ack_certified":False,
            "trailing_broker_ack_certified":False}
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        pass

    # Protective SL/TP being present is NOT proof that a Chandelier
    # trailing-stop amendment was broker accepted.
    try:
        stops = trusted_json(TRAILING)
        if stops.get("state") == "SIX_DEMO_NATIVE_STOPS_VERIFIED":
            report["six_demo_native_protective_stops_proven"] = True
        else:
            report["six_demo_native_protective_stops_proven"] = False
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report["six_demo_native_protective_stops_proven"] = False
    if not report["all_demo_execution_proven"]:
        blockers.append("NO_NEW_COMPLETE_DEMO_NATIVE_ORDER_LIFECYCLE_PROOF")
    if not report["fp_two_account_broker_risk_verified"]:
        blockers.append("FP_0512_1636_OPEN_STOP_RISK_PENDING")
    if not report["fp_v9_49feature_13of13"]:
        blockers.append("FP_V9_FULL_MODEL_COVERAGE_PENDING")
    blockers.append("NO_CERTIFIED_ONE_LOT_ORDER_CALC_PROFIT_FOR_NEW_SNIPER_ENTRIES")
    blockers.append("NO_BROKER_CONFIRMED_NEW_CHANDELIER_STOP_MODIFICATION")
    report["blockers"] = sorted(set(blockers))
    return report


def main():
    report = build()
    OUT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(prefix=".native-risk-", dir=OUT.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(path, OUT)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    print("ABUTRON_NATIVE_RISK_AUDIT",report["status"],
          "FP_BROKER_RISK_ACCOUNTS",sum(
              x.get("broker_native_existing_position_risk_proven", False)
              for x in report["accounts"].values()),
          "OWNER_VERIFIED",report.get("owner_exposure_certified", False),
          "TRADE_ORDERS",0,flush=True)


if __name__ == "__main__":
    main()
