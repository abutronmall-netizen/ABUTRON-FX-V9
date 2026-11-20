# FP Markets V305 — Native execution-cost export and V9 intake

**Status (20 November 2026): SOURCE-ONLY. NO FP DEMO/LIVE DEPLOYMENT.**
Exact existing Ubuntu data-only exporter source:
`/opt/apex-client-mt5/fpmarkets-feed/AbutronFPMarketsNativeFeedParallelV305.mq5`
(base SHA-256 `88baf5415d4e855f14c682dd77f370a645f5208e6b46a3d09b0a28d58b9f4872`).
The new implementation is version-controlled at
`mql5/AbutronFPMarketsNativeFeedParallelV305.mq5`; existing production
source/EX5, charts, bridges and FP LIVE 9392 have **not** been changed.

## Existing MT5 source and export flow

The authenticated FP V305 feed has a broker session heartbeat, and a
per-session **32-character export nonce**. It already publishes
`ABUTRON_FP_CATALOG_V305_<nonce>.txt` and
`ABUTRON_FP_QUOTES_V305_<nonce>.txt`, plus native bars at a 60-second
cadence. The 13 account-resolved symbols are:

`XAUUSD.r, BTCUSD, EURUSD.r, GBPUSD.r, USDJPY.r, AUDUSD.r, US100,
US30, US500, GER40, XTIUSD, USDCHF.r, NZDUSD.r`.

**Do not confuse** the FP heartbeat/export nonce or session-owner UUID
with the separate CRM connection ID; the native cost intake binds the
logged-in account/server to the latter through the actual CRM database.

## Native source addition

The data-only exporter now publishes
`MQL5/Files/ABUTRON_FP_COST_V1_<nonce>.json` on its own 20-second
timer, without changing the catalog/bars/quotes 60-second cadence.
This remains a feed, **not** an order writer. It contains no
`OrderSend`, `OrderCheck`, trade-position modification or order
activation code. It only samples MT5 broker data:

| Field | Native source |
|---|---|
| `tick_size` | `SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE)` |
| `contract_size` | `SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE)` |
| `volume_min`, `volume_step`, `volume_max` | `SYMBOL_VOLUME_*` |
| `point`, `digits` | `SYMBOL_POINT`, `SYMBOL_DIGITS` |
| `stops_level`, `freeze_level` | `SYMBOL_TRADE_STOPS_LEVEL`, `SYMBOL_TRADE_FREEZE_LEVEL` |
| `bid`, `ask`, `tick_epoch` | `SymbolInfoTick` |
| `buy_probe_native_ordercalcprofit_usd` | `OrderCalcProfit(ORDER_TYPE_BUY, symbol, minlot, ask, ask - 10*tick_size, pnl)` |
| `sell_probe_native_ordercalcprofit_usd` | `OrderCalcProfit(ORDER_TYPE_SELL, symbol, minlot, bid, bid + 10*tick_size, pnl)` |
| `account_equity_usd`, `account_currency` | `ACCOUNT_EQUITY`, `ACCOUNT_CURRENCY` |
| session identity | heartbeat nonce, owner UUID, broker server, MT5 login and DEMO mode |

`native_bid_ask_and_ordercalcprofit_verified` is **derived**, never
hardcoded: it is `true` only if all 13 symbols have fresh bid/ask,
valid volume/contract/tick specifications and negative BUY and SELL
`OrderCalcProfit` results. If an instrument is unavailable or stale
(e.g. an inactive GER40 quote), this boolean is `false`; the consumer
does not present partial data as complete current execution costs.

The exporter calls `FileOpen`, `FileWriteString(handle,...)`,
`FileFlush`, `FileClose` and `FileMove(tmp,0,final,FILE_REWRITE)`.
**MQL5 file operations stay in the terminal's MQL5/Files sandbox**.
They do **not** directly create `/var/lib/...` files. The previous
specification's `FileWriteString(path,json)` and `FileRename()`
pseudo-calls have been replaced with proper MQL5 APIs.

The native probe computes hypothetical stop P/L only. It does not place
an order, prove current margin acceptance or establish a real broker
order/SL/TP/trailing modification.

## V9 Python intake

`src/abutron/feeds/fp_native_cost.py` reads this export **only**
from the explicitly configured FP DEMO terminal directory. It validates:

- CRM connection exists, FP Markets, DEMO, CONNECTED and expected suffix.
- Exactly one fresh native heartbeat; matching nonce, session-owner
  token, broker server, DEMO and `NO_ORDER_FUNCTIONS`.
- Same-cycle catalog and quote headers and the catalog's exact set of
  13 FP symbol mappings.
- Cost JSON's native source, account login, server, heartbeat owner,
  nonce, suffix, DEMO environment, USD account currency and nonzero equity.
- File and native sample freshness, complete 13/13 instrument probes,
  actual negative OrderCalcProfit amounts, and cross-consistency of
  symbol contract/volume/digits/trade-mode with the V305 catalog.

**Broker-clock correction:** MQL5 broker tick timestamps are not
necessarily UTC. The exporter samples TimeTradeServer() and TimeGMT()
within the same cycle, recording broker_server_epoch and
broker_clock_offset_seconds. The Python intake normalizes each broker
tick using that observed offset before applying freshness rules.
This supports GMT+2 or other real broker offsets, while still rejecting
genuinely stale quotes.

Only on success can `refresh_fp_v9_cost_report()` atomically
materialize a `0600` root-owned V9 consumer JSON with the existing
`DemoNativeCRM` contract:
`account_suffix`, `environment`,
`native_bid_ask_and_ordercalcprofit_verified: true`,
`account_equity_usd` and `instruments`.

`production_fp_demo.py` calls the reader **only when**
`ABUTRON_FP_FEED_FILES` is explicitly configured. Otherwise its
existing cost-report behavior remains unchanged. This is a component
of the existing runner; **no new autonomous supervisor, gate or service**
was introduced.

For future staging (not a current deployment command):

```bash
# Only after a staged FP DEMO 0512 runtime, signed V9 order writer,
# genuine native feed-cost source and account-specific paths exist:
ABUTRON_FP_FEED_FILES=/var/lib/private/abutron-client-mt5-<0512-CID>/terminal/MQL5/Files
ABUTRON_FP_COST_REPORT=/var/lib/abutron-fp-v4-0512/v4-native-execution-cost-0512.json
```

The resulting report will be **root-owned only when the V9 runner is
run as root**. This source change does not create the cost report on
the actual server and is not evidence that the new V9 FP writer
exists or is armed.

## Build, validation and deployment boundaries

- The isolated full Python suite and new synthetic account/nonce,
  positive/negative cost-contract tests must pass.
- Corrected broker-server-clock native source compiled with official MetaEditor (0 errors, 0 warnings) at commit d22561b. Independently downloaded and verified exact QA EX5 artifact SHA-256 ebffae968f5c56b1653d97731a7395accfed2cb2e997723e4d9bdaa3fdd005eb. This hash is recorded in mql5/AbutronFPMarketsNativeFeedParallelV305.ex5.sha256.txt and supersedes the earlier 2928de20 build. Nothing was deployed to a broker terminal.
- **Do not reuse the existing V305 production EX5 checksum for a newly
  compiled artifact.** Compiled binary hashes must be verified separately.
- The existing FP 0512 order bridge is **currently armed**, though recent
  dispatcher cycles were blocked with zero orders. Its order path,
  trading control, existing native receipts and profile must remain
  unchanged during source-only work.
- An eventual source/EA update to the existing FP feed, V9 0512
  dedicated order writer, single-writer cutover, and any use on FP
  DEMO 1636/1622/2582 require their own explicitly authorized
  operational steps. **FP LIVE 9392 stays untouched and OFF**.

The 65-second `DemoNativeCRM` consumer TTL is met by a 20-second native
cost-export cadence **only if** the broker instruments are genuinely
updating, the data-only EA runs, and the subsequent root-owned V9
materialization is performed on a real staged runtime.
