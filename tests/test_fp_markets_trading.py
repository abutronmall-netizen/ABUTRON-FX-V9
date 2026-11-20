"""Tests for the FP Markets DEMO trading deployment path.

Verifies that:
1. DemoNativeCRM accepts a broker parameter (Deriv default, FP Markets).
2. The FP Markets DEMO production runner structure is correct.
3. The FP Markets DEMO enablement script structure is correct.
4. The FP runner resolves FP Markets CRM connections (not Deriv).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
FP_RUNNER = REPO_ROOT / "src/abutron/agent/production_fp_demo.py"
FP_ENABLE = REPO_ROOT / "ops/v9-cutover/enable_trading_fp_demo.py"
CRM_BINDING = REPO_ROOT / "src/abutron/agent/crm_native_binding.py"
PRESET_PATH = REPO_ROOT / "ops/v9-cutover/PR37WriterExposureOnly.set"


# --------------------------------------------------------------------- #
# DemoNativeCRM broker generalization
# --------------------------------------------------------------------- #

def test_demo_native_crm_accepts_broker_param():
    """DemoNativeCRM.__init__ must accept a broker parameter."""
    src = CRM_BINDING.read_text(encoding="utf-8")
    assert "broker: str = \"Deriv\"" in src
    assert "self.broker = broker" in src


def test_demo_native_crm_validates_broker():
    """DemoNativeCRM must reject unsupported brokers."""
    src = CRM_BINDING.read_text(encoding="utf-8")
    assert 'broker not in ("Deriv", "FP Markets")' in src
    assert "UNSUPPORTED_BROKER" in src


def test_demo_native_crm_snapshot_uses_self_broker():
    """snapshot() must check row['broker'] == self.broker, not hardcoded."""
    src = CRM_BINDING.read_text(encoding="utf-8")
    assert 'row["broker"] == self.broker' in src
    # Must NOT hardcode Deriv in the binding check
    assert 'row["broker"] == "Deriv"' not in src


def test_demo_native_crm_account_uses_self_broker():
    """The account dict must use self.broker, not hardcoded Deriv."""
    src = CRM_BINDING.read_text(encoding="utf-8")
    assert 'broker=self.broker' in src


# --------------------------------------------------------------------- #
# FP Markets DEMO production runner
# --------------------------------------------------------------------- #

def test_fp_runner_exists():
    assert FP_RUNNER.is_file()


def test_fp_runner_syntax_valid():
    ast.parse(FP_RUNNER.read_text(encoding="utf-8"))


def test_fp_runner_resolves_fp_markets_crm():
    """The FP runner must resolve FP Markets DEMO connections, not Deriv."""
    src = FP_RUNNER.read_text(encoding="utf-8")
    assert "broker='FP Markets'" in src
    assert "environment='DEMO'" in src
    # Must NOT resolve Deriv connections
    assert "broker='Deriv'" not in src


def test_fp_runner_uses_fp_broker_constant():
    src = FP_RUNNER.read_text(encoding="utf-8")
    assert 'BROKER = "FP Markets"' in src
    assert 'broker=BROKER' in src


def test_fp_runner_delegates_to_generalized_crm():
    """The FP runner must pass broker=BROKER to DemoNativeCRM."""
    src = FP_RUNNER.read_text(encoding="utf-8")
    assert "DemoNativeCRM(" in src
    assert "broker=BROKER" in src


def test_fp_runner_fp_accounts_listed():
    src = FP_RUNNER.read_text(encoding="utf-8")
    for suffix in ("0512", "1636", "1622", "2582"):
        assert suffix in src


def test_fp_runner_never_touches_live():
    src = FP_RUNNER.read_text(encoding="utf-8")
    assert "Never enables or submits on LIVE" in src
    assert "live=False" in src


def test_fp_runner_execution_session_checks_preset():
    src = FP_RUNNER.read_text(encoding="utf-8")
    assert "inpexposureonly=false" in src
    assert "inptrailingenabled=true" in src
    assert "signed_native_exposure" in src


# --------------------------------------------------------------------- #
# FP Markets DEMO enablement script
# --------------------------------------------------------------------- #

def test_fp_enable_exists():
    assert FP_ENABLE.is_file()


def test_fp_enable_syntax_valid():
    ast.parse(FP_ENABLE.read_text(encoding="utf-8"))


def test_fp_enable_targets_fp_demo_accounts():
    src = FP_ENABLE.read_text(encoding="utf-8")
    for suffix in ("0512", "1636", "1622", "2582"):
        assert f'"{suffix}"' in src


def test_fp_enable_resolves_fp_markets_crm():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "broker='FP Markets'" in src
    assert "environment='DEMO'" in src
    assert "broker='Deriv'" not in src


def test_fp_enable_invokes_fp_module():
    """The script must invoke production_fp_demo as a module."""
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "abutron.agent.production_fp_demo" in src
    assert "production_fp_demo.py" not in src


def test_fp_enable_preserves_account_bound_presets():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "EXECUTION_KEYS" in src
    assert "_parse_preset" in src
    # Account-bound keys must not be in the execution-key set
    exec_block = src.split("EXECUTION_KEYS = {")[1].split("}")[0]
    assert "InpConnectionId" not in exec_block
    assert "InpBrokerServer" not in exec_block
    assert "InpActivationSecret" not in exec_block


def test_fp_enable_verifies_ex5_sha():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "EXPECTED_EX5_SHA" in src
    assert "verify_ex5_sha" in src
    assert "hashlib" in src


def test_fp_enable_no_operator_override():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "ABUTRON_OPERATOR_OVERRIDE_FILE" not in src
    assert "write_override_config" not in src


def test_fp_enable_checks_subprocess_exit_code():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "result.returncode != 0" in src
    assert "EXECUTE_CYCLE_FAILED" in src


def test_fp_enable_only_touches_demo():
    src = FP_ENABLE.read_text(encoding="utf-8")
    assert "LIVE" in src  # mentioned in the no-LIVE-touch comment
    assert "environment='DEMO'" in src


# --------------------------------------------------------------------- #
# Preset template shared by both enablement scripts
# --------------------------------------------------------------------- #

def test_preset_template_enables_dispatch():
    lowered = PRESET_PATH.read_text(encoding="utf-8").lower()
    assert "inpexposureonly=false" in lowered
    assert "inptrailingenabled=true" in lowered
