"""Tests for the EA regression fixes (UTF-8, magic, trailing).

Reproduces the two regressions introduced by the faulty EX5 rebuild:

1. UTF-8 request reading: the FileOpen for the pending request file
   omitted FILE_ANSI | CP_UTF8, causing MQL5 to misread the UTF-8 JSON
   and lose the request_id. The fix restores the explicit UTF-8
   configuration.

2. Missing trade-magic and trailing-stop: the faulty rebuild omitted
   the MAGIC define, req.magic assignment, and the entire trailing-stop
   implementation (ApplyTrailingStops, trail tracking arrays, OnTimer
   call). The fix restores all of these.

These tests assert the source contains the required patterns. They do
NOT compile MQL5 — compilation is verified by the CI workflow.
"""
from __future__ import annotations

import pathlib

import pytest

MQL5_SRC = pathlib.Path(__file__).parent.parent / "mql5" / "ApexNativeWriter.mq5"


@pytest.fixture(scope="module")
def source():
    return MQL5_SRC.read_text()


# --------------------------------------------------------------------- #
# UTF-8 request reading fix
# --------------------------------------------------------------------- #

def test_pending_request_fileopen_uses_utf8(source):
    """The pending-request FileOpen must include FILE_ANSI | CP_UTF8."""
    assert "FileOpen(pending, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8)" in source, (
        "ProcessOrderRequests must read the pending request with "
        "FILE_ANSI | CP_UTF8 — the faulty rebuild omitted this, causing "
        "misread UTF-8 JSON and lost request IDs")


def test_last_file_fileopen_uses_utf8(source):
    """The order-flow last-file FileOpen must also use UTF-8."""
    assert "FileOpen(lastFile, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8)" in source


def test_no_bare_fileopen_file_txt_for_reading(source):
    """No FileOpen for reading should omit FILE_ANSI | CP_UTF8."""
    # Check that every FILE_READ FileOpen includes FILE_ANSI
    for line in source.splitlines():
        if "FileOpen(" in line and "FILE_READ" in line and "FILE_WRITE" not in line:
            assert "FILE_ANSI" in line, (
                f"FileOpen read call without FILE_ANSI: {line.strip()}")


# --------------------------------------------------------------------- #
# Trade-magic fix
# --------------------------------------------------------------------- #

def test_magic_define_present(source):
    """The MAGIC define must be present."""
    assert "#define MAGIC 73113051" in source, (
        "MAGIC define must be restored — the faulty rebuild omitted it")


def test_order_request_sets_magic(source):
    """The order request must set req.magic = MAGIC."""
    assert "req.magic     = MAGIC;" in source, (
        "OrderSend request must set req.magic — the faulty rebuild omitted it")


def test_order_request_sets_comment(source):
    """The order request must set a comment for traceability."""
    assert 'req.comment   = "ABUTRON V9 DEMO";' in source


# --------------------------------------------------------------------- #
# Trailing-stop fix
# --------------------------------------------------------------------- #

def test_trail_tracking_arrays_present(source):
    """The trailing-stop tracking arrays must be declared."""
    for var in ["g_trail_ticket", "g_trail_symbol", "g_trail_dir",
                "g_trail_k", "g_trail_stop", "g_trail_entry",
                "g_trail_active"]:
        assert var in source, f"Missing trail tracking array: {var}"


def test_apply_trailing_stops_function_present(source):
    """The ApplyTrailingStops function must be defined."""
    assert "void ApplyTrailingStops()" in source, (
        "ApplyTrailingStops must be restored — the faulty rebuild "
        "omitted the entire trailing-stop implementation")


def test_ontimer_calls_apply_trailing(source):
    """OnTimer must call ApplyTrailingStops."""
    assert "ApplyTrailingStops();" in source
    # Verify it's called within the non-exposure-only branch
    ontimer_section = source[source.index("void OnTimer()"):
                             source.index("void OnTimer()") + 200]
    assert "ApplyTrailingStops" in ontimer_section


def test_ack_registers_position_for_trailing(source):
    """The ACK path must register the position for trailing management."""
    assert "g_trail_ticket[idx] = res.order;" in source, (
        "ACK path must store the position ticket for trailing")
    assert "g_trail_symbol[idx] = symbol;" in source
    assert "g_trail_dir[idx]    = dirn;" in source
    assert "g_trail_k[idx]      = trailK;" in source
    assert "g_trail_stop[idx]   = trailSt;" in source


def test_trailing_uses_modifyorder(source):
    """The trailing function must use ModifyOrder (TRADE_ACTION_SLTP)."""
    assert "TRADE_ACTION_SLTP" in source, (
        "ApplyTrailingStops must use TRADE_ACTION_SLTP to modify the SL")
    assert "mreq.sl        = trailStop;" in source


def test_trailing_never_widens(source):
    """The trailing function must check that the new stop is tighter."""
    assert "tighter" in source
    assert 'dir == "BUY"' in source


def test_trailing_magic_check(source):
    """The trailing function must verify position magic before managing."""
    assert "PositionGetInteger(POSITION_MAGIC) != MAGIC" in source, (
        "ApplyTrailingStops must verify the position magic before managing")


def test_ondeinit_clears_trail_arrays(source):
    """OnDeinit must free the trail tracking arrays."""
    ondeinit_section = source[source.index("void OnDeinit"):
                              source.index("void OnDeinit") + 200]
    assert "ArrayFree(g_trail_ticket)" in ondeinit_section


def test_lot_normalization_epsilon_preserved(source):
    """The lot-normalization epsilon fix must still be present."""
    assert "MathFloor(v / step + 1.0e-9) * step" in source, (
        "The lot-normalization epsilon fix from 6ce32ac must be retained")
