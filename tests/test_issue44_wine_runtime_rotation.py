"""Offline tests for exact, account-scoped Wine runtime rotation.

No real MT5, broker, systemd service, credentials, or trading actions.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

P=Path(__file__).resolve().parents[1]/"ops/issue44/wine_runtime_rotation.py"
spec=importlib.util.spec_from_file_location("issue44_wine_rotation",P)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

CID="b"*32
NAME="abutron-client-mt5-"+CID

class WineRotation(unittest.TestCase):
 def setup_state(self,root):
    state=Path(root)/NAME
    state.mkdir(mode=0o700)
    original=state/"wine"
    original.mkdir(mode=0o700)
    (original/"historic-original-proof.txt").write_text("native-account-old-session")
    return state

 def test_roundtrip_keeps_old_and_new_separate(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      r=m.rotate_after_stop(state,NAME)
      self.assertEqual(r["status"],"ROTATED_FOR_ISOLATED_ACCOUNT_START")
      self.assertTrue((state/"wine").is_dir())
      self.assertFalse((state/"wine/historic-original-proof.txt").exists())
      self.assertEqual((state/m.PREVIOUS_NAME/"historic-original-proof.txt").read_text(),
                       "native-account-old-session")
      (state/"wine/new-runtime-proof.txt").write_text("untrusted-new")
      a=m.audit(state,NAME)
      self.assertTrue(a["previous_wine_preserved"])
      self.assertIs(a["native_broker_reconnection_certified"],False)
      rollback=m.rollback_after_stop(state,NAME)
      self.assertEqual(rollback["status"],"PREVIOUS_WINE_RUNTIME_RESTORED")
      self.assertEqual((state/"wine/historic-original-proof.txt").read_text(),
                       "native-account-old-session")
      self.assertEqual((state/m.FAILED_NAME/"new-runtime-proof.txt").read_text(),
                       "untrusted-new")
      self.assertTrue((state/m.MARKER).is_file())

 def test_repeat_rotate_refused(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      m.rotate_after_stop(state,NAME)
      with self.assertRaisesRegex(m.WineRotationError,"ROTATION_NOT_IN_UNMODIFIED_BASELINE"):
        m.rotate_after_stop(state,NAME)

 def test_repeat_rollback_refused(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      m.rotate_after_stop(state,NAME)
      m.rollback_after_stop(state,NAME)
      with self.assertRaises(m.WineRotationError):
        m.rollback_after_stop(state,NAME)

 def test_preexisting_journal_blocks_first_rotation(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      (state/m.MARKER).write_text("{}")
      with self.assertRaises(m.WineRotationError):
        m.rotate_after_stop(state,NAME)
      self.assertTrue((state/"wine/historic-original-proof.txt").is_file())

 def test_preexisting_old_backup_refuses_overwrite(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      (state/m.PREVIOUS_NAME).mkdir()
      with self.assertRaises(m.WineRotationError):
        m.rotate_after_stop(state,NAME)
      self.assertTrue((state/"wine/historic-original-proof.txt").is_file())

 def test_mismatched_account_state_denied(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      with self.assertRaises(m.WineRotationError):
        m.rotate_after_stop(state,"abutron-client-mt5-"+"c"*32)
      self.assertTrue((state/"wine/historic-original-proof.txt").is_file())

 def test_state_symlink_denied(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      alias=Path(tmp)/"state-alias"
      alias.symlink_to(state, target_is_directory=True)
      with self.assertRaises(m.WineRotationError):
        m.rotate_after_stop(alias,NAME)

 def test_wine_prefix_symlink_denied(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      old=state/"wine"
      old.rename(state/"actual")
      old.symlink_to(state/"actual",target_is_directory=True)
      with self.assertRaises(m.WineRotationError):
        m.rotate_after_stop(state,NAME)

 def test_group_writable_state_denied(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      state.chmod(0o770)
      with self.assertRaisesRegex(m.WineRotationError,"UNTRUSTED_STATE"):
        m.rotate_after_stop(state,NAME)

 def test_wrong_or_tampered_journal_denies_rollback(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      m.rotate_after_stop(state,NAME)
      (state/m.MARKER).write_text('{"schema":"wrong"}')
      with self.assertRaisesRegex(m.WineRotationError,"ROTATION_JOURNAL_ACCOUNT_MISMATCH"):
        m.rollback_after_stop(state,NAME)
      self.assertTrue((state/m.PREVIOUS_NAME/"historic-original-proof.txt").is_file())

 def test_missing_previous_prefix_denies_rollback(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      m.rotate_after_stop(state,NAME)
      (state/m.PREVIOUS_NAME).rename(state/"some-old-dir")
      with self.assertRaisesRegex(m.WineRotationError,"PREVIOUS_WINE_RUNTIME_NOT_PRESENT"):
        m.rollback_after_stop(state,NAME)

 def test_preexisting_failed_prefix_denies_replay(self):
    with tempfile.TemporaryDirectory() as tmp:
      state=self.setup_state(tmp)
      m.rotate_after_stop(state,NAME)
      (state/m.FAILED_NAME).mkdir()
      with self.assertRaisesRegex(m.WineRotationError,"AMBIGUOUS_ROLLBACK"):
        m.rollback_after_stop(state,NAME)

 def test_source_has_no_systemctl_or_broker_trade_mutation(self):
    source=P.read_text()
    for forbidden in ("OrderSend(","OrderCheck(","TRADE_ACTION_SLTP",
                      "subprocess.","systemctl","os.system("):
      self.assertNotIn(forbidden,source)
    self.assertIn('"production_release_certified":False',source)
    self.assertIn('"native_broker_reconnection_certified":False',source)

if __name__=="__main__":
 unittest.main()
