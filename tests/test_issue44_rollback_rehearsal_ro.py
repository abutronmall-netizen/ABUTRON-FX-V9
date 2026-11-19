"""Issue44 rollback source-contract tests. No broker session or deployment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import hashlib

S=Path(__file__).resolve().parents[1]/"ops/issue44/rollback_rehearsal_ro.py"
spec=importlib.util.spec_from_file_location("issue44_offline_rollback",S)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class OfflineRollback(unittest.TestCase):
    def test_all_boundaries_and_normal_sequence(self):
        result=m.simulate({"ea":(b"old-ea",b"new-ea"),
                           "chart":(b"old-chart",b"new-chart"),
                           "router":(b"old-router",b"new-router")})
        self.assertEqual(result["fault_boundaries_tested"],3)
        self.assertTrue(result["successful_promotion_then_rollback_tested"])
        self.assertFalse(result["production_worker_restarted"])
        self.assertFalse(result["broker_position_preservation_certified"])
        self.assertFalse(result["broker_sltp_amendment_certified"])

    def test_duplicate_key_schema_invalid(self):
        bad={"schema":"abutron.issue44.rollback_sources.v1",
             "artifacts":[{"name":"EA","old_path":"a","old_sha":"0"*64,
                           "new_path":"b","new_sha":"1"*64}]*2}
        with self.assertRaises(m.RehearsalError):
            m.run_manifest(bad)

    def test_malformed_manifest_rejected(self):
        for bad in ({},None,{"schema":"bad","artifacts":[]},
                    {"schema":"abutron.issue44.rollback_sources.v1",
                     "artifacts":[{"name":"bad/file"}]}):
            with self.assertRaises(m.RehearsalError):
                m.run_manifest(bad)

    def test_source_digests_must_match(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.bin"
            p.write_bytes(b"some-verified-data")
            self.assertEqual(m.read_exact(p,hashlib.sha256(p.read_bytes()).hexdigest()),b"some-verified-data")
            with self.assertRaises(m.RehearsalError):
                m.read_exact(p,"f"*64)

    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.bin"
            p.write_bytes(b"source")
            link=Path(d)/"link.bin"
            link.symlink_to(p)
            with self.assertRaises(m.RehearsalError):
                m.read_exact(link,m.sha(p.read_bytes()))

    def test_wrong_sha_length_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.bin"
            p.write_bytes(b"source")
            with self.assertRaises(m.RehearsalError):
                m.read_exact(p,"abc")

    def test_invalid_artifacts_rejected(self):
        for vals in ({},{"../live":(b"old",b"new")},
                     {"bad":(b"",b"new")},
                     {"bad":(b"old","new")}):
            with self.assertRaises(m.RehearsalError):
                m.simulate(vals)

    def test_source_does_not_call_systemd_brokers_or_ssh(self):
        source=S.read_text()
        for word in ("systemctl", "OrderSend(", "TRADE_ACTION_SLTP",
                     "subprocess.run", "os.system(", "requests.post"):
            self.assertNotIn(word,source)

    def test_original_and_candidate_sha_recorded(self):
        x=m.simulate({"b":(b"old",b"new")})
        self.assertEqual(x["original_sha256"]["b"],m.sha(b"old"))
        self.assertEqual(x["candidate_sha256"]["b"],m.sha(b"new"))
        self.assertIs(x["live_order_authority_changed"],False)

if __name__=="__main__":
    unittest.main()
