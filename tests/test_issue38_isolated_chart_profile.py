"""Regression cases for Issue38: MT5 QA 'open charts limit reached'.

Only fake, disposable chart filenames; no account, credentials or broker access.
"""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "ops/issue38/isolated_chart_profile.py"
spec = importlib.util.spec_from_file_location("issue38_qa_chart_profile", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class IsolatedChartCapacityTests(unittest.TestCase):
    def make_profile(self, base):
        state = Path(base) / "private-no-order-qa"
        terminal = state / "terminal"
        default = terminal / "MQL5/Profiles/Charts/Default"
        default.mkdir(parents=True, mode=0o700)
        return state, terminal, default

    def test_initial_99_charts_preserved_with_empty_startup(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            for n in range(1, 100):
                (default / f"chart{n:02}.chr").write_bytes(b"original-qa-chart")
            (default / "order.wnd").write_bytes(b"original-qa-window")
            result = mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertEqual(result["active_charts"], 0)
            self.assertTrue(result["original_qa_profile_preserved"])
            self.assertEqual(len(list(default.iterdir())), 0)
            original = state / "issue38-original-pre-fix-qa-charts"
            self.assertEqual(len(list(original.glob("chart*.chr"))), 99)
            self.assertTrue((original / "order.wnd").is_file())

    def test_second_and_third_run_only_keep_last_disposable_profile(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            (default / "chart01.chr").write_bytes(b"original")
            mod.reset_isolated_probe_chart_capacity(state, terminal)
            (default / "chart01.chr").write_bytes(b"second")
            mod.reset_isolated_probe_chart_capacity(state, terminal)
            prev = state / "issue38-previous-qa-session-charts"
            self.assertEqual((prev / "chart01.chr").read_bytes(), b"second")
            (default / "chart01.chr").write_bytes(b"third")
            mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertEqual((prev / "chart01.chr").read_bytes(), b"third")
            self.assertEqual(
                (state / "issue38-original-pre-fix-qa-charts/chart01.chr").read_bytes(),
                b"original",
            )
            self.assertEqual(len(list(default.iterdir())), 0)

    def test_empty_existing_profile_is_left_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            result = mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertEqual(result["active_charts"], 0)
            self.assertFalse(result["original_qa_profile_preserved"])

    def test_fresh_profile_directory_created_if_absent(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            default.rmdir()
            mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertTrue(default.is_dir())

    def test_non_qa_artifact_is_rejected_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            extra = default / "unreviewed-native-writer.ex5"
            extra.write_bytes(b"untrusted")
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_ISOLATED_QA_CHART_ARTIFACT"):
                mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertEqual(extra.read_bytes(), b"untrusted")

    def test_symlinked_chart_is_rejected_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            link = default / "chart01.chr"
            link.symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_ISOLATED_QA_CHART_ARTIFACT"):
                mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertTrue(link.is_symlink())

    def test_symlinked_default_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            default.rmdir()
            default.symlink_to("/tmp")
            with self.assertRaisesRegex(RuntimeError, "QA_CHART_ARCHIVE_SYMLINK_DENIED"):
                mod.reset_isolated_probe_chart_capacity(state, terminal)

    def test_refuses_non_account_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            with self.assertRaisesRegex(RuntimeError, "NOT_AN_ISOLATED_ACCOUNT_TERMINAL"):
                mod.reset_isolated_probe_chart_capacity(state, Path(td))
            self.assertTrue(default.is_dir())

    def test_unexpected_large_or_many_chart_files_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            (default / "chart01.chr").write_bytes(b"0" * (2_000_001))
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_ISOLATED_QA_CHART_ARTIFACT"):
                mod.reset_isolated_probe_chart_capacity(state, terminal)

    def test_untrusted_previous_archive_blocks_loss(self):
        with tempfile.TemporaryDirectory() as td:
            state, terminal, default = self.make_profile(td)
            (default / "chart01.chr").write_bytes(b"first")
            mod.reset_isolated_probe_chart_capacity(state, terminal)
            (default / "chart01.chr").write_bytes(b"second")
            prev = state / "issue38-previous-qa-session-charts"
            prev.mkdir()
            (prev / "unknown").write_bytes(b"do not delete")
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_ISOLATED_QA_CHART_ARTIFACT"):
                mod.reset_isolated_probe_chart_capacity(state, terminal)
            self.assertTrue((prev / "unknown").is_file())
            self.assertTrue((default / "chart01.chr").is_file())

    def test_source_does_not_call_broker_or_service_mutation(self):
        source = SOURCE.read_text()
        for prohibited in ("OrderSend(", "TRADE_ACTION_DEAL", "systemctl",
                           "subprocess.", "socket.", "requests.post("):
            self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
