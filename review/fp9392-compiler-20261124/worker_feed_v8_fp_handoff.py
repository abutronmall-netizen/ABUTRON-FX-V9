# PUBLIC REVIEW COPY - account identifiers and long numeric literals anonymized; NOT FOR DEPLOYMENT.
#!/usr/bin/python3
"""ABUTRON client MT5 worker: isolated UID, broker-native feed plus gated order bridge.
MT5 credentials arrive via systemd's private runtime credential mount, not argv/env/CRM.
"""
import os,sys,json,subprocess,time,shutil,secrets,uuid,signal
from pathlib import Path
SRC=Path("/opt/abutron/client-mt5-template-v3")
WARM=Path("/opt/abutron/fpmarkets-mql-bootstrap-v1")
FP_SC_SERVERS=Path("/opt/abutron/fpmarkets-sc-servers-v1.dat")
FP_SC_SHA256="d5f0376015341141e6dc2a87ab1bb4b6d45c8bbb7c86b47e69a870d460358baa"
WINE="/opt/abutron/apex-wine-source/bin/wine"
FP_MODES={"FPMarketsSC-Demo":"DEMO","FPMarketsSC-Demo Server":"DEMO",
 "FPMarketsLtd-Demo2":"DEMO","FPMarketsSC-Live":"LIVE","FPMarketsLtd-Live2":"LIVE"}
ALLOWED={("FP Markets",server):"FP_MARKETS" for server in FP_MODES}
def status(path,obj):
 tmp=path.with_suffix(".tmp")
 tmp.write_text(json.dumps(obj,separators=(",",":")))
 tmp.chmod(0o600);os.replace(tmp,path)

def handoff_event(journal_bytes):
    """An MT5 updater handoff in exactly the newly appended journal range."""
    if not journal_bytes: return False
    try:
        text=journal_bytes.decode("utf-16-le",errors="replace") if b"\x00" in journal_bytes[:80] else journal_bytes.decode("utf-8",errors="replace")
    except UnicodeError:
        return False
    low=text.lower()
    return ("liveupdate" in low and "/update" in low and "terminal64.exe" in low)

def fp1622_mql_recompile_startup_handoff(journal_bytes):
    """Recognize exactly the current FP1622 MT5 compiler startup handoff.

    A clean terminal exit with full recompilation of fresh native sources is
    NOT broker authentication. Only the existing exact-nonce heartbeat can
    establish a connected session; retry remains bounded at the call site.
    """
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode("utf-16-le",errors="replace")
              if b"\x00" in journal_bytes[:100] else
              journal_bytes.decode("utf-8",errors="replace")).lower()
    except UnicodeError:return False
    return ("compiler\tfull recompilation has been started" in text
            and "terminal\texit with code 0" in text
            and "terminal\tstopped with 10053" in text)



def mt5_build_6295_detected(journal_bytes):
    # ABUTRON-FP-WORKER-CORRECTIONS-90000003
    # Detect MT5 build 6295 from the terminal journal. Build 6295 changed
    # the compiler handoff journal format and startup timing. This read-only
    # detection allows the worker to recognize build 6295 handoffs without
    # inferring any broker authentication.
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode("utf-16-le",errors="replace")
              if b"\x00" in journal_bytes[:100]
              else journal_bytes.decode("utf-8",errors="replace")).lower()
    except UnicodeError:return False
    return ("build 6295" in text
            or "mt5 terminal build 6295" in text
            or "compiler\tfull recompilation (build 6295)" in text)

def fp1622_mql_recompile_startup_handoff_6295(journal_bytes):
    # ABUTRON-FP-WORKER-CORRECTIONS-90000003
    # Build 6295 variant of the compiler handoff detection. Same bounded
    # retry semantics; never a broker ACK. Used as an alternative handoff
    # detection path in exit-code-69 and compiler-relaunch checks.
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode("utf-16-le",errors="replace")
              if b"\x00" in journal_bytes[:100]
              else journal_bytes.decode("utf-8",errors="replace")).lower()
    except UnicodeError:return False
    return (("compiler\tfull recompilation" in text or
             "compiler\tfull recompilation (build 6295)" in text)
            and "terminal\texit with code 0" in text
            and ("terminal\tstopped with 10053" in text
                 or "terminal\tstopped with 10054" in text
                 or "terminal\tstopped with 6295" in text))

def fp6364_invalid_account_journal(data):
 """Recognize this account's broker rejection, never infer an authenticated session."""
 if not data:return False
 try:
  decoded=(data.decode("utf-16-le",errors="replace") if b"\x00" in data[:100]
           else data.decode("utf-8",errors="replace"))
 except UnicodeError:return False
 return "'90000010': authorization on FPMarketsSC-Live failed (Invalid account)" in decoded

def cgroup_terminal_running():
    """Inspect only this systemd service cgroup; never probe another user's MT5."""
    try:
        lines=Path("/proc/self/cgroup").read_text().splitlines()
        names=[line.split("::",1)[1] for line in lines if line.startswith("0::")]
        if len(names)!=1 or "/abutron-client-mt5-" not in names[0]:
            return False
        group=Path("/sys/fs/cgroup")/names[0].lstrip("/")
        for p in (group/"cgroup.procs").read_text().splitlines():
            if not p.isdigit() or int(p)==os.getpid(): continue
            proc=Path("/proc")/p
            try:
                name=(proc/"comm").read_text().strip().lower()
                if name.startswith("terminal64"):return True
                # Wine may expose the launcher as preloader; verify its command line.
                if name in ("wine64-preloader","wine-preloader","wine64"):
                    argv=(proc/"cmdline").read_bytes().decode("utf-8","ignore").lower()
                    if "terminal64.exe" in argv:return True
            except (FileNotFoundError,ProcessLookupError,PermissionError):
                continue
    except (OSError,ValueError,IndexError):
        pass
    return False

def hold_zero_exit_for_native_proof(returncode,exact_fresh_heartbeat,seconds_since_exit):
    """Allow account-bound native proof to outlive a Wine launcher handoff.

    Never infer a session from exit code 0 or an old file. The caller may
    continue only while exact current-nonce EA proof stays fresh; without it,
    the grace is short and bounded. This does NOT authorize any order.
    """
    if returncode != 0 or seconds_since_exit < 0:
        return False
    if exact_fresh_heartbeat:
        return True
    return seconds_since_exit < 18.0

def exit_action(returncode,was_connected,attached,handoff,attempt,total_elapsed):
    """No restart without fresh MT5 updater evidence; no trading permission changes."""
    if attached:return "MONITOR_CHILD"
    if was_connected:return "STOP"
    if returncode==0 and handoff and attempt<3 and total_elapsed<220:
        return "RETRY_UPDATE"
    return "STOP"


FP1636_V101_HISTORY_SHA256="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"
FP0512_V101_HISTORY_SHA256="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"

def stage_fp1636_v101_history_binary(terminal,cid,server,mode):
 """Restore only the already-approved no-order V101 history indicator at restart.

 This only stages a file; MT5 must attach the indicator before history is proven.
 Never modify broker positions, orders, EA flags or another FP customer's files.
 """
 if cid not in ("f0000000000000000000000000000006","f0000000000000000000000000000002","f0000000000000000000000000000001") or server!="FPMarketsSC-Demo" or mode!="DEMO":
  return "SKIP_NON_APPROVED_FP_DEMO"
 import hashlib
 trusted=Path("/usr/local/lib/abutron-fp1636-v101/AbutronFPAccountHistoryV101.ex5" if cid=="f0000000000000000000000000000006" else "/usr/local/lib/abutron-fp1636-v101/AbutronFPAccountHistoryV101.ex5")
 expected=(FP1636_V101_HISTORY_SHA256 if cid=="f0000000000000000000000000000006" else FP0512_V101_HISTORY_SHA256)
 if (not trusted.is_file() or trusted.is_symlink() or
    hashlib.sha256(trusted.read_bytes()).hexdigest()!=expected):
  return "SKIP_UNTRUSTED_V101_BINARY"
 dest=terminal/"MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5"
 if dest.is_symlink():return "SKIP_SYMLINKED_HISTORY_DESTINATION"
 dest.parent.mkdir(parents=True,exist_ok=True)
 shutil.copy2(trusted,dest)
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
  return "SKIP_COPY_VERIFICATION_FAILED"
 return "FP1636_V101_BINARY_STAGED_NO_ORDER"



FP1636_V307_FEED_SHA256="459c84b607d0eed0ed7cbb06808b12dd7b1eadaabfaa6ffacb591074d0086c6c"
FP1636_V307_CHART_SHA256="1ffb07eb34d32bcb08fa69b572b93ef81ce6664c4eff32d4984d0e4b88ffd778"
FP1636_V305_DEFAULT_CHART_SHA256="b0b867ee5b2009dcd9c89e3001b69e23271e686d27634410d21278cb39ed6622"

def stage_fp1636_v307_feed(terminal,cid,server,mode):
 """Restore only exact FP1636 DEMO read-only V307 with V101 after-auth attachment."""
 if cid!="f0000000000000000000000000000006" or server!="FPMarketsSC-Demo" or mode!="DEMO":
  return "SKIP_NON_FP1636_DEMO"
 import hashlib
 fname="AbutronFPMarketsNativeFeedParallelV307_FP1636.ex5"
 trusted=Path("/usr/local/lib/abutron-fp1636-v101")/fname
 dest=terminal/"MQL5/Experts/ABUTRON"/fname
 if trusted.is_symlink() or dest.is_symlink() or not trusted.is_file():
  return "SKIP_FP1636_V307_UNTRUSTED_PATH"
 if hashlib.sha256(trusted.read_bytes()).hexdigest()!=FP1636_V307_FEED_SHA256:
  return "SKIP_FP1636_V307_HASH_MISMATCH"
 dest.parent.mkdir(parents=True,exist_ok=True)
 shutil.copy2(trusted,dest)
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=FP1636_V307_FEED_SHA256:
  return "SKIP_FP1636_V307_COPY_MISMATCH"
 return "FP1636_V307_READONLY_FEED_STAGED"

def stage_fp1636_v101_restart_chart(terminal,cid,server,mode):
 """Stage FP1636-only read-only V307 feed chart; MT5 EA attaches V101 post-auth."""
 if cid!="f0000000000000000000000000000006" or server!="FPMarketsSC-Demo" or mode!="DEMO":
  return "SKIP_NON_FP1636_DEMO"
 import hashlib
 src=Path("/usr/local/lib/abutron-fp1636-v101/AbutronFP1636V307ReadOnly.chart03.chr")
 profile=terminal/"MQL5/Profiles/Charts/Default/chart03.chr"
 window=terminal/"MQL5/Profiles/Charts/Default/order.wnd"
 indicator=terminal/"MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5"
 v307=terminal/"MQL5/Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV307_FP1636.ex5"
 if any(x.is_symlink() for x in (src,profile,window,indicator,v307)):
  return "SKIP_FP1636_PROFILE_SYMLINK"
 if not all(x.is_file() for x in (src,profile,window,indicator,v307)):
  return "SKIP_FP1636_PROFILE_MISSING"
 if hashlib.sha256(indicator.read_bytes()).hexdigest()!=FP1636_V101_HISTORY_SHA256:
  return "SKIP_FP1636_V101_EX5_NOT_VERIFIED"
 if hashlib.sha256(v307.read_bytes()).hexdigest()!=FP1636_V307_FEED_SHA256:
  return "SKIP_FP1636_V307_FEED_NOT_VERIFIED"
 if hashlib.sha256(profile.read_bytes()).hexdigest()!=FP1636_V305_DEFAULT_CHART_SHA256:
  return "SKIP_FP1636_BASE_PROFILE_NOT_ATTESTED"
 if hashlib.sha256(src.read_bytes()).hexdigest()!=FP1636_V307_CHART_SHA256:
  return "SKIP_FP1636_V307_PROFILE_NOT_ATTESTED"
 if "chart03.chr" not in window.read_bytes().decode("utf-16"):
  return "SKIP_FP1636_ORDER_WINDOW_NOT_ATTESTED"
 shutil.copy2(src,profile)
 if hashlib.sha256(profile.read_bytes()).hexdigest()!=FP1636_V307_CHART_SHA256:
  raise RuntimeError("FP1636_V307_CHART_COPY_UNVERIFIED")
 return "FP1636_V307_V101_POST_AUTH_AUTOATTACH_PROFILE_STAGED"



def stage_fp0512_v101_restart_chart(terminal,cid,server,mode):
 """FP0512 only: stage the approved V101 read-only history chart, never order chart01."""
 if (cid,server,mode)!=("f0000000000000000000000000000002","FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP0512"
 import hashlib
 approved=Path("/usr/local/lib/abutron-fp0512-v101-history/AbutronFP0512ReadOnlyV101.chart03.chr")
 chart=terminal/"MQL5/Profiles/Charts/Default/chart03.chr"
 indicator=terminal/"MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5"
 if any(x.is_symlink() for x in (approved,chart,indicator)) or not all(x.is_file() for x in (approved,chart,indicator)):
  return "SKIP_FP0512_HISTORY_PROFILE_MISSING"
 source_hash=hashlib.sha256(chart.read_bytes()).hexdigest()
 expected_hash="5b6f33d5df985227a59d44e76e54b8140f49d189bd9a722ce9dbee5bdd5aa843"
 if source_hash not in ("b0b867ee5b2009dcd9c89e3001b69e23271e686d27634410d21278cb39ed6622",expected_hash):
  return "SKIP_FP0512_READONLY_BASE_PROFILE_CHANGED"
 if (hashlib.sha256(approved.read_bytes()).hexdigest()!=expected_hash or
     hashlib.sha256(indicator.read_bytes()).hexdigest()!="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"):
  return "SKIP_FP0512_V101_RELEASE_UNATTESTED"
 shutil.copy2(approved,chart)
 if hashlib.sha256(chart.read_bytes()).hexdigest()!=expected_hash:
  raise RuntimeError("FP0512_V101_READONLY_PROFILE_INSTALL_FAILED")
 return "FP0512_V101_HISTORY_INDICATOR_PERSISTED_ON_READONLY_CHART03"


# Corrected V305 broker-native cost export for FP0512/1636 only.
# Reuse the real portable terminal directory and existing approved chart files.
FP_V305_COST_RELEASE=Path("/usr/local/lib/abutron-fp-v305-pr67-90000003")
FP_V305_COST_SHA256="c54c2aacbc5b063e780efb0f7144538c3121a80d06819f24f74bd5aea4bb5917"
FP_V305_COST_NAME="AbutronFPMarketsNativeFeedParallelV305"
FP_V305_COST_TARGETS={
 "f0000000000000000000000000000002":"0512",
 "f0000000000000000000000000000006":"1636",
}
FP_V305_COST_CHART_SOURCE={
 "0512":(Path("/usr/local/lib/abutron-fp0512-v101-history/AbutronFP0512ReadOnlyV101.chart03.chr"),
         "5b6f33d5df985227a59d44e76e54b8140f49d189bd9a722ce9dbee5bdd5aa843"),
 "1636":(Path("/usr/local/lib/abutron-fp1636-v101/AbutronFP1636V307ReadOnly.chart03.chr"),
         "1ffb07eb34d32bcb08fa69b572b93ef81ce6664c4eff32d4984d0e4b88ffd778"),
}

def stage_fp_v305_cost_feed(terminal,cid,server,mode):
 """Stage only the authenticated DEMO feed binary and existing chart03 before MT5 launch."""
 if cid not in FP_V305_COST_TARGETS or server!="FPMarketsSC-Demo" or mode!="DEMO":
  return "SKIP_NON_TARGET_DEMO"
 import hashlib,stat,tempfile
 suffix=FP_V305_COST_TARGETS[cid]
 root=terminal/"MQL5"
 chart=root/"Profiles/Charts/Default/chart03.chr"
 order=chart.parent/"order.wnd"
 dest=root/"Experts/ABUTRON"/(FP_V305_COST_NAME+".ex5")
 source=FP_V305_COST_RELEASE/(FP_V305_COST_NAME+".ex5")
 indicator=root/"Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5"
 template,template_sha=FP_V305_COST_CHART_SOURCE[suffix]
 sha=lambda b:hashlib.sha256(b).hexdigest()
 for p in (chart,order,dest,source,indicator,template):
  if p.is_symlink() or not p.is_file():
   raise RuntimeError("FP"+suffix+"_V305_FEED_ASSET_MISSING_OR_SYMLINK")
 payload=source.read_bytes()
 template_raw=template.read_bytes()
 if sha(payload)!=FP_V305_COST_SHA256 or sha(template_raw)!=template_sha:
  raise RuntimeError("FP"+suffix+"_V305_RELEASE_HASH_MISMATCH")
 if sha(indicator.read_bytes())!="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac":
  raise RuntimeError("FP"+suffix+"_V101_HISTORY_HASH_MISMATCH")
 if "chart03.chr" not in order.read_text(encoding="utf-16").splitlines():
  raise RuntimeError("FP"+suffix+"_V305_CHART03_NOT_IN_PROFILE")
 if suffix=="1636":
  old="AbutronFPMarketsNativeFeedParallelV307_FP1636".encode("utf-16-le")
  if template_raw.count(old)!=2:
   raise RuntimeError("FP1636_V307_CHART_REFERENCE_CHANGED")
  template_raw=template_raw.replace(old,FP_V305_COST_NAME.encode("utf-16-le"))
  if sha(template_raw)!="4e7c769fe43c23538b9f54fadd1991918d11f2131a3ce81db930a4d19bee89e9":
   raise RuntimeError("FP1636_V305_CONVERTED_CHART_HASH_MISMATCH")
 text=template_raw.decode("utf-16")
 if (text.count("<expert>")!=1 or text.count("name="+FP_V305_COST_NAME)!=1
     or text.count("path=Experts\\ABUTRON\\"+FP_V305_COST_NAME+".ex5")!=1
     or "AbutronFPNativeOrderBridge" in text
     or text.count("AbutronFPAccountHistoryV101.ex5")!=1):
  raise RuntimeError("FP"+suffix+"_V305_READONLY_CHART_INVALID")
 # Stage the signed V3 binary; the new V305 attaches it after authentication.
 # Preserve the genuine V101 chart layout; do not insert speculative indicator blocks.
 v3src=FP_V305_COST_RELEASE/"ApexMarketFeedV3.ex5"
 v3dest=root/"Indicators/ABUTRON/ApexMarketFeedV3.ex5"
 if v3src.is_symlink() or not v3src.is_file() or v3dest.is_symlink():
  raise RuntimeError("FP"+suffix+"_V3_INDICATOR_PATH_INVALID")
 v3data=v3src.read_bytes()
 if sha(v3data)!="321bc0d80b783f308a23b9823e4c3da95b138ea95c2e0f595a826fdd3c6f3d29":
  raise RuntimeError("FP"+suffix+"_V3_INDICATOR_HASH_MISMATCH")
 before_v3_sha=sha(template_raw)
 # Preserve the same verified baseline and account-specific persisted chart identities.
 allowed_chart={"16b43c6b397290cd48c4d44d4b1c747e17e7e73e1fe64692c5aa9b3065a6ed54",
                "b0b867ee5b2009dcd9c89e3001b69e23271e686d27634410d21278cb39ed6622",
                "9336c72582f50f4adee5a2d640a7bb3a2f4673d06575006c5e587d66d0797c1c",
                template_sha,before_v3_sha,sha(template_raw)}
 if suffix=="1636":
  allowed_chart.add("23e0ba087501ced3e639b9011e0e754ed03d8701b8049c5eda83e724f090bf41")
 original_chart=chart.read_bytes()
 original_binary=dest.read_bytes()
 if sha(original_chart) not in allowed_chart:
  raise RuntimeError("FP"+suffix+"_V305_BASE_CHART_CHANGED")
 if sha(original_binary) not in ("845c6a30034b2e0b6aa33625e6582b1a9130d568a8621ec980c4aca77455897e",
                                  "615ba5a53590a90400033a4259f1931a49157b2551b77cde483bc6861c3352b8",
                                  "7f2589e74bd22d59ea39d41c2690bacb02113de2f1fb6122d6a2926d396f7ed4",
                                  "e7e67f13a4b2d6969487c6901118247acce7b499d40f7bc77ce5b94ed8783907",
                                  FP_V305_COST_SHA256):
  raise RuntimeError("FP"+suffix+"_V305_BASE_BINARY_CHANGED")
 def atomic(path,data):
  info=path.stat()
  fd,tmp=tempfile.mkstemp(prefix=".v305-",dir=path.parent)
  try:
   with os.fdopen(fd,"wb") as f:
    f.write(data);f.flush();os.fsync(f.fileno())
    os.fchown(f.fileno(),info.st_uid,info.st_gid)
    os.fchmod(f.fileno(),stat.S_IMODE(info.st_mode))
    os.fsync(f.fileno())
   os.replace(tmp,path)
   dirfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(dirfd)
   finally:os.close(dirfd)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
 # Fresh portable startup profile may not contain the V3 binary yet.
 v3dest.parent.mkdir(parents=True,exist_ok=True)
 old_v3=v3dest.read_bytes() if v3dest.is_file() else None
 if v3dest.exists() and not v3dest.is_file():
  raise RuntimeError("FP"+suffix+"_V3_DESTINATION_NOT_REGULAR")
 # Only the feed binary, read-only indicator and Chart03 are changed.

 try:
  atomic(dest,payload)
  if old_v3 is None:
   fd,tmp=tempfile.mkstemp(prefix=".v3-",dir=v3dest.parent)
   try:
    with os.fdopen(fd,"wb") as vf:
     vf.write(v3data);vf.flush();os.fsync(vf.fileno())
     os.fchmod(vf.fileno(),0o644)
    os.replace(tmp,v3dest)
   finally:
    if os.path.exists(tmp):os.unlink(tmp)
  else:
   atomic(v3dest,v3data)
  atomic(chart,template_raw)
  if sha(dest.read_bytes())!=FP_V305_COST_SHA256 or chart.read_bytes()!=template_raw or sha(v3dest.read_bytes())!=sha(v3data):
   raise RuntimeError("FP"+suffix+"_V305_COPY_MISMATCH")
 except Exception:
  if dest.read_bytes()!=original_binary:atomic(dest,original_binary)
  if chart.read_bytes()!=original_chart:atomic(chart,original_chart)
  if old_v3 is None:v3dest.unlink(missing_ok=True)
  elif v3dest.read_bytes()!=old_v3:atomic(v3dest,old_v3)
  raise
 return "FP"+suffix+"_V305_COST_V101_FEED_CHART03_STAGED"

def stage_fp0512_v102_stop_risk(terminal,cid,server,mode):
    """Restore independent NO-ORDER broker stop-risk expert on chart04 at next MT5 boot.

    Scope ONLY FP0512 DEMO; preserve existing order bridge chart01 and feed
    chart03. This function never restarts terminals or submits broker orders.
    """
    if (cid,server,mode)!=("f0000000000000000000000000000002",
                           "FPMarketsSC-Demo","DEMO"):
        return "SKIP_NON_FP0512"
    import re,hashlib
    source=Path('/usr/local/lib/abutron-fp0512-risk-v102/AbutronFPStopRiskROV102.ex5')
    expected_sha='6e1bdc27a05777d299ca2ebf122c33e4bdb7768ca886473048e7e9212d80f1fd'
    if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected_sha:
        raise RuntimeError('FP0512_RISK_V102_BINARY_NOT_APPROVED')
    charts=terminal/'MQL5/Profiles/Charts/Default'
    template=charts/'chart03.chr'
    target=charts/'chart04.chr'
    order=charts/'order.wnd'
    binary=terminal/'MQL5/Experts/ABUTRON/AbutronFPStopRiskROV102.ex5'
    if any(x.is_symlink() for x in (template,target,order,binary)) or not all(x.is_file() for x in (template,order)) or (target.exists() and not target.is_file()):
        raise RuntimeError('FP0512_RISK_V102_CHART_TEMPLATE_INVALID')
    data=template.read_text(encoding='utf-16')
    blocks=re.findall(r'<expert>.*?</expert>',data,re.S)
    if len(blocks)!=1 or 'name=AbutronFPMarketsNativeFeedParallelV305' not in blocks[0] or data.count('id=92432578086933')!=1:
        raise RuntimeError('FP0512_RISK_CHART03_EXPECTATION_CHANGED')
    expert='<expert>\r\nname=AbutronFPStopRiskROV102\r\npath=Experts\\ABUTRON\\AbutronFPStopRiskROV102.ex5\r\nexpertmode=1\r\n<inputs>\r\n</inputs>\r\n</expert>'
    # Reuse only the chart shell; do not clone the V101 indicator to chart04.
    # Two writers of the same native V101 file would race and corrupt history.
    if data.count('<window>') != 1 or data.count('</window>') != 1:
        raise RuntimeError('FP0512_RISK_CHART03_INDICATOR_WINDOW_EXPECTATION_CHANGED')
    head=data.split('<window>',1)[0]
    if head.count('windows_total=1') != 1:
        raise RuntimeError('FP0512_RISK_CHART03_WINDOW_COUNT_UNEXPECTED')
    chart4=head.replace('windows_total=1','windows_total=0')
    chart4=chart4.replace('id=92432578086933','id=92432578086934').replace(blocks[0],expert)
    chart4+='</chart>\r\n'
    if ('AbutronFPNativeOrderBridge' in chart4 or
        'AbutronFPAccountHistoryV101' in chart4 or
        chart4.count('AbutronFPStopRiskROV102')!=2):
        raise RuntimeError('FP0512_RISK_CHART04_CONTAINS_UNRELATED_EXPERT_OR_INDICATOR')
    raw_order=order.read_text(encoding='utf-16')
    order_list=raw_order.splitlines()
    if order_list==['chart03.chr','chart02.chr']:
        order_list.insert(0,'chart01.chr')
    if order_list!=['chart01.chr','chart03.chr','chart02.chr']:
        raise RuntimeError('FP0512_ORDER_WND_UNEXPECTED')
    raw_order='\r\n'.join(order_list)+'\r\n' 
    binary.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=expected_sha:
        raise RuntimeError('FP0512_RISK_COPY_INTEGRITY_INVALID')
    target.write_text(chart4,encoding='utf-16')
    order.write_text(raw_order.rstrip('\r\n')+'\r\nchart04.chr\r\n',encoding='utf-16')
    return 'FP0512_V102_BROKER_NATIVE_ORDER_CALC_PROFIT_OBSERVER_CHART04_STAGED'

def stage_fp0512_single_writer_release(terminal,cid,server,mode):
 """FP0512 only: restore sole active Issue21 V118 order EA and protected read-only charts."""
 if (cid,server,mode)!=("f0000000000000000000000000000002","FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP0512"
 import hashlib
 root=Path("/usr/local/lib/abutron-fp0512-issue39-native-immutable-v118")
 folder=terminal/"MQL5"
 releases=(
  ("AbutronFPNativeOrderBridgeV101.ex5",
   "Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5",
   "cd2d30714beea560f128b43073334b34b2c57eaa937e8abb3723d656e03b3693"),
  ("AbutronFP0512OrderOn.chart01.chr",
   "Profiles/Charts/Default/chart01.chr",
   "228812d9a3d765b837810009bd8f22c9a752c65ee5976ea578805781ccf7f411"),
  ("AbutronFP0512NoDuplicateWriter.chart02.chr",
   "Profiles/Charts/Default/chart02.chr",
   "f7335d4789fcf4d2440aa87dcb92a48c42a57db8d20960228a41f5516930be00"))
 for name,relative,expected in releases:
  source=root/name
  target=folder/relative
  if (source.is_symlink() or not source.is_file() or
      target.is_symlink() or not target.parent.is_dir() or
      hashlib.sha256(source.read_bytes()).hexdigest()!=expected):
   raise RuntimeError("FP0512_RESTART_RELEASE_NOT_ATTESTED")
  shutil.copy2(source,target)
  if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
   raise RuntimeError("FP0512_RESTART_RELEASE_COPY_FAILED")
 return "FP0512_ORDER_BRIDGE_ISSUE21_V118_SINGLE_WRITER_CHART01_ON_CHART02_NONE"

# FP1622 DEMO: account-bound V307 no-order native feed attaches V101 history.
# No changes to other accounts' profile, licensed seats or trade switches.
FP1622_V307_SHA256="db6eb2f047ddf6a14ad55b3730852ca7f3dc80c62efeee9a4f18b9945ea47f26"
FP1622_HISTORY_V101_SHA256="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"
FP1622_ID="f0000000000000000000000000000001"
def stage_fp1622_v101_native_history(terminal,cid,server,mode):
 """1622-only verified binary + native indicator restart attachment, no broker orders."""
 if (cid,server,mode)!=(FP1622_ID,"FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP1622"
 import hashlib
 release=Path("/usr/local/lib/abutron-fp1622-v101")
 copies=(
  (release/"AbutronFPMarketsNativeFeedParallelV307_FP1622.ex5",
   terminal/"MQL5/Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV307_FP1622.ex5",
   FP1622_V307_SHA256),
  (release/"AbutronFPAccountHistoryV101.ex5",
   terminal/"MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5",
   FP1622_HISTORY_V101_SHA256))
 for src,dest,digest in copies:
  if src.is_symlink() or not src.is_file() or dest.is_symlink() or hashlib.sha256(src.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP1622_NATIVE_RELEASE_UNVERIFIED")
  dest.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(src,dest)
  if hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP1622_NATIVE_COPY_UNVERIFIED")
 base=Path("/usr/local/lib/abutron-fp1636-v101/AbutronFP1636V307ReadOnly.chart03.chr")
 chart=terminal/"MQL5/Profiles/Charts/Default/chart03.chr"
 if base.is_symlink() or chart.is_symlink() or not base.is_file() or not chart.is_file():
  raise RuntimeError("FP1622_CHART_PATH_INVALID")
 raw=base.read_bytes()
 if (hashlib.sha256(raw).hexdigest()!="1ffb07eb34d32bcb08fa69b572b93ef81ce6664c4eff32d4984d0e4b88ffd778" or
     hashlib.sha256(chart.read_bytes()).hexdigest()!="b0b867ee5b2009dcd9c89e3001b69e23271e686d27634410d21278cb39ed6622"):
  raise RuntimeError("FP1622_BASELINE_CHART_NOT_ATTESTED")
 old="AbutronFPMarketsNativeFeedParallelV307_FP1636".encode("utf-16-le")
 new="AbutronFPMarketsNativeFeedParallelV307_FP1622".encode("utf-16-le")
 if raw.count(old)!=2:raise RuntimeError("FP1622_CHART_V307_BASE_UNEXPECTED")
 data=raw.replace(old,new)
 chart.write_bytes(data)
 if chart.read_bytes()!=data:raise RuntimeError("FP1622_CHART_WRITE_UNVERIFIED")
 return "FP1622_V307_V101_NATIVE_HISTORY_STAGED_NO_ORDERS"

def stage_fp1622_v113_native_bridge(terminal,cid,server,mode):
 """Account-bound 1622 DEMO adaptive V113 bridge: CRM, broker and risk remain separate."""
 if (cid,server,mode)!=(FP1622_ID,"FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP1622_NATIVE_BRIDGE"
 import hashlib
 source=Path("/usr/local/lib/abutron-fp1636-native-risk-v113/AbutronFPNativeOrderBridgeV101.ex5")
 dest=terminal/"MQL5/Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5"
 expected="76f3503c1661642438e690239d667758f0211fe4790623541345051f0a728a1c"
 if source.is_symlink() or not source.is_file() or dest.is_symlink():
  raise RuntimeError("FP1622_NATIVE_BRIDGE_RELEASE_PATH_UNTRUSTED")
 if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP1622_NATIVE_V113_BINARY_HASH_UNVERIFIED")
 dest.parent.mkdir(parents=True,exist_ok=True)
 shutil.copy2(source,dest)
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP1622_NATIVE_V113_RESTORE_UNVERIFIED")
 return "FP1622_ADAPTIVE_V113_INSTALLED_FOR_BROKER_BOUND_DEMO"

def stage_fp1622_adaptive_input_for_demo(terminal,cid,server,mode):
 """Scope DEMO-only V113 input approval to licensed FP1622; native risk remains enforced."""
 if (cid,server,mode)!=(FP1622_ID,"FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP1622_ADAPTIVE_INPUT"
 import hashlib
 bridge=terminal/"MQL5/Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5"
 chart=terminal/"MQL5/Profiles/Charts/Default/chart01.chr"
 expected="76f3503c1661642438e690239d667758f0211fe4790623541345051f0a728a1c"
 if bridge.is_symlink() or not bridge.is_file() or hashlib.sha256(bridge.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP1622_NATIVE_V113_BEFORE_INPUT_UNVERIFIED")
 if chart.is_symlink():
  raise RuntimeError("FP1622_CHART_BEFORE_INPUT_SYMLINK")
 if not chart.is_file():
  source=Path("/usr/local/lib/abutron-fp1622-v101/AbutronFP1622Chart01Baseline.chr")
  expected_chart="11b5b819ebdd157e7ef1503dc194bc1416fa01aecf94df837d45a9631924b743"
  if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected_chart:
   raise RuntimeError("FP1622_NATIVE_CHART01_RELEASE_UNVERIFIED")
  chart.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(source,chart)
 raw=chart.read_bytes()
 old="AdaptiveV2ReconcilerApproved=false".encode("utf-16-le")
 new="AdaptiveV2ReconcilerApproved=true".encode("utf-16-le")
 if raw.count(old)!=1 or raw.count(new)!=0 or raw.count("EnableNativeDemoOrders=true".encode("utf-16-le"))!=1:
  raise RuntimeError("FP1622_SCOPED_ADAPTIVE_INPUT_CONTRACT_CHANGED")
 chart.write_bytes(raw.replace(old,new))
 if chart.read_bytes().count(new)!=1:
  raise RuntimeError("FP1622_SCOPED_ADAPTIVE_INPUT_WRITE_FAILED")
 return "FP1622_ADAPTIVE_V113_INPUT_STAGED_BROKER_GATES_STILL_REQUIRED"

def stage_fp1622_exclusive_writer_chart(terminal,cid,server,mode):
 """One V113 order bridge on chart01, read-only feed on chart02 for FP1622 only."""
 if (cid,server,mode)!=(FP1622_ID,"FPMarketsSC-Demo","DEMO"):
  return "SKIP_NON_FP1622_EXCLUSIVE_WRITER_CHART"
 import hashlib
 source=Path("/usr/local/lib/abutron-fp1622-v101/AbutronFP1622ReadOnlyChart02.chr")
 target=terminal/"MQL5/Profiles/Charts/Default/chart02.chr"
 expected="9336c72582f50f4adee5a2d640a7bb3a2f4673d06575006c5e587d66d0797c1c"
 if source.is_symlink() or not source.is_file() or target.is_symlink():
  raise RuntimeError("FP1622_READONLY_CHART_SOURCE_INVALID")
 data=source.read_bytes()
 if hashlib.sha256(data).hexdigest()!=expected:
  raise RuntimeError("FP1622_NATIVE_READONLY_CHART_HASH_UNVERIFIED")
 content=data.decode("utf-16")
 if "AbutronFPNativeOrderBridgeV101" in content or content.count("AbutronFPMarketsNativeFeedParallelV305")!=2:
  raise RuntimeError("FP1622_READONLY_FEED_CHART_HAS_ORDER_BRIDGE")
 target.parent.mkdir(parents=True,exist_ok=True)
 target.write_bytes(data)
 if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP1622_READONLY_CHART_COPY_NOT_VERIFIED")
 return "FP1622_EXCLUSIVE_V113_WRITER_CHART01_READONLY_V305_CHART02"

FP_HISTORY_READONLY_SHA256="5f1996c4a06b8ad29cf4dc615bebce356c3ba3d6e5e25d7b28abb19a966f5683"

def stage_fp_demo_history(terminal,files,nonce,challenge_owner,login,server,mode):
 """Prepare FP DEMO history proofs on first MT5 boot. Never attach, trade or override an EA.

 This optional add-on must never block the already-working broker data feed.
 The indicator is copied from the FP-only MQL5 bootstrap and its SHA is fixed.
 """
 if server!="FPMarketsSC-Demo" or mode!="DEMO":return "SKIP_NON_FP_DEMO"
 import hashlib
 indicator=terminal/"MQL5"/"Indicators"/"ABUTRON"/"AbutronFPAccountHistoryV100.ex5"
 if not indicator.is_file() or indicator.is_symlink() or hashlib.sha256(indicator.read_bytes()).hexdigest()!=FP_HISTORY_READONLY_SHA256:
  return "SKIP_FP_HISTORY_BINARY_UNVERIFIED"
 request=files/"ABUTRON_FP_HISTORY_REQUEST_V1.txt"
 if request.exists() or request.is_symlink():return "SKIP_FP_HISTORY_REQUEST_ALREADY_PRESENT"
 payload="\r\n".join(["ABUTRON_FP_HISTORY_REQUEST_V1",nonce,challenge_owner,
                         "FP_MARKETS",server,str(login),mode])+"\r\n"
 fd=os.open(str(request),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"w",encoding="ascii",newline="") as f:
  f.write(payload);f.flush();os.fsync(f.fileno())
 return "FP_HISTORY_REQUEST_STAGED_READONLY"

def stage_fp_live_6364_history(terminal,files,nonce,challenge_owner,login,server,mode,cid):
 """Pin the account-bound LIVE 6364 NO-ORDER history indicator and request only.
 Does not attach indicators, change charts, enable trading, or authorize orders.
 """
 if (cid,login,server,mode)!=("f0000000000000000000000000000005",
                              "90000010","FPMarketsSC-Live","LIVE"):
  return "SKIP_OTHER_ACCOUNT"
 import hashlib
 source=Path("/usr/local/lib/abutron-fp6364-usd-readonly-history-90000001-v1/AbutronFPLiveHistory6364.ex5")
 expected="66b46b95c962a69c0434975ac0f674016196463c793f27c07c2752f689b02896"
 dest=terminal/"MQL5/Indicators/ABUTRON/ABUTRON-FP-LIVE-USD-HISTORY-READONLY/AbutronFPLiveHistory6364.ex5"
 if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP6364_HISTORY_TRUSTED_RELEASE_MISSING")
 if dest.is_symlink():raise RuntimeError("FP6364_HISTORY_DESTINATION_SYMLINK")
 dest.parent.mkdir(parents=True,exist_ok=True)
 if not dest.exists():shutil.copy2(source,dest)
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP6364_HISTORY_INSTALL_HASH_MISMATCH")
 request=files/"ABUTRON_FP_HISTORY_REQUEST_V1.txt"
 payload="\r\n".join(["ABUTRON_FP_HISTORY_REQUEST_V1",nonce,challenge_owner,
                    "FP_MARKETS",server,login,mode])+"\r\n"
 if request.is_symlink():raise RuntimeError("FP6364_HISTORY_REQUEST_SYMLINK")
 if request.exists():
  if request.read_bytes()!=payload.encode("ascii"):raise RuntimeError("FP6364_HISTORY_REQUEST_IDENTITY_CONFLICT")
  return "FP6364_HISTORY_REQUEST_ALREADY_VALID"
 fd=os.open(request,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"wb") as h:
  h.write(payload.encode("ascii"));h.flush();os.fsync(h.fileno())
 return "FP6364_LIVE_READONLY_HISTORY_REQUEST_STAGED"

def stage_fp6364_stock_compiler_assets(terminal,cid,login,server,mode):
 """Seed only verified stock MT5 Expert examples to avoid repeated first-run
 recompilation. Does not copy any customer charts, EAs or broker state."""
 if (cid,login,server,mode)!=("f0000000000000000000000000000005",
                              "90000010","FPMarketsSC-Live","LIVE"):
  return "SKIP_UNRELATED_ACCOUNT"
 import hashlib,json,stat
 cache=Path("/usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1")
 index=cache/"sha256-manifest.json"
 trusted_index="a66bec82836ccaf0eb1c25ba70636b9d60be4d72d4910b6ed67d188db89ce687"
 if not index.is_file() or index.is_symlink() or hashlib.sha256(index.read_bytes()).hexdigest()!=trusted_index:
  raise RuntimeError("FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED")
 manifest=json.loads(index.read_text())["files"]
 if len(manifest)!=64 or sum(r.endswith(".ex5") for r in manifest)!=32:
  raise RuntimeError("FP6364_STOCK_MANIFEST_COUNT_INVALID")
 for relative,expected in sorted(manifest.items()):
  rel=Path(relative)
  if (rel.is_absolute() or ".." in rel.parts or len(rel.parts)<3 or
      rel.parts[:2] not in (("Experts","Advisors"),("Experts","Free Robots")) or
      rel.suffix not in (".mq5",".ex5")):
   raise RuntimeError("FP6364_STOCK_PATH_REJECTED")
  src=cache/rel;dest=terminal/"MQL5"/rel
  if src.is_symlink() or not src.is_file() or dest.is_symlink() or hashlib.sha256(src.read_bytes()).hexdigest()!=expected:
   raise RuntimeError("FP6364_STOCK_SHA256_REJECTED")
  dest.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(src,dest)
  if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
   raise RuntimeError("FP6364_STOCK_INSTALL_FAILED")
 return "FP6364_VERIFIED_STOCK_EXPERT_COMPILER_CACHE_ONLY"

def stage_fp_live_6364_preserved_profile(terminal,cid,login,server,mode):
 """Persist exact currently deployed FP6364 OFF bridge, plus read-only history.

 Strictly preserves existing broker Order EA inputs; does not set LIVE ON.
 """
 if (cid,login,server,mode)!=("f0000000000000000000000000000005",
                              "90000010","FPMarketsSC-Live","LIVE"):
  return "SKIP_OTHER_ACCOUNT"
 import hashlib
 root=Path("/usr/local/lib/abutron-fp6364-readonly-restart-profile-90000002-v1")
 targets={
 "chart02.chr":("MQL5/Profiles/Charts/Default/chart02.chr",
                "c8d369e76007caa9033b3b87dd8f7d6f923d670ec5b79615a5f8a3c1018944d6"),
 "order.wnd":("MQL5/Profiles/Charts/Default/order.wnd",
              "c3fd2e0ab9d69d935ceaa7ff43a8d37022a38c8779f70c23f1c4afef6307f527"),
 "AbutronFPLiveOrderBridge6364.ex5":(
            "MQL5/Experts/ABUTRON/AbutronFPLiveOrderBridge6364.ex5",
            "5d79d8562135fbbeb012b2b0a6e44250fc62f227e5219adaf1998c19779b4381"),
 }
 chart=root/"chart02.chr"
 text=chart.read_text("utf-16")
 if (text.count("<expert>")!=1 or
     text.count("name=AbutronFPLiveOrderBridge6364")!=1 or
     text.count("EnableNativeLiveOrders=false")!=1 or
     "EnableNativeLiveOrders=true" in text or
     text.count("AdaptiveV2ReconcilerApproved=false")!=1 or
     text.count("AbutronFPLiveHistory6364.ex5")!=1 or
     text.count("<indicator>")!=2):
  raise RuntimeError("FP6364_LIVE_CHART_PROTECTIVE_INPUTS_INVALID")
 if (root/"order.wnd").read_text("utf-16").splitlines()!=["chart02.chr"]:
  raise RuntimeError("FP6364_LIVE_PROFILE_WINDOW_ORDER_INVALID")
 for name,(rel,digest) in targets.items():
  src=root/name;dst=terminal/rel
  if (src.is_symlink() or not src.is_file() or src.stat().st_size>100000
      or hashlib.sha256(src.read_bytes()).hexdigest()!=digest
      or dst.is_symlink()):
   raise RuntimeError("FP6364_LIVE_PROFILE_RELEASE_INVALID")
  dst.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  shutil.copy2(src,dst)
  if hashlib.sha256(dst.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP6364_LIVE_PROFILE_COPY_MISMATCH")
 return "FP6364_LIVE_BRIDGE_OFF_AND_HISTORY_PROFILE_STAGED"

def stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid):
 """PINNED LIVE9392 broker-history only, no order calls or LIVE enablement."""
 if (cid,login,server,mode)!=("f0000000000000000000000000000007",
                              "90000008","FPMarketsSC-Live","LIVE"):
  return "SKIP_OTHER_ACCOUNT"
 import hashlib
 source=Path("/usr/local/lib/abutron-fp9392-live-readonly-native-90000002-v1/AbutronFPLiveHistory9392.ex5")
 expected="20d35b7cc302aa0f20ca35c62e58cced0762689bed503d60e011dc65b77db800"
 dest=terminal/"MQL5/Indicators/ABUTRON/AbutronFPLiveHistory9392.ex5"
 if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP9392_APPROVED_NO_ORDER_HISTORY_RELEASE_INVALID")
 if dest.is_symlink():raise RuntimeError("FP9392_HISTORY_DEST_SYMLINK")
 dest.parent.mkdir(parents=True,exist_ok=True)
 if not dest.exists():shutil.copy2(source,dest)
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
  raise RuntimeError("FP9392_HISTORY_INSTALL_HASH_MISMATCH")
 bundle=Path("/usr/local/lib/abutron-fp9392-live-fullrange-v105")
 pinned={"chart01.chr":("MQL5/Profiles/Charts/Default/chart01.chr","8310c61dc9ebe74d9ab15f7b62bf4d87558c02799d94d4f3e77c123e2b431a91"),
  "order.wnd":("MQL5/Profiles/Charts/Default/order.wnd","7a272f6660be5c8f64c91af0fc8b369a602431d9e2339c1bb00e2cd8f5d25509"),
  "AbutronFPLiveHistory9392FullRangeV105.ex5":("MQL5/Indicators/ABUTRON/AbutronFPLiveHistory9392FullRangeV105.ex5","0e5dc65e8dbab8749537dfc2093f38b57367ca75fb94cbecbf70e9e7b04e7790")}
 for name,(rel,digest) in pinned.items():
  source=bundle/name;target=terminal/rel
  if source.is_symlink() or target.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP9392_READONLY_PROFILE_SOURCE_INVALID")
  target.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(source,target)
  if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise RuntimeError("FP9392_READONLY_PROFILE_COPY_MISMATCH")
 if "<expert>" in (terminal/"MQL5/Profiles/Charts/Default/chart01.chr").read_text("utf-16"):
  raise RuntimeError("FP9392_READONLY_PROFILE_ORDER_EA_FORBIDDEN")
 # MT5 /profile only loads a dedicated named profile, not the staged Default.
 # The account-bound chart below is the MT5-native, user-saved no-order chart.
 named=terminal/"MQL5/Profiles/Charts/ABUTRON9392_READONLY"
 named.mkdir(parents=True,exist_ok=True)
 for name,digest in (("chart01.chr","8310c61dc9ebe74d9ab15f7b62bf4d87558c02799d94d4f3e77c123e2b431a91"),
                     ("order.wnd","7a272f6660be5c8f64c91af0fc8b369a602431d9e2339c1bb00e2cd8f5d25509")):
  src=bundle/"ABUTRON9392_READONLY"/name
  dst=named/name
  if src.is_symlink() or dst.is_symlink() or hashlib.sha256(src.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP9392_NAMED_PROFILE_PIN_INVALID")
  shutil.copy2(src,dst)
  if hashlib.sha256(dst.read_bytes()).hexdigest()!=digest:
   raise RuntimeError("FP9392_NAMED_PROFILE_COPY_INVALID")
 named_chart=(named/"chart01.chr").read_text("utf-16")
 if ("<expert>" in named_chart or named_chart.count("<indicator>")!=3 or
     named_chart.count("AbutronFPLiveHistory9392.ex5")!=1 or
     named_chart.count("AbutronFPLiveHistory9392FullRangeV105.ex5")!=1):
  raise RuntimeError("FP9392_NAMED_PROFILE_READONLY_INVALID")
 request=files/"ABUTRON_FP_HISTORY_REQUEST_V1.txt"
 payload="\r\n".join(["ABUTRON_FP_HISTORY_REQUEST_V1",nonce,challenge_owner,
                    "FP_MARKETS",server,login,mode])+"\r\n"
 if request.is_symlink():raise RuntimeError("FP9392_HISTORY_REQUEST_SYMLINK")
 if request.exists():
  if request.read_bytes()!=payload.encode("ascii"):raise RuntimeError("FP9392_HISTORY_REQUEST_IDENTITY_CONFLICT")
  return "FP9392_READONLY_HISTORY_ALREADY_STAGED"
 fd=os.open(request,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"wb") as h:
  h.write(payload.encode("ascii"));h.flush();os.fsync(h.fileno())
 return "FP9392_READONLY_HISTORY_STAGED"

FP_SIDECAR_READONLY_SHA256="55748a797f1d5d6298fb59d7c438add02051ee14ebb98ff4b729907fd082abe9"

def stage_fp_demo_sidecar(terminal,files,nonce,challenge_owner,login,server,mode):
 """FP DEMO 13x6 companion indicator request: evidence only, no trading."""
 if server!="FPMarketsSC-Demo" or mode!="DEMO":return "SKIP_NON_FP_DEMO"
 import hashlib
 sidecar=terminal/"MQL5"/"Indicators"/"ABUTRON"/"AbutronFPMarketsNativeSidecarV100.ex5"
 if not sidecar.is_file() or sidecar.is_symlink() or hashlib.sha256(sidecar.read_bytes()).hexdigest()!=FP_SIDECAR_READONLY_SHA256:
  return "SKIP_FP_SIDECAR_BINARY_UNVERIFIED"
 request=files/"ABUTRON_FP_SIDECAR_REQUEST_V1.txt"
 if request.exists() or request.is_symlink():return "SKIP_FP_SIDECAR_REQUEST_ALREADY_PRESENT"
 payload="\r\n".join(["ABUTRON_FP_SIDECAR_REQUEST_V1",nonce,challenge_owner,
                        "FP_MARKETS",server,str(login),mode])+"\r\n"
 fd=os.open(str(request),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"w",encoding="ascii",newline="") as f:
  f.write(payload);f.flush();os.fsync(f.fileno())
 return "FP_SIDECAR_REQUEST_STAGED_READONLY"

def attach_fp_demo_v9_writer(terminal, cid, server, login, mode):
    accounts = {"90000004":"f0000000000000000000000000000002",
                "90000006":"f0000000000000000000000000000006",
                "90000005":"f0000000000000000000000000000001",
                "90000007":"f0000000000000000000000000000004"}
    if mode != "DEMO" or server != "FPMarketsSC-Demo" or accounts.get(str(login)) != cid:
        return
    import hashlib
    import re
    charts = terminal / "MQL5/Profiles/Charts/Default"
    order = charts / "order.wnd"
    names = order.read_text("utf-16").splitlines()
    shell = (charts / "chart01.chr").read_text("utf-16")
    binary = terminal / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != "a311aeabfea50f00e062538983e4b884f63839628b756ea6346e2d740c49d029":
        raise RuntimeError("FP_V9_WRITER_BINARY_MISMATCH")
    chart = charts / "chart06.chr"
    if (not names or chart.name in names or chart.exists() or shell.count("<expert>") != 1
            or re.search(r"(?m)^symbol=EURUSD\.r$",shell) is None):
        raise RuntimeError("FP_V9_WRITER_CHART_LAYOUT_CHANGED")
    inputs = [
        "InpConnectionId=" + cid, "InpBrokerServer=" + server, "InpActivationSeed=0",
        "InpExposureOnly=false", "InpRiskFraction=0.20", "InpRiskFractionPct=20",
        "InpTrailingEnabled=true",
        "InpV3SymbolList=XAUUSD.r|BTCUSD|EURUSD.r|GBPUSD.r|USDJPY.r|AUDUSD.r|US100|US30|US500|GER40|XTIUSD|USDCHF.r|NZDUSD.r"]
    expert = ["<expert>", "name=ApexNativeWriter", r"path=Experts\ABUTRON\ApexNativeWriter.ex5",
              "expertmode=1", "<inputs>", *inputs, "</inputs>", "</expert>"]
    body = re.sub(r"<expert>.*?</expert>", lambda _: "\n".join(expert), shell, flags=re.S)
    body, changed = re.subn(r"(?m)^id=\d+$", "id=90000011" + str(login)[-4:], body, count=1)
    if changed != 1: raise RuntimeError("FP_V9_CHART_ID_MISSING")
    secret = terminal / "MQL5/Files/ABUTRON_ACTIVATION_SECRET.txt"
    with secret.open("x", encoding="ascii") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(secrets.token_hex(32) + "\n")
    preset = terminal / "MQL5/Presets/PR37WriterExposureOnly.set"
    preset.parent.mkdir(exist_ok=True)
    preset.write_text("\n".join(inputs) + "\n", encoding="utf-8")
    chart.write_text("\r\n".join(body.splitlines()) + "\r\n", encoding="utf-16")
    order.write_text("\r\n".join(names + [chart.name]) + "\r\n", encoding="utf-16")


def main():
 # Keep a single isolated Xvfb display alive across the ENTIRE MT5 update
 # lifecycle. xvfb-run shuts down its X server when the launched command exits;
 # it must wrap this supervisor, not each individual terminal launch.
 if os.environ.get("ABUTRON_MT5_XVFB_CHILD") != "1":
  # xvfb-run's default /tmp/xvfb-run.* cookie can be removed by tmpfiles
  # while the account's isolated MT5 X server remains alive. Store the X11
  # authorization cookie under systemd's per-account runtime directory instead.
  # Never print or persist the cookie in logs, DB or the shared MT5 template.
  runtime=Path(os.environ["RUNTIME_DIRECTORY"])
  if not runtime.is_dir() or runtime.is_symlink() or runtime.stat().st_uid!=os.geteuid():
   raise RuntimeError("FP X11 runtime directory identity not verified")
  authfile=runtime/"fp-mt5-xauthority"
  fd=os.open(str(authfile),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  os.close(fd)
  env=dict(os.environ)
  env["ABUTRON_MT5_XVFB_CHILD"]="1"
  # FP LIVE 90000009 alone: avoid occupied global X11 socket near :109.
  # All other accounts retain their original automatic Xvfb display selection.
  display_args=(["-a","-n","240"] if
                runtime.name=="abutron-client-mt5-f0000000000000000000000000000003"
                else ["-a","-n","241"] if runtime.name=="abutron-client-mt5-f0000000000000000000000000000004"
                else ["-a","-n","242"] if runtime.name=="abutron-client-mt5-f0000000000000000000000000000005"
                else ["-a"])
  cmd=["/usr/bin/xvfb-run",*display_args,"-f",str(authfile),"-s",
       "-screen 0 1280x800x24 -nolisten tcp",
       "/usr/bin/python3","-B",os.path.abspath(__file__)]
  try:
   if runtime.name=="abutron-client-mt5-f0000000000000000000000000000004":
    trace=Path(os.environ["STATE_DIRECTORY"])/"fp2582-startup-diagnostic.log"
    if trace.is_symlink():raise RuntimeError("FP2582_DIAG_SYMLINK_DENIED")
    with open(trace,"wb") as f:
     os.chmod(trace,0o600)
     return subprocess.run(cmd,env=env,stdin=subprocess.DEVNULL,
                          stdout=f,stderr=f,check=False).returncode
   return subprocess.run(cmd,env=env,stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         check=False).returncode
  finally:
   authfile.unlink(missing_ok=True)
 if not os.environ.get("DISPLAY"):
  raise RuntimeError("Isolated MT5 X display missing")
 state=Path(os.environ["STATE_DIRECTORY"]);runtime=Path(os.environ["RUNTIME_DIRECTORY"])
 cred=Path(os.environ["CREDENTIALS_DIRECTORY"])/"mt5"
 if not cred.is_file():raise RuntimeError("Missing private systemd credential")
 doc=json.loads(cred.read_text())
 if set(doc)!={"id","broker","server","login","password","mode"}:raise RuntimeError("Invalid broker credential payload")
 cid=doc["id"];broker=doc["broker"];server=doc["server"];login=doc["login"]
 mode=doc["mode"]
 if broker!="FP Markets" or len(cid)!=32 or (broker,server) not in ALLOWED or mode!=FP_MODES[server]:
  raise RuntimeError("Invalid broker identity")
 if not str(login).isdigit() or not str(doc["password"]):raise RuntimeError("Invalid MT5 credentials")
 terminal=state/"terminal";prefix=state/"wine";health=state/"worker-health.json"
 status(health,{"status":"STARTING","id":cid,"heartbeat_fresh":False})
 # The manager only starts this worker after the previous service is stopped.
 # Never reuse a failed terminal profile carrying stale or inherited broker cache.
 if terminal.exists():
  # MT5 can leave files not removable by the next isolated DynamicUser.
  # Preserve the stopped session without removing any broker journals.
  if terminal.is_symlink() or not terminal.is_dir():
   raise RuntimeError("UNSAFE_EXISTING_FP_TERMINAL")
  retired=state/("terminal-retired-"+str(time.time_ns()))
  terminal.rename(retired)
 shutil.copytree(SRC,terminal,symlinks=False)
 # Warm only stock MQL5 libraries; never copy a prior account profile, config, logs or state.
 if WARM.is_dir():
  for name in ("Include","Indicators","Experts/Examples","Scripts/Examples"):
   standard=WARM/name
   if standard.is_dir():shutil.copytree(standard,terminal/"MQL5"/name,dirs_exist_ok=True,symlinks=False)
 # Restore approved FP 0512/1636 read-only V101 history indicator to fresh DEMO terminals.
 # The active MT5 chart attachment remains independent and never authorizes orders.
 stage_fp1636_v101_history_binary(terminal,cid,server,mode)
 # FP account terminals must never contain the Deriv execution bridge binaries.
 experts=terminal/"MQL5"/"Experts"/"ABUTRON"
 if experts.is_dir():
  for binary in experts.glob("*.ex5"):binary.unlink()
 stage_fp6364_stock_compiler_assets(terminal,cid,login,server,mode)
 assert terminal.joinpath("terminal64.exe").is_file()
 # Exact FP Markets SC broker servers, taken from the official FPMarketsSC MT5 installer.
 # This is FP-only; the Deriv baseline and its broker server list remain untouched.
 if server.startswith("FPMarketsSC-"):
  import hashlib
  if not FP_SC_SERVERS.is_file() or hashlib.sha256(FP_SC_SERVERS.read_bytes()).hexdigest()!=FP_SC_SHA256:
   raise RuntimeError("FP Markets SC trusted server catalogue missing or altered")
  catalogue=FP_SC_SERVERS
  if (cid,login,server,mode)==("f0000000000000000000000000000005","90000010","FPMarketsSC-Live","LIVE"):
   catalogue=Path("/usr/local/lib/abutron-fpmarkets-sc-live-verified-90000002-servers.dat")
   if (not catalogue.is_file() or catalogue.is_symlink() or
       hashlib.sha256(catalogue.read_bytes()).hexdigest()!="6aa639fc8457188f541552adfd38cfdd8f38c69462eb191dac4301964bb0b3f1"):
    raise RuntimeError("FP6364_VERIFIED_SC_LIVE_CATALOGUE_INVALID")
  shutil.copy2(catalogue,terminal/"Config"/"servers.dat")
 assert terminal.joinpath("Config/servers.dat").is_file()
 # Update the read-only proof script even when reusing an existing client profile.
 # Otherwise failed accounts continue to run the older EX5 on their next attempt.
 # Pin FP-only broker-native feed to the currently proven 13x6 binary.
 # The shared MT5 template is older and must never silently replace it.
 import hashlib
 trusted=WARM/"Experts"/"ABUTRON"/"AbutronFPMarketsNativeFeedEA.ex5"
 if (not trusted.is_file() or trusted.is_symlink() or
     hashlib.sha256(trusted.read_bytes()).hexdigest()!="b4af8d9af21887d670bb5ea4963b15bfe756ac96218b95ede1c10dcf5681edcc"):
  raise RuntimeError("FP Markets 13x6 read-only native feed binary unavailable or altered")
 experts.mkdir(parents=True,exist_ok=True)
 shutil.copy2(trusted,experts/"AbutronFPMarketsNativeFeedEA.ex5")
 # Restore the exact approved FP DEMO feed, USD50/100 bridge and signed
 # research charts after the worker wipes the previous per-client terminal.
 # No credentials, broker logins or account permissions are copied.
 # The portal remains OFF until the separate native broker ACK approves DEMO.
 if server=="FPMarketsSC-Demo" and mode=="DEMO":
  try:
   bundle=Path("/opt/abutron/fpmarkets-0512-recovery-bundle-v1")
   approved={"Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV305.ex5": "845c6a30034b2e0b6aa33625e6582b1a9130d568a8621ec980c4aca77455897e", "Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5": "0c20f13984a57f67c40e3f23859bb61c356527be3936178891ad90e12e5bcf8b", "Profiles/Charts/Default/chart02.chr": "0ae283c83672e7af3d2603f9339b23ff4c8db97a339728bec70689b61f70ad19", "Profiles/Charts/Default/chart03.chr": "b0b867ee5b2009dcd9c89e3001b69e23271e686d27634410d21278cb39ed6622", "Profiles/Charts/Default/order.wnd": "5929212f2670a4b5030df33ae235223c74189fb4dd2bb93c3381f860c1dd83ca"}
   if bundle.is_symlink() or not bundle.is_dir():
    raise ValueError("FP_STARTUP_RECOVERY_BUNDLE_MISSING")
   for rel,digest in approved.items():
    src_file=bundle/rel
    if src_file.is_symlink() or not src_file.is_file() or src_file.stat().st_size>90000000:
     raise ValueError("FP_RECOVERY_SOURCE_NOT_TRUSTED")
    if hashlib.sha256(src_file.read_bytes()).hexdigest()!=digest:
     raise ValueError("FP_RECOVERY_HASH_MISMATCH")
    target=terminal/"MQL5"/rel
    if target.is_symlink():raise ValueError("FP_RECOVERY_DESTINATION_SYMLINK")
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(src_file,target)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
     raise ValueError("FP_RECOVERY_COPY_MISMATCH")
  except (OSError,ValueError,TypeError):
   # Preserve normal broker authentication and the original native feed.
   # The native order ACK independently fails closed if the bridge is absent.
   print("FP_DEMO_OPTIONAL_VERIFIED_RECOVERY_NOT_STAGED",file=sys.stderr)
 # FP0512/1636: stage corrected V305 cost feed on separate Chart03.
 # Authentication startup EA, other charts and existing trade controls are unchanged.
 stage_fp0512_v101_status=stage_fp_v305_cost_feed(terminal,cid,server,mode)
 if cid=="f0000000000000000000000000000006":
  print("FP1636_COST_FEED:"+stage_fp0512_v101_status,file=sys.stderr)
 stage_fp0512_release_status=stage_fp0512_single_writer_release(terminal,cid,server,mode)
 stage_fp0512_risk_status=stage_fp0512_v102_stop_risk(terminal,cid,server,mode)
 if cid=="f0000000000000000000000000000002":
  print("FP0512_HISTORY_CHART:"+stage_fp0512_v101_status+";RELEASE:"+stage_fp0512_release_status,file=sys.stderr)
 # Account-specific FP1622 read-only V101 history recovery, preserving its CRM OFF state.
 stage_fp1622_v101_native_history(terminal,cid,server,mode)
 # FP1622 only: restore pinned adaptive V113 without changing other FP accounts.
 stage_fp1622_v113_native_bridge(terminal,cid,server,mode)
 # Restore native DEMO V113 approved input to 1622 only (CRM OFF until verified).
 stage_fp1622_adaptive_input_for_demo(terminal,cid,server,mode)
 # Deduplicate FP1622 native order bridge: chart02 read-only, chart01 V113 sole writer.
 stage_fp1622_exclusive_writer_chart(terminal,cid,server,mode)
 # Account-specific FP1636 adaptive bridge: reuse the proven FP0512
 # native V113 release while keeping FP0512's running terminal untouched.
 # The MT5 challenge still binds every order to FP1636's own nonce/login.
 if cid=="f0000000000000000000000000000006" and server=="FPMarketsSC-Demo" and mode=="DEMO":
  import hashlib
  source=Path("/usr/local/lib/abutron-fp1636-native-risk-v113/AbutronFPNativeOrderBridgeV101.ex5")
  target=terminal/"MQL5/Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5"
  expected="76f3503c1661642438e690239d667758f0211fe4790623541345051f0a728a1c"
  if (source.is_symlink() or target.is_symlink() or not source.is_file()
      or hashlib.sha256(source.read_bytes()).hexdigest()!=expected):
   raise RuntimeError("FP1636_ADAPTIVE_NATIVE_RELEASE_NOT_VERIFIED")
  shutil.copy2(source,target)
  if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
   raise RuntimeError("FP1636_NATIVE_BRIDGE_COPY_INVALID")
 # Preserve the already-authorized FP DEMO order-EA input on fresh MT5 profiles.
 # MT5 generates chart01 after startup unless the verified account chart is staged.
 if (broker,server,mode)==("FP Markets","FPMarketsSC-Demo","DEMO") and cid in {
     "f0000000000000000000000000000002",
     "f0000000000000000000000000000006"}:
  import hashlib
  slot=("0512" if cid=="f0000000000000000000000000000002" else "1636")
  expected="909c9ad70f9bddace51768996d6d9e9fae2b9bde96225c52054080533012a0c6"
  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"
  if (not source.is_file() or source.is_symlink() or
      hashlib.sha256(source.read_bytes()).hexdigest()!=expected):
   raise RuntimeError("FP_DEMO_APPROVED_CHART_RELEASE_MISSING")
  chart=terminal/"MQL5/Profiles/Charts/Default/chart01.chr"
  if chart.is_symlink():raise RuntimeError("FP_DEMO_CHART_SYMLINK")
  chart.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(source,chart)
  if hashlib.sha256(chart.read_bytes()).hexdigest()!=expected:
   raise RuntimeError("FP_DEMO_APPROVED_CHART_NOT_INSTALLED")
 # FP1636: restore ONE native order writer; the shared FP recovery bundle's
 # chart02 contains a second EA with adaptive approval OFF, corrupting V2 proof.
 # The approved chart02 replacement is read-only and has a distinct chart ID.
 if (cid,server,mode)==("f0000000000000000000000000000006","FPMarketsSC-Demo","DEMO"):
  import hashlib
  ro=Path('/usr/local/lib/abutron-fp1636-single-writer-v1/chart02.chr')
  target=terminal/'MQL5/Profiles/Charts/Default/chart02.chr'
  chart1=terminal/'MQL5/Profiles/Charts/Default/chart01.chr'
  order=terminal/'MQL5/Profiles/Charts/Default/order.wnd'
  expected='b68b88e6b112370840d6f9f23388d84e23bcebf1ba1e306927f61bd163aeb25f'
  if any(v.is_symlink() for v in (ro,target,chart1,order)) or not all(v.is_file() for v in (ro,target,chart1,order)) or hashlib.sha256(ro.read_bytes()).hexdigest()!=expected:
   raise RuntimeError('FP1636_SINGLE_WRITER_PROFILE_RELEASE_INVALID')
  primary=chart1.read_text('utf-16')
  if primary.count('name=AbutronFPNativeOrderBridgeV101')!=1 or primary.count('AdaptiveV2ReconcilerApproved=true')!=1:
   raise RuntimeError('FP1636_PRIMARY_NATIVE_WRITER_NOT_APPROVED')
  shutil.copy2(ro,target)
  if hashlib.sha256(target.read_bytes()).hexdigest()!=expected or '<expert>' in target.read_text('utf-16'):
   raise RuntimeError('FP1636_DUPLICATE_WRITER_NOT_REMOVED')
  orderlines=order.read_text('utf-16').splitlines()
  if orderlines==['chart03.chr','chart02.chr']:
   orderlines.insert(0,'chart01.chr')
  if orderlines!=['chart01.chr','chart03.chr','chart02.chr']:
   raise RuntimeError('FP1636_CHART_ORDER_UNEXPECTED')
  order.write_text('\r\n'.join(orderlines)+'\r\n',encoding='utf-16')
 # FP DEMO 2582: verified V113 account-bound native bridge and separate V101
 # broker-history indicator; exactly one order writer; never touches LIVE.
 if (cid,server,mode)==("f0000000000000000000000000000004","FPMarketsSC-Demo","DEMO"):
  import hashlib
  release=Path("/usr/local/lib/abutron-fp2582-v113")
  pinned={
   "AbutronFPNativeOrderBridgeV101.ex5":("MQL5/Experts/ABUTRON/AbutronFPNativeOrderBridgeV101.ex5","76f3503c1661642438e690239d667758f0211fe4790623541345051f0a728a1c"),
   "AbutronFPAccountHistoryV101.ex5":("MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5","3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"),
   "chart01.chr":("MQL5/Profiles/Charts/Default/chart01.chr","057c64953bc39953440d36bc16dc908d1d70539a08a07b5b6ab94308dd957102"),
   "chart02.chr":("MQL5/Profiles/Charts/Default/chart02.chr","39bdfb605cfae35ad02b0cbb32f96494aeb14e4a90a34b467ece5d47ca529e68"),
   "AbutronFPMarketsNativeFeedParallelV305.ex5":("MQL5/Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV305.ex5","615ba5a53590a90400033a4259f1931a49157b2551b77cde483bc6861c3352b8"),
   "chart03.chr":("MQL5/Profiles/Charts/Default/chart03.chr","5b6f33d5df985227a59d44e76e54b8140f49d189bd9a722ce9dbee5bdd5aa843"),
   "order.wnd":("MQL5/Profiles/Charts/Default/order.wnd","249de3043f50fad845ce326f834b8e62d8f7ad9c35a09135fb49cf59fccc9243")}
  for filename,(rel,digest) in pinned.items():
   source=release/filename;dest=terminal/rel
   if (source.is_symlink() or not source.is_file() or dest.is_symlink()
       or hashlib.sha256(source.read_bytes()).hexdigest()!=digest):
    raise RuntimeError("FP2582_RELEASE_INTEGRITY_INVALID")
   dest.parent.mkdir(parents=True,exist_ok=True)
   shutil.copy2(source,dest)
   if hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:
    raise RuntimeError("FP2582_RELEASE_INSTALL_MISMATCH")
  chart1=(terminal/"MQL5/Profiles/Charts/Default/chart01.chr").read_text("utf-16")
  chart3=(terminal/"MQL5/Profiles/Charts/Default/chart03.chr").read_text("utf-16")
  chart2=(terminal/"MQL5/Profiles/Charts/Default/chart02.chr").read_text("utf-16")
  if (chart1.count("name=AbutronFPNativeOrderBridgeV101")!=1
      or chart1.count("AdaptiveV2ReconcilerApproved=true")!=1
      or "AbutronFPNativeOrderBridge" in chart2 or "<expert>" in chart2
      or "AbutronFPNativeOrderBridge" in chart3
      or chart3.count("name=AbutronFPMarketsNativeFeedParallelV305")!=1
      or chart3.count("AbutronFPAccountHistoryV101.ex5")!=1):
   raise RuntimeError("FP2582_SINGLE_WRITER_OR_HISTORY_PROFILE_INVALID")
 # Both terminals already attach the native V305 feed on an account-specific chart.
 if server=="FPMarketsSC-Demo" and mode=="DEMO" and (cid,str(login)) in (
     (FP1622_ID,"90000005"),("f0000000000000000000000000000004","90000007")):
  import hashlib
  for name,rel,expected in (
      (FP_V305_COST_NAME+".ex5","Experts/ABUTRON",FP_V305_COST_SHA256),
      ("ApexMarketFeedV3.ex5","Indicators/ABUTRON","321bc0d80b783f308a23b9823e4c3da95b138ea95c2e0f595a826fdd3c6f3d29")):
   src=FP_V305_COST_RELEASE/name
   dest=terminal/"MQL5"/rel/name
   if src.is_symlink() or dest.is_symlink() or hashlib.sha256(src.read_bytes()).hexdigest()!=expected:
    raise RuntimeError("FP_DEMO_NATIVE_FEED_RELEASE_MISMATCH")
   dest.parent.mkdir(parents=True,exist_ok=True)
   shutil.copyfile(src,dest)
   if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected:
    raise RuntimeError("FP_DEMO_NATIVE_FEED_INSTALL_MISMATCH")
 # Stage the V9 writer separately from account-specific chart activation.
 if mode=="DEMO" and server=="FPMarketsSC-Demo" and str(login) in ("90000004","90000006","90000005","90000007"):
  import hashlib
  writer=Path("/usr/local/lib/abutron-fp-writer-diagnostic-90000003/ApexNativeWriter.ex5")
  target=terminal/"MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
  if writer.is_symlink() or target.is_symlink() or hashlib.sha256(writer.read_bytes()).hexdigest()!="a311aeabfea50f00e062538983e4b884f63839628b756ea6346e2d740c49d029":
   raise RuntimeError("FP_V9_WRITER_RELEASE_MISMATCH")
  shutil.copyfile(writer,target)
 # The obsolete script is never used by the dedicated FP worker.
 (terminal/"MQL5"/"Scripts"/"ABUTRON"/"AbutronFPMarketsNativeFeedV2.ex5").unlink(missing_ok=True)
 files=terminal/"MQL5"/"Files";files.mkdir(parents=True,exist_ok=True)
 # Root-owned systemd order-preflight binds an account-specific order folder.
 # Prepare it before starting MT5, under this account's isolated DynamicUser.
 # Directories alone cannot enable trading; broker/AI/SMC/ICT gates remain.
 if server=="FPMarketsSC-Demo" and mode=="DEMO":
  for sub in ("ABUTRON_FP","ABUTRON_FP/receipts","ABUTRON_FP/evidence"):
   folder=files/sub
   if folder.is_symlink():raise RuntimeError("FP account order folder is symlinked")
   folder.mkdir(mode=0o700,parents=True,exist_ok=True)
   if folder.stat().st_uid!=os.geteuid():raise RuntimeError("FP account order folder owner invalid")
 def new_challenge():
  nonce=secrets.token_hex(16);challenge_owner=str(uuid.uuid4())
  challenge=files/"ABUTRON_MT5_CUSTOMER_CHALLENGE_V6.txt"
  challenge.write_text("\r\n".join(["ABUTRON_MT5_CUSTOMER_V6",nonce,challenge_owner,
   ALLOWED[(broker,server)],login,server,mode])+"\r\n")
  return nonce,challenge_owner
 nonce,challenge_owner=new_challenge()
 # Staged history indicator remains read-only and cannot alter native feed EA.
 # Do not break a broker login if an optional diagnostic binary is unavailable.
 try:
  stage_fp_demo_history(terminal,files,nonce,challenge_owner,login,server,mode)
  stage_fp_live_6364_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)
  stage_fp_live_6364_preserved_profile(terminal,cid,login,server,mode)
  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)
 except (OSError,ValueError,UnicodeError,RuntimeError):
  print("FP_HISTORY_READONLY_ADDON_NOT_STAGED",file=sys.stderr)
 try:
  stage_fp_demo_sidecar(terminal,files,nonce,challenge_owner,login,server,mode)
 except (OSError,ValueError,UnicodeError):
  print("FP_SIDECAR_READONLY_ADDON_NOT_STAGED",file=sys.stderr)
 attach_fp_demo_v9_writer(terminal,cid,server,login,mode)
 auth=runtime/"auth.ini"
 def write_auth(config):
  auth.write_text("[Common]\r\nLogin="+login+"\r\nPassword="+config["password"]+
   "\r\nServer="+server+"\r\nKeepPrivate=0\r\nProxyEnable=0\r\n"+
   "[Experts]\r\nEnabled=1\r\nAllowLiveTrading="+("1" if broker=="FP Markets" and mode=="DEMO" else "0")+"\r\nAllowDllImport=0\r\n"+
   "[StartUp]\r\nExpert=ABUTRON\\AbutronFPMarketsNativeFeedEA\r\n"+
   "Symbol=EURUSD"+
   "\r\nPeriod=M1\r\nShutdownTerminal=0\r\n")
  auth.chmod(0o600)
 write_auth(doc)
 doc["password"]=None
 env={**os.environ,"HOME":str(state),"WINEPREFIX":str(prefix),
    "WINEARCH":"win64","WINEDEBUG":"-all",
    "WINEDLLOVERRIDES":"mscoree,mshtml="}
 exe=terminal/"terminal64.exe"
 winconfig="Z:"+str(auth).replace("/","\\")
 args=[WINE,str(exe),"/portable","/config:"+winconfig]
 if (cid,login,server,mode)==("f0000000000000000000000000000007","90000008","FPMarketsSC-Live","LIVE"):
  named_profile=terminal/"MQL5/Profiles/Charts/ABUTRON9392_READONLY/chart01.chr"
  if not named_profile.is_file() or "<expert>" in named_profile.read_text("utf-16"):
   raise RuntimeError("FP9392_NAMED_STARTUP_PROFILE_MISSING_OR_UNSAFE")
  args.append("/profile:ABUTRON9392_READONLY")
 # MT5 may exit with code 0 to hand off an automatic LiveUpdate process.
 # Restart only if this specific event appears in the current launch's journal.
 # The official Python IPC bridge is not used. The native EA only accepts authorized per-account requests.
 heartbeat=files/("ABUTRON_SESSION_HEARTBEAT_V1_"+nonce+".txt")
 proof={"nonce":nonce,"proof_owner":challenge_owner,"broker":broker,"server":server,"mode":mode}
 started=time.monotonic();last_ok=0.0;was_connected=False
 process=None
 journal=terminal/"logs"/(time.strftime("%Y%m%d")+".log")
 try:
  # Bounded startup attempts; one session nonce, challenge and credential mount.
  # The Wine launcher may exit while LiveUpdate hands the real MT5 terminal
  # to a child within THIS systemd cgroup. Follow it without spawning another.
  attempt=-1
  fp1622=(cid,login,server,mode)==("f0000000000000000000000000000001","90000005","FPMarketsSC-Demo","DEMO")
  if fp1622:
   charts=terminal/"MQL5/Profiles/Charts/Default"
   launch_charts=runtime/"fp1622-startup-charts"
   shutil.copytree(charts,launch_charts)
  while attempt < 3:
   attempt+=1
   launched_at=time.time()
   previous_bytes=journal.stat().st_size if journal.is_file() else 0
   process=subprocess.Popen(args,env=env,cwd=str(state),stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=False)
   post_exit_since=None
   while True:
    elapsed=time.monotonic()-started
    if heartbeat.is_file():
     try:lines=heartbeat.read_text(encoding="utf-8-sig").splitlines()
     except (OSError,UnicodeError):lines=[]
     try:fresh=time.time()-heartbeat.stat().st_mtime<13
     except OSError:fresh=False
     expected=["ABUTRON_MT5_NATIVE_HEARTBEAT_V1",nonce,challenge_owner,
      ALLOWED[(broker,server)],server,mode,"CONNECTED","NO_ORDER_FUNCTIONS"]
     authenticated=bool(fresh and lines==expected and (not fp1622 or heartbeat.stat().st_mtime>=launched_at))
    else:authenticated=False
    # Account-bound, current-launch broker rejection; never publish a stale
    # heartbeat as CONNECTED or wait 240 seconds after definitive denial.
    if (cid=="f0000000000000000000000000000005" and
        broker=="FP Markets" and login=="90000010" and
        server=="FPMarketsSC-Live" and mode=="LIVE" and
        not authenticated and not was_connected):
     try:
      denied_log=(journal.read_bytes()[previous_bytes:]
                  if journal.is_file() and journal.stat().st_size<250000 else b"")
     except OSError:denied_log=b""
     if fp6364_invalid_account_journal(denied_log):
      status(health,{**proof,"status":"BROKER_AUTH_INVALID_ACCOUNT",
         "id":cid,"heartbeat_fresh":False})
      return 3
    if authenticated:
     was_connected=True;last_ok=time.monotonic()
     if auth.exists():auth.unlink()
     status(health,{**proof,"status":"CONNECTED","id":cid,
        "heartbeat_fresh":True,"verified_at":int(time.time())})
    elif was_connected:
     status(health,{**proof,"status":"BROKER_SESSION_LOST","id":cid,
        "heartbeat_fresh":False})
    elif elapsed<240:
     status(health,{**proof,"status":"CONNECTING","id":cid,"heartbeat_fresh":False})
    else:
     status(health,{**proof,"status":"AUTH_TIMEOUT","id":cid,"heartbeat_fresh":False})
     return 2
    if process.poll() is None:
     post_exit_since=None
     time.sleep(2)
     continue
    if cgroup_terminal_running():
     # Wine's original Popen returned but the MT5 terminal is still alive.
     # Never detach this worker or mark Connected without fresh native proof.
     post_exit_since=None
     time.sleep(2)
     continue
    # A bounded grace allows the Wine updater to spawn its child after the
    # launcher exits. This must not be mistaken for a successful broker login.
    if post_exit_since is None:post_exit_since=time.monotonic()
    # MT5 may authorize the broker and publish its 3-second native heartbeat
    # after Wine's launcher has already returned 0. The original 12-second
    # handoff window exited even as the genuine broker heartbeat arrived.
    # Follow the exact account/session proof; if it stops renewing, fail closed.
    if hold_zero_exit_for_native_proof(process.returncode,authenticated,
                                       time.monotonic()-post_exit_since):
     time.sleep(1)
     continue
    break
   if fp1622 and was_connected and process.returncode==0 and not cgroup_terminal_running():
    status(health,{**proof,"status":"CONNECTING","id":cid,"heartbeat_fresh":False,
                   "restart_reason":"AUTHENTICATED_TERMINAL_EXITED"})
    config=json.loads(cred.read_text())
    if (config.get("id"),str(config.get("login")),config.get("server"),config.get("mode"))!=(cid,login,server,mode):
     raise RuntimeError("FP1622_RECONNECT_CREDENTIAL_IDENTITY_CHANGED")
    archived=state/"ended-feed-sessions"/nonce
    archived.mkdir(parents=True,mode=0o700,exist_ok=False)
    # Restored V101 indicators can initialize before broker login and then
    # suppress the V307 host's post-auth attach. Reuse this session's known
    # startup charts. Keep the just-closed terminal's saved charts intact.
    charts.rename(archived/"saved-charts")
    shutil.copytree(launch_charts,charts)
    for src in files.iterdir():
     if (nonce in src.name or src.name in ("ABUTRON_FP_HISTORY_REQUEST_V1.txt",
             "ABUTRON_FP_SIDECAR_REQUEST_V1.txt","ABUTRON_MT5_CUSTOMER_CHALLENGE_V6.txt")):
      if not src.is_file() or src.is_symlink():raise RuntimeError("FP1622_SESSION_FILE_INVALID")
      src.rename(archived/src.name)
    nonce,challenge_owner=new_challenge()
    heartbeat=files/("ABUTRON_SESSION_HEARTBEAT_V1_"+nonce+".txt")
    proof=dict(proof,nonce=nonce,proof_owner=challenge_owner)
    stage_fp_demo_history(terminal,files,nonce,challenge_owner,login,server,mode)
    stage_fp_demo_sidecar(terminal,files,nonce,challenge_owner,login,server,mode)
    write_auth(config)
    config["password"]=None
    started=time.monotonic();was_connected=False;attempt=-1
    time.sleep(3)
    continue
   if was_connected or attempt>=3 or time.monotonic()-started>=220:
    break
   try:
    data=journal.read_bytes()
    tail=data[previous_bytes:] if len(data)>previous_bytes else b""
   except OSError:
    tail=b""
   # FP6364 LIVE's Wine launcher reports 69 while its signed-session MT5
   # journal records the specific orderly full-recompile handoff. Only this
   # one existing OFF account may retry, never infer authentication from 69.
   fp6364_compiler_exit69=(
       cid=="f0000000000000000000000000000005"
       and broker=="FP Markets" and server=="FPMarketsSC-Live"
       and login=="90000010" and mode=="LIVE"
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
   # FP DEMO accounts: the Wine launcher may report exit code 69 during
   # the MT5 compiler full-recompilation handoff, same as FP6364 LIVE.
   # Bounded by the same compiler-handoff journal detection. Never infer
   # broker authentication from exit code 69. Uses in-scope broker/mode
   # variables — no hardcoded account identifiers.
   fp_demo_compiler_exit69=(
       broker=="FP Markets" and mode=="DEMO"
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and (fp1622_mql_recompile_startup_handoff(tail)
            or fp1622_mql_recompile_startup_handoff_6295(tail)))

   # FP LIVE read-only accounts (9392): same compiler-handoff exit
   # code 69 as FP6364 LIVE. Scoped to accounts configured for read-only
   # operation via the ABUTRON9392_READONLY profile marker in args —
   # not a platform-wide LIVE change. Bounded by the same journal
   # detection. Never infer broker authentication from exit code 69.
   fp_live_compiler_exit69=(
       broker=="FP Markets" and mode=="LIVE"
       and "ABUTRON9392_READONLY" in " ".join(args)
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and (fp1622_mql_recompile_startup_handoff(tail)
            or fp1622_mql_recompile_startup_handoff_6295(tail)))

   if process.returncode!=0 and not (fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69):
    break
   # FP DEMO 1622's signed first-run recompilation exits cleanly before
   # native broker login. Permit only a bounded same-worker retry. Never treat
   # this event as a broker ACK or enable unverified orders.
   trusted_fp1622_compiler_relaunch=(
       cid=="f0000000000000000000000000000001"
       and broker=="FP Markets" and server=="FPMarketsSC-Demo"
       and mode=="DEMO" and process.returncode==0
       and not was_connected and attempt<2
       and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
   # FP6364 LIVE: same exact first-run MT5 compiler handoff, not a
   # native broker ACK. Bound to this one account and retain its
   # pinned EnableNativeLiveOrders=false and CRM LIVE OFF switches.
   trusted_fp6364_compiler_relaunch=(
       cid=="f0000000000000000000000000000005"
       and broker=="FP Markets" and server=="FPMarketsSC-Live"
       and login=="90000010" and mode=="LIVE"
       and process.returncode in (0,69) and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
   decision=exit_action(0 if fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69 else process.returncode,was_connected,
              cgroup_terminal_running(),
              handoff_event(tail) or trusted_fp1622_compiler_relaunch or trusted_fp6364_compiler_relaunch or fp_demo_compiler_exit69 or fp_live_compiler_exit69,
              attempt,time.monotonic()-started)
   if decision=="MONITOR_CHILD":
    # A late child appeared after the launcher exit. Monitor its proof using
    # the same exact account/session nonce; never spawn a competing terminal.
    while cgroup_terminal_running() and ((fp1622 and was_connected) or time.monotonic()-started<240):
     if heartbeat.is_file():
      try:
       lines=heartbeat.read_text(encoding="utf-8-sig").splitlines()
       fresh=time.time()-heartbeat.stat().st_mtime<13
      except (OSError,UnicodeError):
       lines=[];fresh=False
      expected=["ABUTRON_MT5_NATIVE_HEARTBEAT_V1",nonce,challenge_owner,
         ALLOWED[(broker,server)],server,mode,"CONNECTED","NO_ORDER_FUNCTIONS"]
      if fresh and lines==expected:
       was_connected=True
       if auth.exists():auth.unlink()
       status(health,{**proof,"status":"CONNECTED","id":cid,
              "heartbeat_fresh":True,"verified_at":int(time.time())})
      else:
       status(health,{**proof,"status":"BROKER_SESSION_LOST" if was_connected else "CONNECTING",
          "id":cid,"heartbeat_fresh":False})
     time.sleep(2)
    break
   if decision!="RETRY_UPDATE":break
   status(health,{**proof,"status":"CONNECTING","id":cid,
          "heartbeat_fresh":False,"restart_reason":"MT5_LIVE_UPDATE","attempt":attempt+1})
   time.sleep(4)
  status(health,{**proof,"status":"PROCESS_EXITED","id":cid,"heartbeat_fresh":False,
                 "process_exit_code":process.returncode,
                 "authenticated_native_proof_seen":was_connected})
  return 1
 finally:
  auth.unlink(missing_ok=True)
  if process is not None and process.poll() is None:
   process.terminate()
   try:process.wait(timeout=7)
   except subprocess.TimeoutExpired:process.kill()
if __name__=="__main__":
 try:sys.exit(main() or 0)
 except Exception as exc:
  if os.environ.get("RUNTIME_DIRECTORY","").endswith("abutron-client-mt5-f0000000000000000000000000000004"):
   import re
   msg=str(exc)
   safe=msg if re.fullmatch(r"[A-Z][A-Z0-9_]{3,100}",msg) else type(exc).__name__
   print("FP2582_BOOT_FAILURE:"+safe,file=sys.stderr)
  print("ISOLATED_MT5_WORKER_FAILED",file=sys.stderr)
  sys.exit(1)
