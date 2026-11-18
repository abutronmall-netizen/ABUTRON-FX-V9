# ABUTRON MT5 Gateway — a multi-tenant API bridge for MetaTrader 5

A MetaApi-style control plane for MT5, built on top of the ABUTRON runtime
that already exists in this repository. Clients get one API key and a
REST + WebSocket interface; each client can connect one or many MT5
accounts; the gateway provisions, isolates, rate-limits, audits and
streams them.

```
Client bots / dashboards
        │  REST + WebSocket (tenant API keys)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  API tier — FastAPI, stateless per request                  │
│  keys · scopes · rate limits · idempotency · audit          │
│  accounts · quotes · candles · positions · trades · history │
│  WebSocket hub · signed webhooks                            │
└───────────────┬─────────────────────────────────────────────┘
                │  per-account bridge token (HMAC)
                ▼
┌─────────────────────────────────────────────────────────────┐
│  Account transport (one per MT5 account)                    │
│  a) pull/push MQL5 bridge EA  → real terminal               │
│  b) simulated market          → dev, preview, tests         │
│  c) (extension) Wine-side MetaTrader5 Python adapter        │
└───────────────┬─────────────────────────────────────────────┘
                │  single writer
                ▼
        MT5 terminal (Wine on Linux, or a Windows VPS)
        · one EA per terminal · one terminal per account
```

## Self-hosted: no MetaApi dependency

This gateway is not a client of MetaApi. There is no MetaApi account, API
key, SDK, relay or outbound call anywhere in the code path. It is also not
a wrapper around the `MetaTrader5` pip package (which is Windows-only and
single-connection).

| Layer | What it depends on |
|---|---|
| API tier | FastAPI + SQLite, all in this repository |
| Account transport | the MQL5 bridge EA inside *your* terminal, or the built-in simulator |
| Broker access | the MT5 terminal's own broker login |
| Market data | the terminal (`CopyRates`, `SymbolInfoDouble`, …) |
| Webhooks | your own HTTPS endpoint, optional |
| Streaming | the gateway's own WebSocket hub |

The only network paths are:

```
your clients ──▶ your gateway          (REST + WebSocket)
your terminal EA ──▶ your gateway      (outbound state/command HTTP)
your gateway ──▶ your webhook endpoint (optional)
```

Nothing leaves your infrastructure. For an air-gapped deployment, set
`ABUTRON_GATEWAY_WEBHOOKS=false`, keep accounts on the simulated transport
or a private LAN bridge, and no external host is contacted.

### Runs as part of the ABUTRON platform

By default the platform process (`python -m abutron.main`) starts the
gateway as a **second listener in the same process**:

```
abutron-portal.service
├── portal + APEX engine      0.0.0.0:19743
└── MT5 API gateway           0.0.0.0:19744   (ABUTRON_GATEWAY_ENABLED=true)
```

They share the deployment and data root but not their auth: the portal's
cookie sessions cannot reach gateway routes, and gateway API keys cannot
reach the operator portal. The gateway has its own database
(`data/gateway.db`) and its own tenant registry.

- Disable embedded mode with `ABUTRON_GATEWAY_ENABLED=false`.
- Run it separately with `./run-gateway.sh` or the
  `deploy/systemd/abutron-gateway.service` unit.
- A gateway startup failure is logged and never takes the autotrader down.

## Why not just the `MetaTrader5` Python package?

Because it is not a cloud API. The official package:

- runs **only on Windows** (or inside a Wine prefix),
- connects to a **local** terminal, not a remote one,
- has **one global connection per process** — you cannot multiplex
  client accounts in one process,
- exposes no authentication, tenancy, streaming protocol or audit.

MQL5 also has no built-in HTTP server. It has `WebRequest` (outbound
HTTP), `SocketConnect` (raw sockets), named pipes and file I/O. Therefore
every hosted MT5 API is some combination of:

1. **A terminal per account** (containerised Windows/Wine), plus
2. **An agent inside the terminal** (EA or Python) that talks to a
   control plane, plus
3. **A multi-tenant API tier** that owns keys, routing, limits and
   billing.

This repository now has all three. The agent is a pull/push bridge EA,
which is the NAT-friendly option: the terminal only makes outbound HTTP
requests, so it works behind a client's firewall with no inbound port.

## What is implemented here

| Capability | Where |
|---|---|
| Tenant keys, hashed at rest, scopes, rate limits | `src/abutron/gateway/auth.py` |
| Accounts, snapshots, commands, idempotency, webhooks, audit | `src/abutron/gateway/store.py` |
| Guards: control mode, kill switch, lot/position/loss limits, symbol allowlist | `src/abutron/gateway/service.py` |
| REST API + OpenAPI + WebSocket + bridge plane | `src/abutron/gateway/app.py` |
| Simulated full MT5 behaviour (quotes, bars, orders, deals) | `src/abutron/gateway/transports/simulated.py` |
| Hosted bridge transport (pull commands, push state) | `src/abutron/gateway/transports/bridge.py` |
| Signed webhooks with persisted retries | `src/abutron/gateway/webhooks.py` |
| MQL5 bridge EA for a real terminal | `mql5/AbuGatewayBridge.mq5` |
| Python client + smoke test | `examples/gateway_client.py` |
| Fake terminal bridge harness (no MT5/MetaApi) | `examples/bridge_terminal_demo.py` |
| Embedded platform runtime (portal + gateway, one process) | `src/abutron/gateway/embedded.py` |
| Read-only mirror of engine-managed accounts | `src/abutron/gateway/transports/engine.py` |
| Tests (35) | `tests/test_gateway.py`, `tests/test_gateway_platform.py` |

## The single-writer invariant

The APEX autotrader already in this repo and the API gateway are two
products that can touch the same broker account. Letting both write is how
duplicate positions and unexplained drawdowns happen. The gateway therefore
gives every account exactly one authority:

| `control_mode` | Who may trade | Intended use |
|---|---|---|
| `api` | Only this gateway | A client bot/developer |
| `read_only` | Nobody via the API | Monitoring, analytics, copy-feed source |

Accounts that the platform already automates are **mirrored read-only**
into the API (see transport 3 below). They appear under the
`tnt_platform` tenant with `market_data_mode: "engine"`,
`control_mode: "read_only"` and `trading_enabled: false`; every trade or
enable-trading call returns `409 ENGINE_MANAGED_ACCOUNT`. The engine keeps
the only writer.

As a second safety net, *explicit* account creation checks the engine's
registry: if a matching `login`/`server` is currently `effective_on` under
automation, the gateway returns `409 AUTOMATION_WRITER_ACTIVE` instead of
granting order authority. The check is fail-open only when the engine DB
cannot be read (standalone gateway deployment).

On the terminal side: attach **either** `ApexNativeWriter.mq5` (automation)
**or** `AbuGatewayBridge.mq5` (API), never both.

## Transports

### 1. Simulated (`market_data_mode: "simulated"`)

A deterministic, in-process MT5-shaped account. Full behaviour: mean
reverting quotes, synthetic OHLC history, market and pending orders, SL/TP
validation, partial closes, commission, deal/history-order records, and
state durable across restarts. This is what the preview and tests run on.
It is not a backtester — it is an API contract test double with a
plausible market attached.

### 2. Bridge (`market_data_mode: "bridge"`)

A real terminal runs `AbuGatewayBridge.mq5`:

```
EA  ──POST /internal/bridge/{id}/state────────────▶ gateway
EA  ──GET  /internal/bridge/{id}/commands?wait=1──▶ gateway
EA  ──POST /internal/bridge/{id}/commands/{cid}/result──▶ gateway
```

- The EA pushes account, positions, orders, prices, candles, deals and
  history orders every few seconds.
- The EA pulls pending trade commands. `format=text` returns a flat
  `key=value;key=value` line, which is far safer to parse in MQL5 than
  nested JSON.
- Each command is delivered **at most once**. If a bridge dies mid-send,
  the command stays `SENT`; that is surfaced honestly rather than retried
  into a possible duplicate broker position.
- The public REST call waits `ABUTRON_GATEWAY_COMMAND_TIMEOUT` seconds
  (default 8) for a result. If the bridge does not answer, the API returns
  **202** with a `command_id` and `poll_url` — never a fabricated fill.

Bridge authentication is a per-account HMAC token derived from a server
secret and a per-account salt. Rotating the salt (`POST
/v1/accounts/{id}/bridge-token`) invalidates the old token; deleting the
account invalidates everything.

### 3. Engine mirror (`market_data_mode: "engine"`, read-only)

At start-up the gateway scans the platform's account registry and mirrors
every engine-managed account into the API under tenant
`tnt_platform` (`ABUTRON_GATEWAY_ENGINE_TENANT`). The mirror is
idempotent: accounts are keyed by `engine_connection_id`, so a restart
updates the existing API account instead of creating duplicates.

| Data | Source |
|---|---|
| Login, server, broker, symbols, environment | engine account registry |
| Balance / equity / leverage / currency | native identity export (`ABUTRON_NATIVE_ACCOUNT_V2_*.txt`) |
| Quotes and candles | native bars export (`ABUTRON_NATIVE_BARS_V2_*.txt`) |
| Open positions | engine `order_receipts` (OPEN/PROTECTED, minus closed tickets) |
| Deal history | engine `ledger` (CLOSE/CASH rows) |
| Order history | engine `order_receipts` |

Guarantees and limits:

- **Never writes.** `execute()` raises `READ_ONLY_ACCOUNT`; the service
  guard refuses before the transport is called. `trading_enabled` cannot
  be turned on via `PATCH /v1/accounts/{id}`.
- **Freshness is honest.** The transport reads the export file's mtime.
  Older than 90s → `connected=false`, `stale=true`; reads still return
  the last known book, clearly flagged.
- **No synthetic bars.** Asking for a timeframe finer than the writer's
  native bars returns `400 TIMEFRAME_NOT_AVAILABLE`; coarser timeframes
  are aggregated from real bars.
- **Pending orders are not mirrored.** They live only inside the terminal
  and are absent from the native export; the API returns an empty list
  rather than inventing them.
- **Profit on open positions is marked-to-market** from the latest native
  bar plus the receipt's commission/swap, so it is an estimate, not a
  broker statement.

Disable with `ABUTRON_GATEWAY_ADOPT_AUTOMATION=false`. To read these
accounts, mint a key for the platform tenant:

```bash
.venv/bin/python -m abutron.gateway.admin create-key \
  --tenant tnt_platform --label "read-only integration"
```

```bash
curl -H "Authorization: Bearer abk_..." \
  http://localhost:19744/v1/accounts/eng_mt5_<id>/positions
```

### 4. Wine-side `MetaTrader5` adapter (extension point)

The same transport contract can be implemented by a small Windows-Python
process inside each Wine prefix using the official `MetaTrader5` package,
speaking to the gateway over the same command/state protocol. Choose this
when you need MT4 parity or APIs the EA surface does not cover. The
interface is `Mt5Transport` in `transports/base.py`; nothing above it
changes.

## REST API

Base URL: `http://<host>:19744`. Auth: `Authorization: Bearer abk_...` or
`X-API-Key: abk_...`. Full interactive schema: `/docs` (Swagger) and
`/redoc`.

### Ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, account/stream counts |
| GET | `/v1/me` | Tenant + key identity and scopes |
| GET | `/v1/usage` | Rate-limit counters and totals |
| GET | `/v1/audit?limit=` | Tenant audit trail |
| POST | `/v1/admin/tenants` | Mint a tenant + key (needs `X-Admin-Secret`, disabled unless configured) |

### Accounts

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/accounts` | Provision an MT5 account |
| GET | `/v1/accounts` | List tenant accounts |
| GET | `/v1/accounts/{id}` | Account metadata and live status |
| PATCH | `/v1/accounts/{id}` | Kill switch, limits, control mode, symbols |
| DELETE | `/v1/accounts/{id}` | Revoke account + bridge token |
| POST | `/v1/accounts/{id}/bridge-token` | Rotate the bridge token |
| GET | `/v1/accounts/{id}/snapshot` | Account + positions + orders + prices |

Provisioning:

```bash
curl -X POST http://localhost:19744/v1/accounts \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "login": 12345678,
    "server": "Acme-Live",
    "broker": "acme",
    "environment": "live",
    "market_data_mode": "bridge",
    "control_mode": "api",
    "symbols": ["EURUSD.r", "GBPUSD.r", "XAUUSD.r"],
    "max_lot": 2.0,
    "max_open_positions": 10,
    "daily_loss_limit_usd": 500,
    "symbol_allowlist": ["EURUSD.r", "GBPUSD.r"]
  }'
```

The optional `password` is accepted for provisioning but **never stored or
returned**; `credentials_accepted` only confirms receipt. For bridge
accounts, store the terminal credentials in the terminal profile (or a
secret manager consumed by the worker) — not in this database.

### Market data

| Method | Path |
|---|---|
| GET | `/v1/accounts/{id}/information` |
| GET | `/v1/accounts/{id}/symbols` |
| GET | `/v1/accounts/{id}/symbols/{symbol}` |
| GET | `/v1/accounts/{id}/symbols/{symbol}/price` |
| GET | `/v1/accounts/{id}/symbols/{symbol}/candles?timeframe=5m&startTime=&limit=` |

Timeframes: `1m, 5m, 15m, 30m, 1h, 4h, 1d` (MT5 names `M5`, `H1`, … are
also accepted). Symbol aliasing is suffix-aware: `EURUSD` resolves to
`EURUSD.r` when that is the broker's real name.

### Trading

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/accounts/{id}/trade` | Market or pending order |
| POST | `/v1/accounts/{id}/positions/{pid}/modify` | Change SL/TP |
| POST | `/v1/accounts/{id}/positions/{pid}/close` | Full or partial close |
| POST | `/v1/accounts/{id}/orders/{oid}/cancel` | Cancel pending order |
| GET | `/v1/accounts/{id}/commands/{cid}` | Poll a queued command |
| GET | `/v1/accounts/{id}/positions` | Open positions |
| GET | `/v1/accounts/{id}/orders` | Pending orders |
| GET | `/v1/accounts/{id}/history/deals?from=&to=&limit=` | Deal history |
| GET | `/v1/accounts/{id}/history/orders?from=&to=&limit=` | Order history |

```bash
curl -X POST http://localhost:19744/v1/accounts/$AID/trade \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: bot-42-20261118-001" \
  -d '{
    "action": "market", "side": "buy", "symbol": "EURUSD",
    "volume": 0.10, "stop_loss": 1.0800, "take_profit": 1.0920
  }'
```

Outcomes:

- `200 EXECUTED` — a transport reported a real execution (ticket/deal ids).
- `202 QUEUED` — bridge has not answered; poll `poll_url`.
- `422 REJECTED` — transport/broker refused; `retcode` is machine-readable.
- `409` — a policy guard (kill switch, read-only, limits, idempotency).

Writes accept `Idempotency-Key`. Replaying the same key with the same body
returns the original response with `"idempotent_replay": true`; reusing it
with a different body returns `409 IDEMPOTENCY_KEY_REUSED`.

### Errors

```json
{
  "error": {
    "code": "MAX_OPEN_POSITIONS",
    "message": "max_open_positions reached for this account",
    "request_id": "req_9f2c...",
    "details": {}
  }
}
```

Stable codes include `UNAUTHORIZED`, `FORBIDDEN_SCOPE`, `RATE_LIMITED`,
`NOT_FOUND`, `VALIDATION_ERROR`, `TRADING_DISABLED`, `ACCOUNT_READ_ONLY`,
`AUTOMATION_WRITER_ACTIVE`, `SYMBOL_NOT_ALLOWED`, `INVALID_VOLUME`,
`INVALID_STOPS`, `MAX_OPEN_POSITIONS`, `DAILY_LOSS_LIMIT`,
`IDEMPOTENCY_KEY_REUSED`, `ACCOUNT_NOT_CONNECTED`.

## WebSocket stream

```
ws://<host>:19744/v1/stream?account_id=acc_...&api_key=abk_...
```

Optional `channels=quotes,positions,orders,trades,account,commands` and
`symbols=EURUSD,XAUUSD`. The same key may also be presented via
`Authorization` / `X-API-Key` headers.

On connect the server sends `connected`, then an `account` event and a
`snapshot` (positions/orders/prices) if those channels are subscribed. It
then streams events. Inbound messages:

```json
{"type":"subscribe","channels":["quotes","trades"],"symbols":["XAUUSD"]}
{"type":"unsubscribe","channels":["quotes"]}
{"type":"ping"}
```

Every event has `type`, `account_id`, `sequence`, `timestamp` and `data`.
Slow consumers lose their oldest event rather than back-pressuring the
producer. A `heartbeat` arrives every 20s when idle.

## Webhooks

```bash
curl -X POST http://localhost:19744/v1/webhooks \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/hooks/mt5",
       "events":["trade.executed","position.closed"]}'
```

The response contains a `whsec_...` secret **once**. Deliveries are signed:

```
X-Abutron-Signature: t=1700000000,v1=<hex hmac_sha256(secret, t + "." + body)>
```

Verify the timestamp freshness (<= 5 min) and compare in constant time.
Deliveries are persisted before the first attempt, retried with backoff
(2s, 10s, 30s, 120s, 600s) and marked `FAILED` after
`ABUTRON_GATEWAY_WEBHOOK_ATTEMPTS` attempts.

Events currently emitted: `trade.executed`, `trade.rejected`,
`position.opened`, `position.closed`, `order.placed`, `order.cancelled`,
`account.updated`, `account.connected`, `*`.

## Bridge protocol (for the EA author)

All bridge endpoints require `X-Bridge-Token: <token>` (or Bearer) and are
excluded from the public OpenAPI schema.

`GET /internal/bridge/{id}/handshake`

```json
{"account_id":"acc_...","login":12345678,"server":"Acme-Live",
 "broker":"acme","platform":"mt5","symbols":["EURUSD.r"],
 "control_mode":"api","trading_enabled":true,
 "command_poll_wait_s":20,"state_push_interval_s":5}
```

`POST /internal/bridge/{id}/state` — the EA pushes a snapshot:

```json
{
  "sequence": 17,
  "account": {"login":12345678,"currency":"USD","leverage":500,
              "balance":10000.0,"equity":10012.5,"margin":210.0,
              "free_margin":9802.5,"margin_level":4767.9,
              "trade_allowed":true,"connected":true},
  "symbols": [{"symbol":"EURUSD.r","digits":5,"point":0.00001,
               "contract_size":100000,"min_volume":0.01,
               "max_volume":50,"volume_step":0.01,"spread":0.00002}],
  "prices": {"EURUSD.r":{"bid":1.08500,"ask":1.08520,
                          "time":"2026-11-18T09:00:00Z"}},
  "candles": [{"symbol":"EURUSD.r","timeframe":"M5",
               "open_time":"2026-11-18T08:55:00Z",
               "close_time":"2026-11-18T09:00:00Z",
               "open":1.0849,"high":1.0854,"low":1.0847,
               "close":1.0852,"volume":812,"closed":true}],
  "positions": [{"position_id":"123456","symbol":"EURUSD.r",
                 "side":"buy","volume":0.1,"open_price":1.0800,
                 "current_price":1.0852,"stop_loss":1.0750,
                 "take_profit":1.0950,"profit":5.2,"swap":0,
                 "commission":-0.35,"comment":"","magic":0,
                 "opened_at":"2026-11-18T08:00:00Z"}],
  "orders": [],
  "deals": [],
  "history_orders": []
}
```

`GET /internal/bridge/{id}/commands?wait=1&format=text` returns, per line:

```
command_id=cmd_ab12;kind=market;symbol=EURUSD.r;side=buy;volume=0.1;stop_loss=1.08;take_profit=1.092
command_id=cmd_cd34;kind=pending;symbol=XAUUSD.r;side=sell;volume=0.05;order_type=limit;price=2400;...
command_id=cmd_ef56;kind=modify;position_id=123456;stop_loss=1.081;take_profit=0
command_id=cmd_gh78;kind=close;position_id=123456;volume=0.05
command_id=cmd_ij90;kind=cancel;order_id=998877
```

`POST /internal/bridge/{id}/commands/{cid}/result`:

```json
{"accepted":true,"retcode":"DONE","message":"Order executed",
 "order_id":"998877","position_id":"123456","deal_id":"554433",
 "price":1.0852,"volume":0.1}
```

Setup steps for the EA:

1. In MT5: **Tools → Options → Expert Advisors → Allow WebRequest for
   listed URLs** and add your gateway base URL (`http://host:19744`).
2. Attach `AbuGatewayBridge.mq5` to one chart in the account's terminal
   with `InpGatewayBaseUrl`, `InpConnectionId`, `InpBridgeToken` and the
   symbol list.
3. Allow algorithmic trading. The EA does not expose a remote login;
   the terminal session is the broker session.
4. Attach nothing else that trades. One EA per terminal.

## Security model

- Keys are shown once, stored as `sha256(pepper || secret)` plus a public
  lookup prefix. Comparison is constant time.
- Scopes: `read`, `trade`, `manage` — a read-only key cannot trade or
  provision.
- Tenant isolation is enforced in the repository layer: every account
  lookup is keyed by tenant. Cross-tenant ids return **404** to avoid
  account-id enumeration.
- Bridge tokens are per-account HMACs with a rotatable salt.
- Rate limits are per key (plus a stricter trade bucket), returned as
  `X-RateLimit-*` headers, with `Retry-After` on 429.
- Every mutation is written to an append-only audit table with actor key,
  account, IP and detail. Secrets are never logged.
- Passwords are never persisted. Production deployments should put the
  pepper, bridge secret and any terminal credentials in a secret manager.

## Deployment and scaling

Single node (what this repo ships):

```bash
./run.sh                              # portal :19743 + gateway :19744
# or the gateway alone:
./run-gateway.sh                      # 0.0.0.0:19744
python -m pytest tests/test_gateway.py tests/test_gateway_platform.py -q
```

With embedded mode the gateway lives in `abutron-portal.service`; for a
split deployment use `abutron-gateway.service` and set
`ABUTRON_GATEWAY_ENABLED=false` on the portal unit.

Mint tenants without exposing an admin HTTP secret:

```bash
# same ABUTRON_DATA_ROOT / ABUTRON_GATEWAY_* env as the service
python -m abutron.gateway.admin create-tenant --name "Acme Ltd"
python -m abutron.gateway.admin create-key --tenant tnt_...
python -m abutron.gateway.admin list-tenants
```

The key is printed once; only its hash is stored.

For real accounts, run the existing per-account systemd template
(`deploy/systemd/abutron-mt5-worker@.service`) or a container per MT5
terminal, each with its own Wine prefix, and point each bridge EA at the
gateway. The gateway itself needs one process, a persistent volume for
SQLite (or Postgres, below) and outbound access for webhooks.

To scale past one API process:

| Concern | Upgrade |
|---|---|
| Database | Swap SQLite for Postgres; the store methods are the only seam |
| WebSocket fan-out | Redis pub/sub (or NATS) behind `StreamHub.publish` |
| Command dispatch | The SQLite queue already decouples producers/consumers; move it to Postgres `SKIP LOCKED` or a queue |
| Terminal density | One Wine prefix + terminal per account; ~0.5–1 vCPU and ~400–700 MB RSS each under active ticks |
| Regions | Run a gateway + terminal pool per broker region; route by account metadata. Latency to the broker matters more than latency to the client |
| Abuse | Move the in-process limiter to Redis; add per-tenant quotas and billing metering on `/v1/usage` |

Expected latency (honest numbers, not marketing):

- Simulated transport: single-digit milliseconds.
- All blocking transport calls run in Starlette's worker threadpool, not
  on the event loop, so a slow bridge never blocks the API or another
  tenant. Size `ABUTRON_GATEWAY_COMMAND_TIMEOUT` against that pool.
- Bridge EA: one poll interval (default 250 ms–1 s) + broker round trip.
  A 1s poll gives ~0.3–1.3s end-to-end for a market order. Long-polling
  lowers this but blocks the EA's event loop, so 250 ms polling is the
  practical default.
- Market data freshness equals the EA push interval (default 1–5s). If you
  need sub-second tick streaming, push ticks from `OnTick` (rate-limited)
  or move to a socket-based EA.

MetaApi's advantage is not a magic MT5 API — it is operational: thousands
of managed terminals, regions, failover and an SDK ecosystem. This
gateway gives you the same architecture and lets you own the terminals.

## What is deliberately *not* built (yet)

- MetaTrader 4. The transport contract is platform-neutral; the EA is MQL5.
- socket.io compatibility. The stream is native WebSocket + JSON;
  socket.io SDK parity is a wrapper, not a rewrite.
- Copy trading / investor access / MetaStats-style analytics.
- Billing and metering beyond `/v1/usage`.
- Full automated terminal login. Provisioning accepts a password but does
  not store it; terminal login is handled by the terminal profile or the
  worker's secret injection.
- Pending orders in the engine mirror (the native export does not carry
  them). Positions, deals and order history are mirrored.
- Sub-5-minute candles for engine mirrors when the writer publishes M5.
- MT5 manager-side operations (broker Admin API) — that is a different
  product and requires broker authority.
- Horizontal streaming without Redis/NATS (single-process hub today).

## Suggested roadmap

1. **Harden the bridge** — compile the EA, run one demo account through a
   24h soak, add a `SENT`-timeout reconciler that queries the terminal for
   the broker order before resolving an unknown outcome.
2. **Postgres + Redis** — unlock multiple API replicas.
3. **Wine MT5 Python adapter** — MT4/MT5 parity and richer history.
4. **SDKs** — Python, Node, Go generated from OpenAPI plus a thin WS client.
5. **Billing** — usage metering, per-plan account caps, invoice export.
6. **Operator console** — account fleet health, terminal restarts, bridge
   version pinning, per-tenant usage dashboards.

## Running it now

```bash
# one-time
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# start the platform (portal + gateway embedded)
./run.sh
# gateway only: ./run-gateway.sh

# in another shell — simulated account round trip
export ABUTRON_GATEWAY_KEY=abk_demo_0123456789abcdef0123456789abcdef
python examples/gateway_client.py --demo

# bridge round trip with NO MetaApi and NO real MT5 terminal:
# a local process behaves exactly like AbuGatewayBridge.mq5
python examples/bridge_terminal_demo.py --demo
```

`bridge_terminal_demo.py` creates a bridge-backed account, pushes terminal
state, pulls the queued order, executes it, posts the real result and
closes the position through the public REST API — the same wire protocol a
real terminal uses.

Demo mode (`ABUTRON_GATEWAY_DEMO=true`, the default) seeds one tenant and
one simulated account so the API is usable immediately. Set it to `false`
and configure `ABUTRON_GATEWAY_ADMIN_SECRET` to mint tenants explicitly,
or use `POST /v1/admin/tenants` with `X-Admin-Secret`.
