# FP Markets V305 Exporter Extension — Cost-Report Contract

**Status:** Source-only specification. **No production change.** The
extended exporter is not yet compiled or deployed. This document defines
the exact JSON contract the extended `AbutronFPMarketsNativeFeedParallelV305.mq5`
must publish for V9's `DemoNativeCRM` to accept it as genuine FP
broker-cost evidence.

**Scope:** FP Markets DEMO 0512. The existing feed, order bridge, and
LIVE 9392 remain unchanged. This extension only adds a cost-report
publisher to the existing feed EA — it does not add order functions.

## 1. Contract summary

V9 reads a cost-report JSON file from the account's cost-report path
(`ABUTRON_FP_COST_REPORT`, default
`/var/lib/abutron-fp-v4-0512/v4-native-execution-cost-0512.json`).
`DemoNativeCRM.snapshot()` validates it read-only. The contract is:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `account_suffix` | string | yes | Must match the account suffix (e.g. `"0512"`) |
| `environment` | string | yes | Must be exactly `"DEMO"` |
| `native_bid_ask_and_ordercalcprofit_verified` | boolean | yes | Must be JSON `true`, derived from actual broker probes |
| `account_equity_usd` | float | yes | Current `ACCOUNT_EQUITY`, must be > 0 |
| `instruments` | object | yes | Dict of symbol → spec dict |

Each entry in `instruments` must contain at least:

| Field | Type | Required | MQL5 source |
|---|---|---|---|
| `tick_size` | float | yes | `SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE)` |
| `contract_size` | float | recommended | `SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP)` |
| `volume_min` | float | recommended | `SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN)` |
| `volume_step` | float | recommended | `SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP)` |
| `volume_max` | float | recommended | `SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX)` |
| `stops_level` | int | recommended | `SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL)` |
| `freeze_level` | int | recommended | `SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL)` |
| `digits` | int | recommended | `SymbolInfoInteger(sym, SYMBOL_DIGITS)` |

Additional spec fields are accepted and passed through to V9's
`native_specs` (used by the market-data adapter and dispatcher).

## 2. The 13 FP symbols

Confirmed on the live FP 0512 terminal. These are actual FP Markets
symbols, not copied Deriv specifications:

```
XAUUSD.r, BTCUSD, EURUSD.r, GBPUSD.r, USDJPY.r,
AUDUSD.r, US100, US30, US500, GER40, XTIUSD,
USDCHF.r, NZDUSD.r
```

The exporter should iterate these symbols (or discover them from the
broker's symbol list) and probe each one. V9 derives its trading
symbol list from `sorted(instruments.keys())`.

## 3. The verification flag — never fabricated

`native_bid_ask_and_ordercalcprofit_verified` must be `true` **only**
when every instrument probe has succeeded. It is a derived boolean,
not a hardcoded value. The exporter must set it to `false` (or omit
it) if any probe fails — V9 will reject the report with
`BROKER_NATIVE_ORDER_CONTRACT_NOT_AVAILABLE`.

The probes that must succeed for the flag to be `true`:

1. **Bid/ask quote** — `SymbolInfoTick(sym, tick)` returns valid
   `tick.bid > 0` and `tick.ask > 0`.
2. **OrderCalcProfit stop-risk** — `OrderCalcProfit(type, sym, vol,
   price, sl, pnl)` returns `pnl < 0` (a valid stop-loss distance).
3. **Tick size** — `SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE) > 0`.
4. **Volume specs** — `SYMBOL_VOLUME_MIN`, `SYMBOL_VOLUME_STEP` return
   valid positive values.

If any symbol's probes fail, the entire report's flag must be `false`.
V9 rejects partial or fabricated reports.

## 4. MQL5 extension pattern

The V305 exporter is a feed EA (no `OrderSend`). The extension adds a
cost-report publisher to its existing timer loop. The pattern follows
the existing exporters' conventions (`ApexOrderFlowExportV3.mq5`,
`ApexMarketFeedV3.mq5`):

```mql5
// --- Cost-report publishing (V9 integration) ---

string g_cost_report_path = "";  // set from InpCostReportPath input
static string g_cost_json = "";

string JsonEscape(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", "");
   StringReplace(value, "\n", "");
   return value;
}

string CostReportJson()
{
   string instruments = "";
   bool all_verified = true;

   // Iterate the 13 FP symbols.
   string symbols[] = {"XAUUSD.r","BTCUSD","EURUSD.r","GBPUSD.r",
                       "USDJPY.r","AUDUSD.r","US100","US30","US500",
                       "GER40","XTIUSD","USDCHF.r","NZDUSD.r"};
   for(int i = 0; i < ArraySize(symbols); i++)
   {
      string sym = symbols[i];
      if(!SymbolSelect(sym, true))
      {
         all_verified = false;
         continue;
      }
      MqlTick tick;
      double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
      double vol_min   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
      double vol_step  = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
      double vol_max   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
      long   stops     = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
      long   freeze    = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
      long   digits    = SymbolInfoInteger(sym, SYMBOL_DIGITS);

      // Verify bid/ask.
      if(!SymbolInfoTick(sym, tick) || tick.bid <= 0 || tick.ask <= 0)
      {
         all_verified = false;
         continue;
      }

      // Verify OrderCalcProfit stop-risk with a 1-lot probe.
      double pnl = 0.0;
      double ask = tick.ask;
      if(!OrderCalcProfit(ORDER_TYPE_BUY, sym, vol_min, ask,
                          ask - 10.0 * tick_size, pnl) || pnl >= 0)
      {
         all_verified = false;
         continue;
      }

      // Append instrument spec to JSON.
      if(i > 0) instruments += ",";
      instruments += "\"" + JsonEscape(sym) + "\":{"
                     + "\"tick_size\":" + DoubleToString(tick_size, 8) + ","
                     + "\"volume_min\":" + DoubleToString(vol_min, 8) + ","
                     + "\"volume_step\":" + DoubleToString(vol_step, 8) + ","
                     + "\"volume_max\":" + DoubleToString(vol_max, 8) + ","
                     + "\"stops_level\":" + IntegerToString(stops) + ","
                     + "\"freeze_level\":" + IntegerToString(freeze) + ","
                     + "\"digits\":" + IntegerToString(digits)
                     + "}";
   }

   string suffix = "";
   long login = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   // Derive suffix from login (e.g. 7410512 -> "0512").
   suffix = StringSubstr(IntegerToString(login), StringLen(IntegerToString(login)) - 4);

   return "{\"account_suffix\":\"" + suffix + "\","
        + "\"environment\":\"DEMO\","
        + "\"native_bid_ask_and_ordercalcprofit_verified\":"
        + (all_verified ? "true" : "false") + ","
        + "\"account_equity_usd\":"
        + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ","
        + "\"instruments\":{" + instruments + "}}";
}

void PublishCostReport()
{
   if(g_cost_report_path == "") return;
   string json = CostReportJson();
   string tmp = g_cost_report_path + ".tmp";
   if(FileWriteString(tmp, json) > 0)
   {
      FileRename(tmp, g_cost_report_path);
   }
}
```

## 5. Freshness requirement

`DemoNativeCRM` enforces a **65-second TTL** on the cost report. The
exporter's timer interval must produce a fresh report within this
window. If the V305 exporter's current interval exceeds 65 seconds, it
must be reduced (or a dedicated cost-publish timer added) so the report
is always current.

The report's file mtime is what V9 checks — the `FileRename` in
`PublishCostReport()` updates it atomically.

## 6. Deployment sequence (not yet authorized)

When the V305 extension is ready for deployment on Ubuntu:

1. Add the V305 exporter source to the repo (`mql5/`).
2. Apply the cost-report extension from this document.
3. Compile the `.ex5`; record its SHA-256.
4. Pin the SHA-256 in the V9 runner / enablement script.
5. Deploy to the FP 0512 terminal (feed EA only — no order functions).
6. Verify the cost-report JSON satisfies the contract tests
   (`tests/test_fp_cost_contract.py`).
7. Run V9 preflight against the published cost report.

**The existing FP 0512 order bridge remains armed and unchanged during
all steps above.** The one-writer cutover (disarm bridge → arm V9) is
a separate, separately-authorized deployment step.

## 7. Test coverage

The contract is enforced by `tests/test_fp_cost_contract.py` (15
tests):

- **Accepted:** all 13 symbols, tick_size flows to native_specs,
  equity flows to account, instruments carry additional specs.
- **Rejected:** wrong suffix, wrong environment, verification flag
  false, verification flag as string, instruments not a dict, missing
  equity, zero equity, missing verification flag, stale report (TTL).
- **Runner-side:** tick_size extraction for receipt reconciliation,
  skipping instruments without tick_size.
