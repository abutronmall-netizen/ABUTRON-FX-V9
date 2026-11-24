#!/usr/bin/env python3
"""Tests for reconcile_fp9392_compiler_assets.py.

Verifies the surgical corrections for FP 9392 compiler asset preparation:
  1. Pinned stock compiler assets extended to the 9392 LIVE read-only
     account (gate accepts both FP6364 and 9392, with identities
     extracted from the file — no hardcoded identifiers).
  2. Preservation of LIVE OFF guards, retry bounds, 9392 profile
     preparation, and manifest verification.
  3. Atomic write with ownership preservation.
  4. Runtime regression: the patched staging function executes and
     stages verified stock compiler assets for 9392 (isolated fixtures,
     no broker data).
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops" / "v9-cutover"))

from reconcile_fp9392_compiler_assets import (
    PATCH_MARKER,
    _extract_gate_tuple,
    atomic_write,
    find_function,
    is_already_patched,
    patch_worker,
    verify_patch,
)

# --------------------------------------------------------------------------- #
# Fixture: focused inline worker source containing all production anchors
# referenced by the correction script and its verification checks.
# Mirrors the deployed worker's structure (anonymized, NOT for deployment).
# --------------------------------------------------------------------------- #
WORKER_SOURCE = """\
#!/usr/bin/python3
\"\"\"ABUTRON client MT5 worker: isolated UID, broker-native feed plus gated order bridge.\"\"\"
import os,sys,json,subprocess,time,shutil,secrets,uuid,signal
from pathlib import Path

SRC=Path(\"/opt/abutron/client-mt5-template-v3\")
WARM=Path(\"/opt/abutron/fpmarkets-mql-bootstrap-v1\")

def handoff_event(journal_bytes):
    if not journal_bytes: return False
    return False

def fp1622_mql_recompile_startup_handoff(journal_bytes):
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode(\"utf-16-le\",errors=\"replace\")
              if b\"\\x00\" in journal_bytes[:100]
              else journal_bytes.decode(\"utf-8\",errors=\"replace\")).lower()
    except UnicodeError:return False
    return (\"compiler\\tfull recompilation has been started\" in text
            and \"terminal\\texit with code 0\" in text
            and \"terminal\\tstopped with 10053\" in text)

def fp6364_invalid_account_journal(data):
 if not data:return False
 return False

def cgroup_terminal_running():
    return False

def hold_zero_exit_for_native_proof(returncode,exact_fresh_heartbeat,seconds_since_exit):
    if returncode != 0 or seconds_since_exit < 0:
        return False
    if exact_fresh_heartbeat:
        return True
    return seconds_since_exit < 18.0

def exit_action(returncode,was_connected,attached,handoff,attempt,total_elapsed):
    if attached:return \"MONITOR_CHILD\"
    if was_connected:return \"STOP\"
    if returncode==0 and handoff and attempt<3 and total_elapsed<220:
        return \"RETRY_UPDATE\"
    return \"STOP\"

def stage_fp_live_6364_preserved_profile(terminal,cid,login,server,mode):
 return \"SKIP\"

def stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid):
 \"\"\"PINNED LIVE9392 broker-history only, no order calls or LIVE enablement.\"\"\"
 if (cid,login,server,mode)!=(\"f0000000000000000000000000000007\",
                              \"90000008\",\"FPMarketsSC-Live\",\"LIVE\"):
  return \"SKIP_OTHER_ACCOUNT\"
 named=terminal/\"MQL5/Profiles/Charts/ABUTRON9392_READONLY\"
 return \"FP9392_STAGED\"

def stage_fp6364_stock_compiler_assets(terminal,cid,login,server,mode):
 \"\"\"Seed only verified stock MT5 Expert examples to avoid repeated first-run
 recompilation. Does not copy any customer charts, EAs or broker state.\"\"\"
 if (cid,login,server,mode)!=(\"f0000000000000000000000000000005\",
                              \"90000010\",\"FPMarketsSC-Live\",\"LIVE\"):
  return \"SKIP_UNRELATED_ACCOUNT\"
 import hashlib,json,stat
 cache=Path(\"/usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1\")
 index=cache/\"sha256-manifest.json\"
 trusted_index=\"a66bec82836ccaf0eb1c25ba70636b9d60be4d72d4910b6ed67d188db89ce687\"
 if not index.is_file() or index.is_symlink() or hashlib.sha256(index.read_bytes()).hexdigest()!=trusted_index:
  raise RuntimeError(\"FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED\")
 manifest=json.loads(index.read_text())[\"files\"]
 if len(manifest)!=64 or sum(r.endswith(\".ex5\") for r in manifest)!=32:
  raise RuntimeError(\"FP6364_STOCK_MANIFEST_COUNT_INVALID\")
 for relative,expected in sorted(manifest.items()):
  rel=Path(relative)
  if (rel.is_absolute() or \"..\" in rel.parts or len(rel.parts)<3 or
      rel.parts[:2] not in ((\"Experts\",\"Advisors\"),(\"Experts\",\"Free Robots\")) or
      rel.suffix not in (\".mq5\",\".ex5\")):
   raise RuntimeError(\"FP6364_STOCK_PATH_REJECTED\")
  src=cache/rel;dest=terminal/\"MQL5\"/rel
  if src.is_symlink() or not src.is_file() or dest.is_symlink() or hashlib.sha256(src.read_bytes()).hexdigest()!=expected:
   raise RuntimeError(\"FP6364_STOCK_SHA256_REJECTED\")
  dest.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(src,dest)
  if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
   raise RuntimeError(\"FP6364_STOCK_INSTALL_FAILED\")
 return \"FP6364_VERIFIED_STOCK_EXPERT_COMPILER_CACHE_ONLY\"

def main():
 terminal=Path(\"/tmp/terminal\")
 if WARM.is_dir():
  for name in (\"Include\",\"Indicators\",\"Experts/Examples\",\"Scripts/Examples\"):
   standard=WARM/name
   if standard.is_dir():shutil.copytree(standard,terminal/\"MQL5\"/name,dirs_exist_ok=True,symlinks=False)
 stage_fp6364_stock_compiler_assets(terminal,cid,login,server,mode)
 attempt=-1
 while attempt < 3:
  attempt+=1
  fp6364_compiler_exit69=(
      cid==\"f0000000000000000000000000000005\"
      and broker==\"FP Markets\" and server==\"FPMarketsSC-Live\"
      and login==\"90000010\" and mode==\"LIVE\"
      and process.returncode==69 and not was_connected
      and attempt<2 and time.monotonic()-started<90
      and fp1622_mql_recompile_startup_handoff(tail))
  if process.returncode!=0 and not fp6364_compiler_exit69:
   break
 return
"""


# --------------------------------------------------------------------------- #
# TestPatchContent
# --------------------------------------------------------------------------- #
class TestPatchContent:
    """Verify the patched source contains the required corrections."""

    def test_gate_extended_to_two_identities(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        fn_start, fn_end = find_function(patched, "stage_fp6364_stock_compiler_assets")
        fn_text = patched[fn_start:fn_end]
        assert "not in ((" in fn_text

    def test_gate_old_removed(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        fn_start, fn_end = find_function(patched, "stage_fp6364_stock_compiler_assets")
        fn_text = patched[fn_start:fn_end]
        assert "!=(\"f0000000000000000000000000000005\"" not in fn_text

    def test_fp6364_identity_preserved_in_gate(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        fn_start, fn_end = find_function(patched, "stage_fp6364_stock_compiler_assets")
        fn_text = patched[fn_start:fn_end]
        assert "f0000000000000000000000000000005" in fn_text
        assert "90000010" in fn_text

    def test_9392_identity_in_gate(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        fn_start, fn_end = find_function(patched, "stage_fp6364_stock_compiler_assets")
        fn_text = patched[fn_start:fn_end]
        assert "f0000000000000000000000000000007" in fn_text
        assert "90000008" in fn_text

    def test_patch_marker_inserted(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert PATCH_MARKER in patched

    def test_compiles(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        compile(patched, "<test>", "exec")

    def test_warm_cache_loop_unchanged(self):
        """The warm-cache copy loop must remain unchanged — the pinned
        stock compiler cache is the correct asset source, and the warm
        cache does not contain the additional directories."""
        patched, _ = patch_worker(WORKER_SOURCE)
        for line in patched.splitlines():
            if 'for name in ("Include"' in line:
                assert "Experts/Advisors" not in line
                assert "Experts/Free Robots" not in line
                break

    def test_no_hardcoded_identifiers_in_patch_constants(self):
        """The patch script must not contain hardcoded account identifiers
        in its gate constants — identities are extracted from the file."""
        import reconcile_fp9392_compiler_assets as mod
        # The module should not have GATE_OLD / GATE_NEW constants with
        # hardcoded identifiers
        assert not hasattr(mod, "GATE_OLD") or "f0000000000000000000000000000005" not in str(getattr(mod, "GATE_OLD", ""))
        assert not hasattr(mod, "GATE_NEW") or "f0000000000000000000000000000007" not in str(getattr(mod, "GATE_NEW", ""))


# --------------------------------------------------------------------------- #
# TestIdentifierAgnostic
# --------------------------------------------------------------------------- #
class TestIdentifierAgnostic:
    """Verify the patch works with any account identifiers, not just the
    anonymized placeholders in the review copy."""

    @staticmethod
    def _make_variant(cid_6364, cid_9392):
        text = WORKER_SOURCE
        text = text.replace("f0000000000000000000000000000005", cid_6364)
        text = text.replace("f0000000000000000000000000000007", cid_9392)
        return text

    def test_patch_with_real_identifiers(self):
        text = self._make_variant("a1b2c3d4e5f60718293a4b5c6d7e8f90",
                                   "b2c3d4e5f60718293a4b5c6d7e8f9a01")
        patched, changes = patch_worker(text)
        errors = verify_patch(patched, text)
        assert errors == []
        assert "a1b2c3d4e5f60718293a4b5c6d7e8f90" in patched
        assert "b2c3d4e5f60718293a4b5c6d7e8f9a01" in patched

    def test_patch_with_short_identifiers(self):
        text = self._make_variant("cid6364", "cid9392")
        patched, changes = patch_worker(text)
        errors = verify_patch(patched, text)
        assert errors == []
        assert "cid6364" in patched
        assert "cid9392" in patched

    def test_patch_with_hex_identifiers(self):
        text = self._make_variant("deadbeefdeadbeefdeadbeefdeadbeef",
                                   "cafebabe cafebabe cafebabe cafebabe".replace(" ", ""))
        patched, changes = patch_worker(text)
        errors = verify_patch(patched, text)
        assert errors == []


# --------------------------------------------------------------------------- #
# TestVerifyPatch
# --------------------------------------------------------------------------- #
class TestVerifyPatch:
    """Verify verify_patch() detects missing or broken corrections."""

    def test_verify_passes_on_patched(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert errors == []

    def test_verify_detects_missing_gate_correction(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # Revert the gate correction: replace the extended gate with the
        # original FP6364-only gate inside the function
        fn_start, fn_end = find_function(patched, "stage_fp6364_stock_compiler_assets")
        fn_text = patched[fn_start:fn_end]
        gate_idx = fn_text.find("if (cid,login,server,mode)")
        gate_end = fn_text.find('return "SKIP_UNRELATED_ACCOUNT"', gate_idx) + len('return "SKIP_UNRELATED_ACCOUNT"')
        old_gate = ('if (cid,login,server,mode)!=("f0000000000000000000000000000005",\n'
                    '                              "90000010","FPMarketsSC-Live","LIVE"):\n'
                    '  return "SKIP_UNRELATED_ACCOUNT"')
        patched = patched[:fn_start] + fn_text[:gate_idx] + old_gate + fn_text[gate_end:] + patched[fn_end:]
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("gate not extended" in e or "two account identities" in e for e in errors)

    def test_verify_detects_missing_marker(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace(PATCH_MARKER, "", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("PATCH_MARKER" in e for e in errors)

    def test_verify_detects_retry_bound_change(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("while attempt < 3:", "while attempt < 5:", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("retry bound" in e for e in errors)

    def test_verify_detects_lost_live_off_guard(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("def hold_zero_exit_for_native_proof",
                                  "def _removed_hold_zero_exit", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("LIVE OFF guard" in e for e in errors)

    def test_verify_detects_lost_9392_profile(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("ABUTRON9392_READONLY", "SOMETHING_ELSE", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("ABUTRON9392_READONLY" in e for e in errors)

    def test_verify_detects_lost_manifest_verification(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED",
                                  "REMOVED_MANIFEST_CHECK", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("manifest verification" in e for e in errors)

    def test_verify_detects_fp6364_identity_change(self):
        """If the FP6364 identity in the patched gate differs from the
        original, verify_patch must flag it."""
        patched, _ = patch_worker(WORKER_SOURCE)
        # Tamper with the FP6364 identity in the patched gate
        patched = patched.replace("f0000000000000000000000000000005",
                                   "tampered_cid", 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("FP6364 identity changed" in e for e in errors)


# --------------------------------------------------------------------------- #
# TestIdempotency
# --------------------------------------------------------------------------- #
class TestIdempotency:
    def test_already_patched_detected(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert is_already_patched(patched)

    def test_patch_is_idempotent(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched2, changes2 = patch_worker(patched)
        assert patched2 == patched
        assert "idempotent" in changes2[0]


# --------------------------------------------------------------------------- #
# TestAtomicWrite
# --------------------------------------------------------------------------- #
class TestAtomicWrite:
    def test_atomic_write_creates_file(self, tmp_path):
        target = tmp_path / "test.py"
        atomic_write(target, "print('hello')")
        assert target.is_file()
        assert target.read_text() == "print('hello')"

    def test_atomic_write_replaces_existing(self, tmp_path):
        target = tmp_path / "test.py"
        target.write_text("old content")
        atomic_write(target, "new content")
        assert target.read_text() == "new content"
        leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert len(leftovers) == 0

    def test_atomic_write_preserves_permissions(self, tmp_path):
        target = tmp_path / "test.py"
        target.write_text("old content")
        target.chmod(0o600)
        original_mode = target.stat().st_mode & 0o777
        atomic_write(target, "new content")
        assert (target.stat().st_mode & 0o777) == original_mode

    def test_atomic_write_preserves_ownership(self, tmp_path):
        """atomic_write must call os.chown with the original UID/GID.

        shutil.copystat does NOT preserve ownership. This test uses
        unittest.mock to verify os.chown is called with the correct
        UID and GID, demonstrating explicit ownership preservation.
        """
        target = tmp_path / "test.py"
        target.write_text("old content")
        stat_before = target.stat()

        with patch("os.chown") as mock_chown:
            atomic_write(target, "new content")
            assert mock_chown.called, "os.chown must be called to preserve ownership"
            call_args = mock_chown.call_args
            assert call_args[0][1] == stat_before.st_uid, "UID must be preserved"
            assert call_args[0][2] == stat_before.st_gid, "GID must be preserved"


# --------------------------------------------------------------------------- #
# TestRuntimeRegression
# --------------------------------------------------------------------------- #
class TestRuntimeRegression:
    """Runtime tests that execute the patched staging function.

    Uses isolated offline fixtures (fake cache + fake terminal) — no
    broker data, no credentials, no real MT5 installation.
    """

    @staticmethod
    def _make_patched_module(patched_source):
        """Compile and return a module from patched source for execution."""
        mod = types.ModuleType("patched_worker")
        mod.__dict__["__name__"] = "patched_worker"
        exec(compile(patched_source, "<patched>", "exec"), mod.__dict__)
        return mod

    @staticmethod
    def _make_stock_cache(tmp_path):
        """Create an isolated verified stock compiler assets cache.

        Mimics /usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1:
        - sha256-manifest.json with 64 files (32 MQ5 + 32 EX5)
        - path-restricted to Experts/Advisors and Experts/Free Robots
        - all files hashed and verified
        """
        import hashlib
        import json

        cache = tmp_path / "stock-cache"
        cache.mkdir()
        files = {}
        for i in range(16):
            for sub in ("Experts/Advisors", "Experts/Free Robots"):
                for ext in (".mq5", ".ex5"):
                    rel = f"{sub}/Stock{i}{ext}"
                    f = cache / rel
                    f.parent.mkdir(parents=True, exist_ok=True)
                    content = f"stock-compiler-asset-{i}{ext}".encode()
                    f.write_bytes(content)
                    files[rel] = hashlib.sha256(content).hexdigest()
        assert len(files) == 64
        assert sum(r.endswith(".ex5") for r in files) == 32

        manifest = {"files": files}
        index = cache / "sha256-manifest.json"
        index.write_text(json.dumps(manifest))
        trusted_index = hashlib.sha256(index.read_bytes()).hexdigest()
        return cache, trusted_index

    @staticmethod
    def _patch_fixture_paths(patched, cache, trusted_index):
        """Redirect the staging function to isolated fixture paths."""
        patched = patched.replace(
            'trusted_index="a66bec82836ccaf0eb1c25ba70636b9d60be4d72d4910b6ed67d188db89ce687"',
            f'trusted_index="{trusted_index}"',
            1
        )
        patched = patched.replace(
            'cache=Path("/usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1")',
            f'cache=Path("{cache}")',
            1
        )
        return patched

    def test_9392_staging_function_accepts_9392_identity(self, tmp_path):
        """The patched staging function must accept the 9392 LIVE
        read-only identity and stage verified stock compiler assets."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        result = mod.stage_fp6364_stock_compiler_assets(
            terminal,
            "f0000000000000000000000000000007",
            "90000008",
            "FPMarketsSC-Live",
            "LIVE",
        )
        assert result == "FP6364_VERIFIED_STOCK_EXPERT_COMPILER_CACHE_ONLY"

        staged_ex5 = list((terminal / "MQL5").rglob("*.ex5"))
        assert len(staged_ex5) == 32, f"Expected 32 EX5 files, got {len(staged_ex5)}"
        staged_mq5 = list((terminal / "MQL5").rglob("*.mq5"))
        assert len(staged_mq5) == 32, f"Expected 32 MQ5 files, got {len(staged_mq5)}"

    def test_9392_staging_rejects_unrelated_account(self, tmp_path):
        """The patched staging function must still reject unrelated
        accounts (account isolation preserved)."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        result = mod.stage_fp6364_stock_compiler_assets(
            terminal,
            "f0000000000000000000000000000009",
            "90000099",
            "FPMarketsSC-Live",
            "LIVE",
        )
        assert result == "SKIP_UNRELATED_ACCOUNT"
        assert not (terminal / "MQL5").exists()

    def test_9392_staging_rejects_untrusted_manifest(self, tmp_path):
        """The patched staging function must reject a tampered manifest."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, _ = self._make_stock_cache(tmp_path)
        index = cache / "sha256-manifest.json"
        index.write_text("tampered")

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        with pytest.raises(RuntimeError, match="FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED"):
            mod.stage_fp6364_stock_compiler_assets(
                terminal,
                "f0000000000000000000000000000007",
                "90000008",
                "FPMarketsSC-Live",
                "LIVE",
            )

    def test_9392_staging_rejects_tampered_asset(self, tmp_path):
        """The patched staging function must reject a tampered asset file."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        victim = cache / "Experts" / "Advisors" / "Stock0.ex5"
        victim.write_bytes(b"tampered-content")
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        with pytest.raises(RuntimeError, match="FP6364_STOCK_SHA256_REJECTED"):
            mod.stage_fp6364_stock_compiler_assets(
                terminal,
                "f0000000000000000000000000000007",
                "90000008",
                "FPMarketsSC-Live",
                "LIVE",
            )

    def test_9392_staging_rejects_symlink_source(self, tmp_path):
        """The patched staging function must reject symlinked sources."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        victim = cache / "Experts" / "Advisors" / "Stock1.mq5"
        victim.unlink()
        victim.symlink_to(cache / "Experts" / "Advisors" / "Stock0.mq5")
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        with pytest.raises(RuntimeError, match="FP6364_STOCK_SHA256_REJECTED"):
            mod.stage_fp6364_stock_compiler_assets(
                terminal,
                "f0000000000000000000000000000007",
                "90000008",
                "FPMarketsSC-Live",
                "LIVE",
            )

    def test_fp6364_still_staged_after_patch(self, tmp_path):
        """FP6364 must still be staged after the gate extension (no
        regression)."""
        patched, _ = patch_worker(WORKER_SOURCE)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        result = mod.stage_fp6364_stock_compiler_assets(
            terminal,
            "f0000000000000000000000000000005",
            "90000010",
            "FPMarketsSC-Live",
            "LIVE",
        )
        assert result == "FP6364_VERIFIED_STOCK_EXPERT_COMPILER_CACHE_ONLY"
        staged = list((terminal / "MQL5").rglob("*.ex5"))
        assert len(staged) == 32

    def test_9392_staging_with_real_identifiers(self, tmp_path):
        """The patched staging function must work with real (non-anonymized)
        identifiers extracted from the file."""
        # Use real-looking identifiers
        text = WORKER_SOURCE.replace("f0000000000000000000000000000005",
                                     "a1b2c3d4e5f60718293a4b5c6d7e8f90")
        text = text.replace("f0000000000000000000000000000007",
                            "b2c3d4e5f60718293a4b5c6d7e8f9a01")
        patched, _ = patch_worker(text)
        cache, trusted_index = self._make_stock_cache(tmp_path)
        patched = self._patch_fixture_paths(patched, cache, trusted_index)

        mod = self._make_patched_module(patched)
        terminal = tmp_path / "terminal"
        terminal.mkdir()

        result = mod.stage_fp6364_stock_compiler_assets(
            terminal,
            "b2c3d4e5f60718293a4b5c6d7e8f9a01",
            "90000008",
            "FPMarketsSC-Live",
            "LIVE",
        )
        assert result == "FP6364_VERIFIED_STOCK_EXPERT_COMPILER_CACHE_ONLY"
        staged = list((terminal / "MQL5").rglob("*.ex5"))
        assert len(staged) == 32


# --------------------------------------------------------------------------- #
# TestDryRun
# --------------------------------------------------------------------------- #
class TestDryRun:
    def test_dry_run_no_file_write(self, tmp_path):
        worker = tmp_path / "worker_feed_v8_fp_handoff.py"
        worker.write_text(WORKER_SOURCE)
        original = worker.read_text()

        import subprocess
        result = subprocess.run(
            [sys.executable, "ops/v9-cutover/reconcile_fp9392_compiler_assets.py",
             "--worker-path", str(worker), "--dry-run"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout
        assert worker.read_text() == original, "Dry run must not modify the file"

    def test_missing_file_exits_error(self, tmp_path):
        import subprocess
        result = subprocess.run(
            [sys.executable, "ops/v9-cutover/reconcile_fp9392_compiler_assets.py",
             "--worker-path", str(tmp_path / "nonexistent.py"), "--dry-run"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 1
        assert "not found" in result.stdout
