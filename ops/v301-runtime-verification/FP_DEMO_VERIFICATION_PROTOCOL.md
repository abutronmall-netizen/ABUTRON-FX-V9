# FP DEMO V3.01 Runtime Verification Protocol

**Scope:** FP DEMO accounts 0512 and 1636 only. DEMO persistence-only pattern.
LIVE 9392 and all trading switches/risk settings remain untouched.

**Goal:** Prove the V3.01 multi-symbol refactor produces correct per-symbol
tick + DOM export for all 13 FP instruments against a real broker terminal.

## 1. Prerequisites (verified in repo)

| Item | Value |
|---|---|
| V3 indicator binary SHA-256 | `321bc0d80b783f308a23b9823e4c3da95b138ea95c2e0f595a826fdd3c6f3d29` |
| V305 EA binary SHA-256 | `e7e67f13a4b2d6969487c6901118247acce7b499d40f7bc77ce5b94ed8783907` |
| V305 symbol list (13) | `XAUUSD.r|BTCUSD|EURUSD.r|GBPUSD.r|USDJPY.r|AUDUSD.r|US100|US30|US500|GER40|XTIUSD|USDCHF.r|NZDUSD.r` |
| Status monitor SHA pin | **updated** to V3.01 (`321bc0d8…`) in `scripts/native_market_v3_status_ro.py` |

## 2. Deployment steps (host-side, per account)

Performed on the Ubuntu worker for each DEMO account (0512, then 1636):

1. **Stop** the existing V305 feed chart process for the account.
2. **Stage** the V3.01 binaries:
   - `dist/mql5/ApexMarketFeedV3.ex5` → `MQL5/Indicators/ABUTRON/`
   - V305 EA → `MQL5/Experts/ABUTRON/`
3. **Verify** each staged binary hash matches the pin above (`sha256sum`).
4. **Start** the V305 feed chart (auth startup expert preserved; V3
   attaches programmatically via `EnsureV3Indicator()` after broker auth).
5. **Confirm** the terminal log shows:
   `ABUTRON_FP_V3_INDICATOR_ATTACHED_AFTER_BROKER_AUTH` and
   `Indicators\custom indicator ApexMarketFeedV3 (<symbol>,<tf>) loaded succesfully`
6. **Wait** for the market session to produce ticks (or use the existing
   DEMO streaming session). Allow ≥ 5 minutes of streaming per symbol.

## 3. Verification (read-only, after streaming)

Run on the host against the produced files:

```bash
python3 scripts/verify_fp_v301_runtime.py \
    --data-dir <worker_root>/ABUTRON_V9_NATIVE \
    --day <YYYYMMDD>
```

Or JSON output for evidence collection:

```bash
python3 scripts/verify_fp_v301_runtime.py \
    --data-dir <worker_root>/ABUTRON_V9_NATIVE \
    --day <YYYYMMDD> --json
```

## 4. Per-symbol assertions

For each of the 13 FP symbols, the verifier asserts:

| # | Assertion | Detail |
|---|---|---|
| 1 | File layout | One JSONL per symbol per day: `ABUTRON_NATIVE_MARKET_V3_<sanitized_symbol>_<YYYYMMDD>.jsonl` |
| 2 | Schema | Every record has `schema`, `symbol`, `broker_server`, `account_login`, `environment="DEMO"`, `record_type` |
| 3 | Identity | Record `symbol` matches filename's sanitized symbol |
| 4 | Record types | `QUOTE`, `MARKET_PRINT`, `DOM`, `STATUS` all present |
| 5 | Price precision | All prices in a symbol's records use consistent decimals matching that symbol's `SYMBOL_DIGITS` |
| 6 | DOM structure | `levels[]` with `book_type`, `price`, `volume_real`, `volume` per level |
| 7 | STATUS | `dom_subscribed=true` for subscribed symbols |
| 8 | Read-only | No `order_ticket`, `position_ticket`, `trade_action`, `deal_id`, `order_send`, `position_modify` fields |

Expected per-symbol digit counts (FP Markets contract):

| Symbol | Digits | | Symbol | Digits |
|---|---|---|---|---|
| XAUUSD.r | 2 | | US100 | 2 |
| BTCUSD | 2 | | US30 | 2 |
| EURUSD.r | 5 | | US500 | 2 |
| GBPUSD.r | 5 | | GER40 | 2 |
| USDJPY.r | 3 | | XTIUSD | 3 |
| AUDUSD.r | 5 | | USDCHF.r | 5 |
| | | | NZDUSD.r | 5 |

## 5. Evidence to collect per account

- Full verifier output (text or JSON) for the verification day.
- Terminal log excerpt showing the V3 attach attestation lines.
- `sha256sum` output of both staged binaries.
- Record counts per symbol per file (from verifier output).
- Any warnings (precision mismatches) with the actual vs expected value.

## 6. Pass / fail criteria

**PASS:** All 13 symbols pass all 8 assertions. Zero errors.
**FAIL:** Any symbol fails any assertion. Record the error, inspect the
terminal log for that symbol, and do not proceed to Deriv verification
until resolved.

## 7. Explicit exclusions

- LIVE 9392 — not touched, not verified here.
- Trading switches, risk settings, order bridges — unchanged.
- V9 writer / order-capable charts — unchanged.
- No market prints required; quote-only symbols are valid.

## 8. Related tooling

- `scripts/verify_fp_v301_runtime.py` — per-symbol file verifier (this protocol).
- `scripts/native_market_v3_status_ro.py` — read-only monitor (SHA pin updated to V3.01).
- `scripts/verify_native_market_v3.py` — standalone V3 evidence inspector.
- `src/abutron/feeds/native_market_v3.py` — collector (`read_v3`, `_sanitize_file_symbol`).
- `mql5/ApexMarketFeedV3.mq5` — V3.01 indicator source (multi-symbol).
- `mql5/AbutronFPMarketsNativeFeedParallelV305.mq5` — FP feed EA.
