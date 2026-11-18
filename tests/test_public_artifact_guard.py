"""Offline, stdlib-only checks for the public repository privacy boundary."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "ci" / "verify_public_repo_artifacts.py"
spec = importlib.util.spec_from_file_location("public_artifact_guard", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PublicArtifactGuardTests(unittest.TestCase):
    def test_rejects_issue25_archive(self):
        self.assertIsNotNone(mod.forbidden_reason("issue25-code.tgz"))

    def test_rejects_nested_host_evidence(self):
        self.assertIsNotNone(mod.forbidden_reason("host_evidence/client/proof.json"))

    def test_rejects_broker_statement(self):
        self.assertIsNotNone(mod.forbidden_reason("ReportHistory-account.xlsx"))

    def test_rejects_native_cache(self):
        self.assertIsNotNone(mod.forbidden_reason("data/2026.hcc"))

    def test_rejects_sqlite_state(self):
        self.assertIsNotNone(mod.forbidden_reason("var/lib/state.sqlite3"))

    def test_allows_reviewable_source(self):
        self.assertIsNone(mod.forbidden_reason("src/abutron/brain/brain.py"))
        self.assertIsNone(mod.forbidden_reason("docs/ISSUE25_PUBLIC_SOURCE_HANDOFF.md"))

    def test_does_not_reject_public_brain_provenance(self):
        self.assertIsNone(mod.forbidden_reason("data/brains/unified_m5/brain_provenance.json"))

    def test_detects_tracked_path_not_just_ignored_file(self):
        self.assertEqual(1, len(mod.check_paths(["issue25-code.tgz", "src/module.py"])))


if __name__ == "__main__":
    unittest.main()
