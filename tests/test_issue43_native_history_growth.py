"""Issue #43 — only the three native history reads grow; risk bounds remain."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "ops/issue43/native_history_growth.py"
spec = importlib.util.spec_from_file_location("fp_issue43_native_history_growth", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

OLD_SHA = "a" * 64
NEW_SHA = "b" * 64

SAMPLE_ROUTER = (
    "PORTFOLIO_CEILING=0.20\n"
    "history1=load(HISTORY,300000)\n"
    "history2=load(HISTORY,300000)\n"
    "history3=load(HISTORY,300000)\n"
    "other=load(RECON,300000)\n"
    "NATIVE_ORDERS_WILL_NOT_BE_SENT_BY_SOURCE_TEST=True\n"
)


class NativeHistoryGrowthContract(unittest.TestCase):
    def test_all_three_broker_history_call_sites_expand(self):
        new = mod.propose_router_patch(SAMPLE_ROUTER)
        self.assertEqual(new.count("load(HISTORY,NATIVE_HISTORY_LIMIT_BYTES)"), 3)
        self.assertEqual(new.count("load(HISTORY,300000)"), 0)
        self.assertIn("NATIVE_HISTORY_LIMIT_BYTES=5_000_000", new)

    def test_portfolio_risk_and_unrelated_reconcilers_unchanged(self):
        new = mod.propose_router_patch(SAMPLE_ROUTER)
        self.assertEqual(new.count("PORTFOLIO_CEILING=0.20"), 1)
        self.assertEqual(new.count("load(RECON,300000)"), 1)
        self.assertIn("NATIVE_ORDERS_WILL_NOT_BE_SENT_BY_SOURCE_TEST=True", new)

    def test_invalid_partial_or_unexpected_router_rejected(self):
        for x in (
            SAMPLE_ROUTER.replace("load(HISTORY,300000)", "load(HISTORY,1)", 1),
            SAMPLE_ROUTER.replace("PORTFOLIO_CEILING=0.20", "PORTFOLIO_CEILING=1.0"),
            mod.propose_router_patch(SAMPLE_ROUTER),
        ):
            with self.assertRaises(mod.NativeHistoryPatchError):
                mod.propose_router_patch(x)

    def test_new_activation_hash_is_exact_allowlist_not_wildcard(self):
        source = "APPROVED_ROUTER_SHAS=frozenset({\n '" + OLD_SHA + "',\n})"
        altered = mod.propose_activation_source_allowlist(source, OLD_SHA, NEW_SHA)
        self.assertIn("'" + OLD_SHA + "',", altered)
        self.assertEqual(altered.count("'" + NEW_SHA + "',"), 1)
        self.assertNotIn("None, # allow everything", altered)

    def test_already_approved_or_malformed_hash_rejected(self):
        source = "APPROVED_ROUTER_SHAS={" + "'" + OLD_SHA + "',}"
        for candidate in (OLD_SHA, "bad-hash"):
            with self.assertRaises(mod.NativeHistoryPatchError):
                mod.propose_activation_source_allowlist(source, OLD_SHA, candidate)
        prepared = mod.propose_activation_source_allowlist(source, OLD_SHA, NEW_SHA)
        with self.assertRaises(mod.NativeHistoryPatchError):
            mod.propose_activation_source_allowlist(prepared, OLD_SHA, NEW_SHA)

    def test_current_over_300k_authentic_json_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            prefix = '{"padding":"'
            suffix = '"}'
            path.write_bytes((prefix + "x" * (300_141-len(prefix)-len(suffix)) + suffix).encode())
            self.assertEqual(path.stat().st_size, 300_141)
            doc, digest = mod.read_bounded_native_json(path)
            self.assertEqual(len(doc["padding"]), 300_141-len(prefix)-len(suffix))
            self.assertEqual(digest, mod.sha256(path.read_bytes()))

    def test_5million_plus_one_rejected_without_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            path.write_bytes(b'{"a":"' + b"x"*4_999_994 + b'"}')
            self.assertGreater(path.stat().st_size, 5_000_000)
            with self.assertRaises(mod.NativeHistoryPatchError):
                mod.read_bounded_native_json(path)

    def test_symbolic_link_never_resolves_as_broker_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            good = root / "native.json"
            good.write_text('{"history_verified_from_mt5":true}')
            link = root / "link.json"
            link.symlink_to(good)
            with self.assertRaises(mod.NativeHistoryPatchError):
                mod.read_bounded_native_json(link)

    def test_empty_and_invalid_json_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            path.write_bytes(b"")
            with self.assertRaises(mod.NativeHistoryPatchError):
                mod.read_bounded_native_json(path)
            path.write_bytes(b"{broken")
            with self.assertRaises(json.JSONDecodeError):
                mod.read_bounded_native_json(path)

    def test_signed_source_hash_never_guessed(self):
        before = b"original"
        after = b"candidate"
        mod.verify_hash_pinned_candidate(
            before, mod.sha256(before), after, mod.sha256(after)
        )
        with self.assertRaises(mod.NativeHistoryPatchError):
            mod.verify_hash_pinned_candidate(
                before, mod.sha256(b"unrelated"), after, mod.sha256(after)
            )


if __name__ == "__main__":
    unittest.main()
