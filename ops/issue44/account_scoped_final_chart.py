"""Issue #49 — make an account-specific final chart overlay auditable.

Public, account-neutral source transformer. It does not deploy a worker,
restart MT5, copy an EA, read credentials, or change CRM/order authority.
An authorized operator supplies the account ID and exact source SHA pins
through a *root-private* deployment manifest; never commit that manifest.

The private production worker has a two-stage chart restore: account-specific
EA restaging followed by a shared FP DEMO chart overlay. Leaving the second
stage unchanged silently drops newly enabled native stop-trailing inputs.
"""
from __future__ import annotations
import ast
import hashlib
import re

class ChartOverrideError(ValueError): pass

OLD_PIN="909c9ad70f9bddace51768996d6d9e9fae2b9bde96225c52054080533012a0c6"
NEW_ISSUE44_CHART_PIN="d0e5acaf81e30d281a338a4dda7ac9a797f35d8e31c3cc8d712b868bd94838dc"

def sha(source:str)->str:
    return hashlib.sha256(source.encode()).hexdigest()

def apply_fp_demo_single_account_overlay(
    source:str, *,
    target_connection_id:str,
    new_chart_path:str,
    expected_original_worker_sha256:str,
    candidate_chart_sha256:str,
) -> str:
    """Create *source text* for an account-specific overlay, never execute it.

    Reject unknown baseline, previously modified workers, ambiguous anchors
    and attempts to alter any non-target FP DEMO chart or LIVE account.
    """
    if not isinstance(source,str) or sha(source)!=expected_original_worker_sha256:
        raise ChartOverrideError("WORKER_BASE_SHA_NOT_ATTESTED")
    if (not isinstance(target_connection_id,str) or
        re.fullmatch(r"[0-9a-f]{32}",target_connection_id) is None or
        candidate_chart_sha256!=NEW_ISSUE44_CHART_PIN or
        not isinstance(new_chart_path,str) or
        re.fullmatch(r"/usr/local/lib/[a-zA-Z0-9_./-]+\.chr",new_chart_path) is None or
        ".." in new_chart_path):
        raise ChartOverrideError("INVALID_PRIVATE_ACCOUNT_OR_RELEASE_PIN")
    a='''  slot=("0512" if cid=="'''+target_connection_id+'''" else "1636")
  expected="'''+OLD_PIN+'''"
  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"'''
    b='''  # Account-specific final chart overlay: preserve the signed Issue44
  # Chandelier-enabled profile. Other DEMO accounts retain the old pin.
  if cid=="'''+target_connection_id+'''":
   expected="'''+NEW_ISSUE44_CHART_PIN+'''"
   source=Path("'''+new_chart_path+'''")
  else:
   expected="'''+OLD_PIN+'''"
   source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1/1636/chart01.chr")'''
    if source.count(a)!=1:
        raise ChartOverrideError("AMBIGUOUS_OR_ALREADY_MODIFIED_FINAL_CHART_OVERLAY")
    if source.count(OLD_PIN)!=1 or source.count("stage_fp0512_single_writer_release(")<2:
        raise ChartOverrideError("UNEXPECTED_ACCOUNT_RESTAGER_CONTRACT")
    candidate=source.replace(a,b)
    if (candidate.count(NEW_ISSUE44_CHART_PIN)!=1 or
        candidate.count(OLD_PIN)!=1 or
        candidate.count(new_chart_path)!=1):
        raise ChartOverrideError("FINAL_CHART_PIN_CONTRACT_INVALID")
    ast.parse(candidate)
    # Caller must independently verify the candidate against a real signed
    # chart, exact other-account rollback source and identity-specific tests.
    return candidate
