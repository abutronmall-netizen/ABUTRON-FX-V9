"""Privacy, reconnect discovery, and broker-ticket uniqueness regression tests."""
from __future__ import annotations
import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SCRIPTS=ROOT/"scripts"
def load(name):
    p=SCRIPTS/name
    spec=importlib.util.spec_from_file_location("abutron_review_"+name.removesuffix(".py"),p)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class ReviewableIssue25Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collector=load("fp_native_m5_collector.py")
        cls.hcc=load("verified_native_hcc_m5.py")
        cls.archive=load("native_trade_archive.py")

    def make_crm(self,wrong=None,duplicate=False):
        temp=tempfile.TemporaryDirectory(prefix="issue25-public-roster-fixture-")
        database=Path(temp.name)/"crm.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE apex_mt5_connections (id TEXT,account_reference TEXT,broker TEXT,environment TEXT)")
            cases=(("0512","FP Markets","DEMO","a"),
                   ("1636","FP Markets","DEMO","b"),
                   ("1622","FP Markets","DEMO","c"),
                   ("2582","FP Markets","DEMO","d"),
                   ("6207","Deriv","DEMO","e"),
                   ("7593","Deriv","DEMO","f"))
            for suffix,broker,mode,letter in cases:
                if wrong==suffix:mode="LIVE"
                db.execute("INSERT INTO apex_mt5_connections VALUES(?,?,?,?)",
                  (letter*32,"0000"+suffix,broker,mode))
            if duplicate:
                db.execute("INSERT INTO apex_mt5_connections VALUES(?,?,?,?)",
                           ("1"*32,"00000512","FP Markets","DEMO"))
        return temp,database

    def test_fp_roster_from_native_account_db(self):
        temp,db=self.make_crm()
        with temp:
            vals=self.collector.account_connections(db)
            self.assertEqual(set(vals),set(self.collector.EXPECTED_FP_DEMO))
            self.assertEqual(vals["0512"],"a"*32)

    def test_fp_rejects_wrong_live_mode(self):
        temp,db=self.make_crm(wrong="0512")
        with temp:
            with self.assertRaises(ValueError):
                self.collector.account_connections(db)

    def test_fp_rejects_duplicate_native_login(self):
        temp,db=self.make_crm(duplicate=True)
        with temp:
            with self.assertRaises(ValueError):
                self.collector.account_connections(db)

    def test_hcc_both_brokers_discovered_independently(self):
        temp,db=self.make_crm()
        with temp:
            fp,deriv=self.hcc.two_broker_demo_profiles(db)
            self.assertEqual(fp,["a"*32,"b"*32])
            self.assertEqual(deriv,["e"*32,"f"*32])

    def test_hcc_rejects_deriv_live_profile(self):
        temp,db=self.make_crm(wrong="6207")
        with temp:
            with self.assertRaises(ValueError):
                self.hcc.two_broker_demo_profiles(db)

    def test_native_trade_ticket_prevents_identical_entry_collision(self):
        key=self.archive.stable_native_trade_key
        args=("Deriv","6207","Volatility 50 Index.0","BUY",
              "2026-11-18T09:00:00+00:00","150.25","0.2")
        a=key(args[0],args[1],"991001",*args[2:])
        b=key(args[0],args[1],"991002",*args[2:])
        self.assertNotEqual(a,b)
        self.assertEqual(a,key(args[0],args[1],"991001",*args[2:]))

    def test_no_connection_ids_or_full_login_in_public_scripts(self):
        import re
        for path in SCRIPTS.glob("*.py"):
            txt=path.read_text()
            self.assertFalse(re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])",txt),path.name)
            self.assertNotIn("-----BEGIN PRIVATE KEY",txt)

    def test_no_promotion_or_order_submission_paths_introduced(self):
        trainer=(SCRIPTS/"causal_owner_candidate_trainer.py").read_text()
        archive=(SCRIPTS/"native_trade_archive.py").read_text()
        self.assertIn("production_model_promoted=False",trainer)
        self.assertIn("broker_orders_submitted=0",archive)

class FullStagedTransferContractTests(unittest.TestCase):
    """Validate the actual Issue 25 transfer directory, not unrelated top-level scripts."""
    def test_staged_inventory_and_manifest_sha256(self):
        import hashlib
        import json
        root=ROOT
        manifest=json.loads((root/"sanitized-source-manifest.json").read_text())
        expected={str(p.relative_to(root.parents[1])) for p in (root/"scripts").glob("*.py")}
        expected.update(str(p.relative_to(root.parents[1])) for p in (root/"systemd").iterdir())
        self.assertEqual(len(expected),24)
        self.assertEqual(set(manifest["source_files"]),expected)
        for relative,data in manifest["source_files"].items():
            file=root.parents[1]/relative
            self.assertTrue(file.is_file() and not file.is_symlink())
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),data["published_sha256"])

    def test_exact_production_systemd_files_have_no_connection_id_literals(self):
        import re
        units=sorted((ROOT/"systemd").iterdir())
        self.assertEqual(len(units),14)
        for unit in units:
            self.assertIn(unit.suffix,(".service",".timer"))
            self.assertNotIn(b"\x00",unit.read_bytes())
            text=unit.read_text()
            self.assertNotRegex(text,r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
            self.assertNotIn("-----BEGIN PRIVATE KEY",text)

    def test_current_ten_scripts_are_public_text_without_host_ids_or_keys(self):
        import re
        scripts=sorted((ROOT/"scripts").glob("*.py"))
        self.assertEqual(len(scripts),10)
        for script in scripts:
            text=script.read_text()
            self.assertNotRegex(text,r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
            self.assertNotRegex(text,r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")
            self.assertNotRegex(text,r"gh[opusr]_[A-Za-z0-9_]{20,}")
            self.assertNotRegex(text,r"github_pat_[A-Za-z0-9_]{20,}")
            self.assertNotIn("32456207",text)

    def test_staged_bundle_contains_only_reviewable_utf8_files(self):
        stage=ROOT
        paths=[p for p in stage.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        workflow=ROOT.parents[1]/".github"/"workflows"/"abutron-issue25-source.yml"
        self.assertTrue(workflow.is_file())
        paths.append(workflow)
        self.assertEqual(len(paths),28)
        for file in paths:
            self.assertFalse(file.is_symlink())
            self.assertNotIn(b"\x00",file.read_bytes())
            file.read_text(encoding="utf-8")
            self.assertNotIn(file.suffix,(".tgz",".sqlite3",".hcc",".xlsx"))

class LazyRuntimeExecutionTests(unittest.TestCase):
    """Exercise the formerly undefined runtime bindings without broker writes."""

    @staticmethod
    def module(name):
        import sys
        import types
        from unittest import mock
        # CI tests entrypoint wiring, not ndarray math; no optional native
        # numeric/trading package is needed to import these source modules.
        if "numpy" not in sys.modules:
            sys.modules["numpy"] = types.ModuleType("numpy")
        return load(name + ".py")

    def test_causal_helper_resolves_release_src_in_sandbox(self):
        from unittest import mock
        import sys
        mod=self.module("chronological_owner_holdout")
        with tempfile.TemporaryDirectory() as td:
            release=Path(td)/"release"
            src=release/"src"
            src.mkdir(parents=True)
            (src/"issue25_fake_broker_runtime.py").write_text(
                "PROOF='FAKE_BROKER_IMPORT_IN_ISOLATED_RELEASE'" + chr(10),
                encoding="utf-8")
            helper=Path(td)/"train_from_trades_causal.py"
            helper.write_text(
                "from issue25_fake_broker_runtime import PROOF" + chr(10),
                encoding="utf-8")
            original_paths=list(sys.path)
            try:
                with mock.patch.object(mod,"RELEASE",release):
                    with mock.patch.object(mod,"BRAINTRAIN",str(helper)):
                        loaded=mod._load_helper()
                        self.assertEqual(loaded.PROOF,
                            "FAKE_BROKER_IMPORT_IN_ISOLATED_RELEASE")
            finally:
                sys.path[:]=original_paths
                sys.modules.pop("issue25_fake_broker_runtime",None)

    def test_brain_service_data_paths_explicit_and_sandbox_writable(self):
        units=("abutron-v9-four-owner-chronological-holdout-ro.service",
               "abutron-v9-forward-frozen-owner-oos-ro.service",
               "abutron-v9-prospective-broker-m5-shadow-ro.service")
        for unit in units:
            service=(ROOT/"systemd"/unit).read_text()
            self.assertIn("ProtectSystem=strict",service)
            self.assertIn("Environment=ABUTRON_DATA_ROOT=",service)
            self.assertIn("ReadWritePaths=",service)
            self.assertIn("Environment=ABUTRON_LIVE_TRADING_ENABLED=false",service)

    def test_chronological_replay_resolves_all_deps_before_events(self):
        from unittest import mock
        mod = self.module("chronological_owner_holdout")
        class ReachedSource(Exception): pass
        with mock.patch.object(mod,"_load_parent",
              return_value={"sources": lambda: (_ for _ in ()).throw(ReachedSource())}), \
             mock.patch.object(mod,"_load_helper",return_value=object()) as helper, \
             mock.patch.object(mod,"_load_brain_modules",
                               return_value=(object(),object())) as brain:
            with self.assertRaises(ReachedSource):mod.run()
            helper.assert_called_once_with()
            brain.assert_called_once_with()

    def test_chronological_loader_targets_causal_trade_helpers(self):
        from unittest import mock
        mod = self.module("chronological_owner_holdout")
        with tempfile.TemporaryDirectory() as td:
            helper=Path(td)/"train_from_trades_causal.py"
            helper.write_text("TEST_CAUSAL_HELPER='OK'\n",encoding="utf-8")
            with mock.patch.object(mod,"BRAINTRAIN",str(helper)):
                self.assertEqual(mod._load_helper().TEST_CAUSAL_HELPER,"OK")

    def test_deriv_native_scanner_initialized_before_source_io(self):
        from unittest import mock
        mod=self.module("deriv_precision_m5")
        class ReachedIO(Exception):pass
        with mock.patch.object(mod,"_load_scanner",
              return_value=(object(),object())) as scanner, \
             mock.patch.object(mod,"safe_file",side_effect=ReachedIO):
            with self.assertRaises(ReachedIO):mod.main()
            scanner.assert_called_once_with()

    def test_frozen_entry_oos_initializes_before_archive_read(self):
        from unittest import mock
        mod=self.module("frozen_entry_trade_oos")
        class ReachedSource(Exception):pass
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"private"
            root.mkdir(mode=0o700)
            parent={"sources":lambda: (_ for _ in ()).throw(ReachedSource())}
            with mock.patch.object(mod,"ROOT",root), \
                 mock.patch.object(mod,"VALID",root/"validation"), \
                 mock.patch.object(mod.runpy,"run_path",return_value=parent), \
                 mock.patch.object(mod,"_load_production_modules",
                                  return_value=(object(),object())) as brain, \
                 mock.patch.object(mod,"_load_trainhelper",
                                   return_value=object()) as helper:
                with self.assertRaises(ReachedSource):mod.run()
                brain.assert_called_once_with()
                helper.assert_called_once_with()

    def test_prospective_m5_shadow_binds_brain_before_roster_io(self):
        from unittest import mock
        import stat
        import types
        mod=self.module("prospective_m5_shadow")
        class ReachedRoster(Exception):pass
        with tempfile.TemporaryDirectory() as td:
            root=types.SimpleNamespace(is_dir=lambda:True,
                stat=lambda:types.SimpleNamespace(
                    st_uid=0,st_mode=stat.S_IFDIR|0o700))
            with mock.patch.object(mod,"ROOT",root), \
                 mock.patch.object(mod,"OUT",Path(td)/"shadow"), \
                 mock.patch.object(mod,"roster",side_effect=ReachedRoster), \
                 mock.patch.object(mod,"_load_production_modules",
                                  return_value=tuple(object() for _ in range(5))) as brain:
                with self.assertRaises(ReachedRoster):mod.main()
                brain.assert_called_once_with()
                for name in ("load_unified","build_signal_bundle",
                             "features_for_brain","validate_snapshot","utc_now"):
                    self.assertIn(name,vars(mod))

class RuntimeDefectRegressionTests(unittest.TestCase):
    """Permanent regression tests for the three post-merge staging defects.

    These reproduce the deterministic runtime failures found in the
    post-merge staging review of PR #29 (issue #25) and guard against
    their recurrence.
    """

    @staticmethod
    def module(name):
        import sys
        import types
        from unittest import mock
        if "numpy" not in sys.modules:
            sys.modules["numpy"] = types.ModuleType("numpy")
        return load(name + ".py")

    # ---- P1: prospective_m5_shadow base_close must not be shadowed ----
    def test_prospective_shadow_base_close_not_shadowed_by_horizon_outcome(self):
        mod = self.module("prospective_m5_shadow")
        from unittest import mock
        import sqlite3
        # Fixed base time aligned to a 300s M5 boundary.
        now_ts = (1700000000 // 300 + 1) * 300
        bars = []
        t = now_ts - 300 * 65
        for _ in range(65):
            bars.append({
                "open_time": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(),
                "close": "100.0",
            })
            t += 300
        # Current (last closed) bar close = 164.
        bars[-1]["close"] = "164.0"
        acct = "a" * 32
        symbol = "Volatility 50 Index.0"
        rawsource = "test-source"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "shadow"
            out.mkdir()
            dbdir = out / "owners" / "ownerhash"
            dbdir.mkdir(parents=True, mode=0o700)
            dbfile = dbdir / "shadow.sqlite3"
            mod.init_db(sqlite3.connect(dbfile))
            # Pre-seed a horizon_outcome whose next_close (120) differs from
            # the current bar close (164). target_bar_open is intentionally
            # NOT in the known bars so the revision check is skipped, but the
            # verification loop still iterates over this row.
            with sqlite3.connect(dbfile) as db:
                db.execute(
                    "INSERT OR IGNORE INTO horizon_outcomes VALUES (?,?,?,?,?,?,?,?,?)",
                    (acct, symbol, 1, 30, 9999999999, 1, 120.0, now_ts, rawsource))
            fake_bundle = mock.MagicMock()
            fake_bundle.at.return_value = {"market_features": "feats"}
            with mock.patch.object(mod, "OUT", out), \
                 mock.patch.object(mod, "build_signal_bundle",
                                   return_value=fake_bundle, create=True), \
                 mock.patch.object(mod, "probability", return_value=0.5):
                mod.record("ownerhash", acct, symbol, rawsource, bars,
                           mock.MagicMock(), "candsha", mock.MagicMock(), now_ts)
            with sqlite3.connect(dbfile) as db:
                rows = db.execute(
                    "SELECT base_close FROM predictions WHERE account=? AND symbol=?",
                    (acct, symbol)).fetchall()
            self.assertEqual(len(rows), 1, "expected one staged prediction")
            stored = float(rows[0][0])
            self.assertAlmostEqual(stored, 164.0, places=6,
                msg="base_close must be the current bar close (164), "
                    "not the horizon outcome next_close (120)")
            self.assertNotAlmostEqual(stored, 120.0, places=6)

    # ---- P2a: causal trainer quarantines orphan incomplete runs ----
    def test_causal_trainer_quarantines_orphan_incomplete_run(self):
        mod = self.module("causal_owner_candidate_trainer")
        with tempfile.TemporaryDirectory() as td:
            ownerbase = Path(td) / "cand" / "ownerhash"
            runs = ownerbase / "runs"
            runs.mkdir(parents=True, mode=0o700)
            # One verified run (manifest + trades.csv) -- must be untouched.
            verified = runs / "20260101T000000Z"
            verified.mkdir()
            (verified / "candidate-manifest.json").write_text("{}")
            (verified / "trades.csv").write_text("x")
            # One orphan run: trades.csv written, trainer failed before the
            # verified manifest -- must be quarantined, not block retries.
            orphan = runs / "20260102T000000Z"
            orphan.mkdir()
            (orphan / "trades.csv").write_text("y")

            quarantined = mod.quarantine_incomplete_runs(ownerbase)
            self.assertEqual(quarantined, ["20260102T000000Z"])
            # Orphan preserved under _quarantine, verified run untouched.
            self.assertTrue(
                (runs / "_quarantine" / "20260102T000000Z" / "trades.csv").is_file())
            self.assertTrue((verified / "candidate-manifest.json").is_file())
            self.assertTrue((verified / "trades.csv").is_file())
            # Idempotent: second call finds nothing to quarantine.
            self.assertEqual(mod.quarantine_incomplete_runs(ownerbase), [])

    def test_causal_trainer_equality_check_passes_after_quarantine(self):
        mod = self.module("causal_owner_candidate_trainer")
        from unittest import mock
        import hashlib
        # With an orphan trades.csv present the equality check raises
        # MISSING_RUN_MANIFEST_OR_TRAIN_CSV; after quarantine it must not.
        with tempfile.TemporaryDirectory() as td:
            ownerbase = Path(td) / "cand" / "ownerhash"
            runs = ownerbase / "runs"
            runs.mkdir(parents=True, mode=0o700)
            # verified run: empty trades.csv + manifest + checkpoint file
            verified = runs / "20260101T000000Z"
            verified.mkdir()
            (verified / "trades.csv").write_text("")
            csv_sha = hashlib.sha256(b"").hexdigest()
            model_file = verified / "model" / "nexus_brain.json"
            model_file.parent.mkdir()
            model_body = b'{"n_features":49,"members":[]}'
            model_file.write_bytes(model_body)
            model_sha = hashlib.sha256(model_body).hexdigest()
            manifest = {
                "schema": "abutron.v9.owner_isolated_broker_causal_candidate.v1",
                "owner_hash": "ownerhash",
                "pinned_baseline_sha256": mod.BASESHA,
                "model_promotion_allowed": False,
                "broker_orders_sent": 0,
                "candidate_model_path": str(model_file),
                "candidate_model_sha256": model_sha,
                "source_private_training_csv_sha256": csv_sha,
                "newly_trained_events": 0,
                "trained_event_ids": [],
            }
            (verified / "candidate-manifest.json").write_text(
                json.dumps(manifest, sort_keys=True))
            # orphan run: trades.csv, no manifest (failed/incomplete attempt)
            orphan = runs / "20260102T000000Z"
            orphan.mkdir()
            (orphan / "trades.csv").write_text("")
            # safe_json enforces root-owned 0o600; bypass for the fixture so
            # the test exercises the equality check, not the trust machinery.
            def fake_safe_json(p):
                return json.loads(Path(p).read_text())
            with mock.patch.object(mod, "safe_json", side_effect=fake_safe_json), \
                 mock.patch.object(mod, "owner_native_lookup", return_value={}):
                # trained_history_from_runs auto-quarantines the orphan, so the
                # equality check no longer raises MISSING_RUN_MANIFEST_OR_TRAIN_CSV.
                learned, checkpoints = mod.trained_history_from_runs(ownerbase, {})
                self.assertEqual(len(checkpoints), 1)
                # Orphan moved aside; verified run preserved.
                self.assertTrue(
                    (runs / "_quarantine" / "20260102T000000Z" / "trades.csv").is_file())
                self.assertTrue((verified / "candidate-manifest.json").is_file())

    # ---- P2b: chronological holdout empty owner -> INSUFFICIENT, no IndexError ----
    def test_chronological_holdout_empty_owner_returns_insufficient_not_indexerror(self):
        mod = self.module("chronological_owner_holdout")
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "private"
            root.mkdir(mode=0o700)
            owner = "c" * 64
            parent = {
                "sources": lambda: (
                    {"total_causally_closed_positions_in_private_archive": 0}, {}),
                "events": lambda audit: ({}, {}),
                "market_index": lambda: 0,
                "eligible": lambda owners, index: ({owner: []}, 0, {}),
                "BASESHA": "b" * 64,
                "runtime_market": lambda *a, **k: None,
                "csvbytes": lambda rows, fields: b"",
                "TRADEFIELDS": ["a"],
            }
            with mock.patch.object(mod, "_load_parent", return_value=parent), \
                 mock.patch.object(mod, "_load_helper", return_value=mock.MagicMock()), \
                 mock.patch.object(mod, "_load_brain_modules",
                                   return_value=(mock.MagicMock(), mock.MagicMock())), \
                 mock.patch.object(mod, "ROOT", root):
                # Must NOT raise IndexError on rows[int(len(rows)*0.7)].
                mod.run()
            report_path = root / "validation" / "latest-chronological-holdout.json"
            self.assertTrue(report_path.is_file(), "holdout report should be written")
            report = json.loads(report_path.read_text())
            entry = report["owners"][owner[:12]]
            self.assertEqual(entry["status"], "INSUFFICIENT_PURGED_HISTORY")
            self.assertEqual(entry["train"], 0)
            self.assertEqual(entry["holdout"], 0)


if __name__=="__main__":
    unittest.main()

