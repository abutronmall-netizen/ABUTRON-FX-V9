"""Privacy, reconnect discovery, and broker-ticket uniqueness regression tests."""
from __future__ import annotations
import importlib.util
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
        stage=ROOT.parents[1]
        paths=[p for p in stage.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        self.assertEqual(len(paths),28)
        for file in paths:
            self.assertFalse(file.is_symlink())
            self.assertNotIn(b"\x00",file.read_bytes())
            file.read_text(encoding="utf-8")
            self.assertNotIn(file.suffix,(".tgz",".sqlite3",".hcc",".xlsx"))

if __name__=="__main__":
    unittest.main()
