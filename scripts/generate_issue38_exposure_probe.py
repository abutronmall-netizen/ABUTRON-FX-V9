#!/usr/bin/env python3
"""Generate a NO-ORDER MQL5 exposure probe from the signed native writer.

The resulting EA contains no order request handling, no OrderSend and no
trade-receipt writer. Intended ONLY for isolated DEMO broker evidence.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def make_probe(source: str) -> str:
    timer = """   // DEMO exposure QA is strictly read-only unless explicitly reconfigured.
   if(!InpExposureOnly)
   {
      ProcessOrderRequests();
      ManageAgenticTrailingStops();
   }"""
    assert source.count(timer) == 1, "Unexpected OnTimer layout"
    source = source.replace(
        timer,
        "   // NO-ORDER QA binary: never handles broker order requests.",
        1,
    )
    start_marker = "//+------------------------------------------------------------------+\n//| Process signed order requests from MQL5/Files handoff"
    end_marker = "//+------------------------------------------------------------------+\n//| Write a text file (creates/overwrites)"
    assert source.count(start_marker) == 1, "Unexpected OrderSend block"
    assert source.count(end_marker) == 1, "Unexpected native export block"
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    source = source[:start] + (
        "// ABUTRON ISSUE38 EXPOSURE-ONLY: ORDER FUNCTIONS REMOVED.\n\n"
    ) + source[end:]
    receipt_start = "//+------------------------------------------------------------------+\n//| Write native receipt"
    receipt_end = "\nstring TimeStampToStr(datetime ts)"
    assert source.count(receipt_start) == 1
    start = source.index(receipt_start)
    end = source.index(receipt_end, start)
    source = source[:start] + source[end:]
    source = source.replace(
        'input bool    InpExposureOnly   = true;',
        'input bool    InpExposureOnly   = true; // NO-ORDER build, fixed behavior',
        1,
    )
    trailing_marker = "//| Native MT5 trailing: broker-side SLTP"
    assert source.count(trailing_marker) == 1, "Unexpected trailing layout"
    trailing_start = source.rfind("//+------------------------------------------------------------------+", 0, source.index(trailing_marker))
    source = source[:trailing_start]
    for forbidden in (
        "OrderSend(", "ProcessOrderRequests(", "WriteReceipt(",
        "ManageAgenticTrailingStops(", "WriteAgenticTrailingReceipt(",
        "TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP",
        "ABUTRON_REQ_PENDING", "FileDelete(",
    ):
        assert forbidden not in source, f"Order capability retained: {forbidden}"
    assert "void ExportNativeData()" in source
    assert "HmacSha256Hex(" in source
    assert "OnInit()" in source
    assert "ACCOUNT_TRADE_MODE_DEMO" in source
    return source


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default="mql5/ApexNativeWriter.mq5")
    p.add_argument("--output", default="mql5/ApexNativeExposureProbe.mq5")
    opts = p.parse_args(argv)
    src = Path(opts.source)
    dst = Path(opts.output)
    output = make_probe(src.read_text(encoding="utf-8"))
    if src.resolve() == dst.resolve():
        raise SystemExit("refusing to overwrite main EA")
    dst.write_text(output, encoding="utf-8")
    print(f"NO_ORDER_EA_GENERATED {dst} bytes={len(output.encode())}")


if __name__ == "__main__":
    main()
