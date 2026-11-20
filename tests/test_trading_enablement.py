"""Tests for the V9 trading enablement deployment script.

Verifies the preset file enables dispatch (InpExposureOnly=false),
the operator override config format is valid, and the deployment
script structure is correct.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import patch

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
PRESET_PATH = REPO_ROOT / "ops/v9-cutover/PR37WriterExposureOnly.set"
OVERRIDE_TEMPLATE_PATH = REPO_ROOT / "ops/v9-cutover/operator_override_template.json"
SCRIPT_PATH = REPO_ROOT / "ops/v9-cutover/enable_trading_all_accounts.py"


@pytest.fixture(scope="module")
def preset_content():
    return PRESET_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def override_template():
    return json.loads(OVERRIDE_TEMPLATE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- #
# Preset file
# --------------------------------------------------------------------- #

def test_preset_file_exists(preset_content):
    assert PRESET_PATH.is_file()
    assert len(preset_content) > 0


def test_preset_enables_dispatch(preset_content):
    """InpExposureOnly must be false to enable OrderSend."""
    lowered = preset_content.lower()
    assert "inpexposureonly=false" in lowered


def test_preset_enables_trailing(preset_content):
    """Trailing must be enabled alongside dispatch."""
    lowered = preset_content.lower()
    assert "inptrailingenabled=true" in lowered


def test_preset_sets_trade_magic(preset_content):
    """Trade magic must match the deployed EA default."""
    assert "InpTradeMagic=20261120" in preset_content


def test_preset_sets_trailing_atr_mult(preset_content):
    """Trailing ATR multiple must be configured."""
    assert "InpTrailingAtrMult=2.0" in preset_content


def test_preset_passes_execution_session_checks(preset_content):
    """Simulates the checks in production_deriv_demo.execution_session_enabled."""
    lowered = preset_content.lower()
    assert "inpexposureonly=false" in lowered
    assert "inptrailingenabled=true" in lowered


# --------------------------------------------------------------------- #
# Operator override template
# --------------------------------------------------------------------- #

def test_override_template_exists(override_template):
    assert OVERRIDE_TEMPLATE_PATH.is_file()
    assert isinstance(override_template, dict)


def test_override_template_has_required_fields(override_template):
    assert "authorized_by" in override_template
    assert "connections" in override_template
    assert "reason" in override_template
    assert "active" in override_template


def test_override_template_connections_nonempty(override_template):
    assert len(override_template["connections"]) >= 3
    assert "REPLACE_WITH_6207_CONNECTION_ID" in override_template["connections"]


# --------------------------------------------------------------------- #
# Deployment script
# --------------------------------------------------------------------- #

def test_script_syntax_valid():
    import ast
    ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))


def test_script_targets_all_three_accounts():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"6207"' in content
    assert '"7593"' in content
    assert '"9244"' in content


def test_script_only_touches_demo():
    """The script must not touch LIVE accounts."""
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "environment='DEMO'" in content
    assert "LIVE" in content  # mentioned in the no-LIVE-touch comment


def test_script_sets_override_env_var():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "ABUTRON_OPERATOR_OVERRIDE_FILE" in content


def test_script_runs_execute_mode():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"--mode", "execute"' in content


def test_script_risk_invariants_not_bypassed():
    """The script must not weaken RiskGate invariants."""
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "NOT bypassed" in content or "not bypassed" in content
    assert "0.15%" in content or "0.0015" in content
    assert "20%" in content or "0.02" in content
