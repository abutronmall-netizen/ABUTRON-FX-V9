#!/usr/bin/env python3
# Migration export: CRM connection IDs are supplied by per-account environment variables.
"""FP Markets DEMO 1622/2582 independent V9 model inference; NEVER sends orders.

Uses the proven, pinned 49-feature 0512/1636 read-only implementation with
independent account-binding and output. Fail-closed broker-native validation.
"""
import importlib.util
from pathlib import Path

BASE = Path("/usr/local/sbin/abutron-v9-fp-demo-native-inference-ro")
OUTPUT = Path("/var/lib/abutron-v9-brain/decisions/fp-demo-other-accounts-latest.json")
ACCOUNTS = {
    "1622": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1622"],
    "2582": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_2582"],
}

if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location(
        "abutron_fp_demo_pinned_readonly", BASE
    )
    # Source without .py suffix needs SourceFileLoader explicitly.
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("abutron_fp_demo_pinned_readonly", str(BASE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise SystemExit("FP_V9_READONLY_IMPLEMENTATION_UNAVAILABLE")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.ACCOUNTS == {
        "0512": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_0512"],
        "1636": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1636"],
    }, "FP_V9_ORIGINAL_ACCOUNT_MAPPING_CHANGED"
    mod.ACCOUNTS = dict(ACCOUNTS)
    result = mod.infer()
    assert result["orders_submitted"] == 0
    assert result["native_order_dispatch"] == "NOT_CONNECTED"
    assert set(result["accounts"]) == set(ACCOUNTS)
    assert all(
        row["connection_id"] == ACCOUNTS[suffix]
        and row["environment"] == "DEMO"
        and row["broker"] == "FP Markets"
        and all(symbol["features_count"] == 49
                and symbol["order_authority"] is False
                for symbol in row["symbols"])
        for suffix, row in result["accounts"].items()
    ), "ACCOUNT_SCOPE_OR_MODEL_INFERENCE_INVALID"
    mod.write_atomic(result, OUTPUT)
    print(
        "V9_FP_SECONDARY_DEMO_INFERENCE",
        {k: {"symbols_current": v["symbols_current"],
             "certified": v["scope_certified"],
             "missing": v["symbols_missing"]}
         for k, v in result["accounts"].items()},
        "ORDERS_ZERO",
    )
