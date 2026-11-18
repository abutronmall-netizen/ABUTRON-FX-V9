# AbuGatewayBridge.mq5 — compile audit

No MetaEditor/MT5 compiler is available in this environment, so this is a
static audit against the documented MQL5 API. Run the checklist at the
bottom in MetaEditor before trusting the EA with a demo account.

## What was fixed in this pass

| Issue | Impact | Fix |
|---|---|---|
| `Np()` returned `string`, assigned to `req.price/sl/tp` (`double`) | **compile error** (`type mismatch`) | `Np()` now returns a normalized `double` |
| `req.type_filling = FillingMode(...)` (`int` → enum) | compile warning/error on strict builds | explicit `ENUM_ORDER_TYPE_FILLING` cast |
| Pending orders used the market filling mode | broker `INVALID_FILL` on many servers | pending orders force `ORDER_FILLING_RETURN` |
| `int start = MathMax(0, total - 200)` (`double` → `int`) | narrowing warning | explicit ternary |
| `req.magic = InpMagic` (`long` → `ulong`) | warning | explicit cast |
| `client_order_id` appended to the broker comment | 31-char comment pollution | removed |
| Market positions reported `order_id` as `position_id` | modify/close targeted the wrong ticket on hedging accounts | position id resolved from the deal via `HistoryDealSelect` + `DEAL_POSITION_ID` |

## MQL5 surface used

**Lifecycle:** `OnInit`, `OnDeinit`, `OnTimer`, `OnTick`, `EventSetTimer`,
`EventKillTimer`, `GetTickCount64`.

**HTTP:** `WebRequest` (string-body overload), `StringToCharArray`,
`CharArrayToString`, `StringToLen`, `GetLastError` (4060 = URL not in the
terminal allowlist).

**Account/symbol:** `AccountInfoInteger/Double/String`,
`TerminalInfoInteger(TERMINAL_CONNECTED / TERMINAL_TRADE_ALLOWED)`,
`SymbolSelect`, `SymbolInfoInteger/Double/String`.

**Market data:** `CopyRates`, `MqlRates`, `TimeCurrent`, `TimeGMT`,
`TimeToString`.

**Positions/orders:** `PositionsTotal`, `PositionGetTicket`,
`PositionSelectByTicket`, `PositionGetInteger/Double/String`,
`OrdersTotal`, `OrderGetTicket`, `OrderSelect`, `OrderGetInteger/Double/String`,
`HistorySelect`, `HistoryDealsTotal`, `HistoryDealGetTicket`,
`HistoryDealSelect`, `HistoryDealGet*`, `HistoryOrdersTotal`,
`HistoryOrderGetTicket`, `HistoryOrderGet*`.

**Trading:** `OrderSend`, `MqlTradeRequest`, `MqlTradeResult`,
`TRADE_ACTION_DEAL / PENDING / SLTP / REMOVE`, `ORDER_TYPE_BUY/SELL`,
`*_LIMIT / *_STOP`, `ORDER_TIME_GTC`, `SYMBOL_FILLING_MODE`,
`TRADE_RETCODE_*`.

**Strings/JSON:** `StringFormat`, `StringLen`, `StringSubstr`,
`StringFind`, `StringSplit`, `StringGetCharacter`, `StringTrimLeft/Right`,
`DoubleToString`, `IntegerToString`, `ShortToString`, `NormalizeDouble`,
`MathRound`, `MathMin/Max/Abs`, `ZeroMemory`.

## Deliberate design choices

- **At-most-once commands.** The EA never re-executes a command line. If
  the result POST fails, it retries only the result upload (the gateway
  completes idempotently); it does not re-send the order.
- **No fabricated ACK.** `OrderSend` failure uploads `accepted:false` with
  the broker retcode. A network failure leaves the command `SENT` for the
  gateway to surface as unknown.
- **UTC timestamps.** Broker server time is converted using
  `TimeCurrent() - TimeGMT()` before upload.
- **Immediate state push after a fill** so REST/WS readers see a new
  position without waiting for the next 5s interval.
- **One writer.** Never attach this EA and `ApexNativeWriter.mq5` to the
  same terminal.

## MetaEditor checklist (must be done on a demo account)

1. Compile with **0 errors**; review warnings. MQL5 warnings usually mean
   an implicit conversion that can be wrong at runtime.
2. `Tools → Options → Expert Advisors → Allow WebRequest for listed URL`
   includes the exact gateway base URL.
3. Attach to a demo account with `InpEnableTrading=false` first; confirm
   the gateway shows `CONNECTED`, balance/equity, prices and candles.
4. Enable trading and place a **0.01 lot** market order. Confirm:
   - gateway returns `EXECUTED` with a ticket/deal,
   - position id can be modified,
   - partial close works on the account's position mode,
   - deal history contains both entry and close.
5. Break the network mid-command (disable the adapter), send another
   order, and confirm the gateway reports `202`/`SENT` — never a fake fill.
6. Repeat for a pending order and its cancel.
7. Soak 24h on demo: check EA memory, state size, failed uploads, and that
   `sequence` keeps increasing.
8. Only then move to a live account, starting at minimum volume.

## Known runtime caveats

- Partial close only works on **hedging** accounts; netting accounts close
  the whole position.
- The broker's `ORDER_FILLING_MODE` can change per symbol/session; the EA
  chooses FOK → IOC → RETURN for market orders and RETURN for pending.
- `WebRequest` is blocking and runs on the EA thread. The poll is
  throttled (`InpPollMs`, default 250 ms) so it cannot starve `OnTick`
  processing.
- State pushes include up to `InpCandleCount × symbols` bars. Keep the
  count modest (60) or raise `InpStateIntervalSec` on slow links.
- `TRADE_RETCODE_*` strings are mapped for the common codes; unknown codes
  appear as `RETCODE_<n>` and are still machine-readable.
