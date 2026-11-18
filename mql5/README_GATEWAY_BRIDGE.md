# AbuGatewayBridge.mq5 — setup

This EA connects one MT5 terminal to the multi-tenant ABUTRON gateway
(`docs/MT5_GATEWAY.md`). It is the **only** order authority for an API
account.

## 1. Provision the account

```bash
curl -X POST http://gateway:19744/v1/accounts \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"login":12345678,"server":"Acme-Demo","broker":"acme",
       "market_data_mode":"bridge","control_mode":"api",
       "symbols":["EURUSD.r","GBPUSD.r","XAUUSD.r"]}'
```

The response contains `account_id` and a one-time `bridge.token`.

## 2. Allow the gateway URL in the terminal

**Tools → Options → Expert Advisors → Allow WebRequest for listed URL**,
then add the exact base URL, e.g. `http://gateway:19744`.
MT5 matches the URL prefix; no trailing slash is needed.

## 3. Attach the EA

Compile `AbuGatewayBridge.mq5` in MetaEditor, then attach it to one chart
with:

| Input | Value |
|---|---|
| `InpGatewayBaseUrl` | `http://gateway:19744` |
| `InpConnectionId` | `acc_...` from provisioning |
| `InpBridgeToken` | `bridge.token` from provisioning |
| `InpSymbols` | comma-separated broker symbols |
| `InpStateIntervalSec` | `5` (state push cadence) |
| `InpPollMs` | `250` (command latency vs CPU) |
| `InpEnableTrading` | `true` |
| `InpMagic` | your EA magic |

Then enable **Algo Trading**. The terminal session is the broker login;
the bridge never receives or stores the account password.

## 4. Verify

- The EA log prints `state pushed seq=…` every few seconds.
- The gateway shows the account as `CONNECTED`.
- `GET /v1/accounts/{id}/information` returns real balance/equity.
- A trade call returns `200 EXECUTED` with a broker ticket, or `202`
  if the EA has not polled yet (poll the command id).

## Rules

- **One EA per terminal.** Never attach this and `ApexNativeWriter.mq5`
  to the same account.
- Rotating the bridge token (`POST /v1/accounts/{id}/bridge-token`)
  invalidates the old one — update the EA input.
- The EA reports real `OrderSend` retcodes. Treat `SENT`/`TIMEOUT`
  commands as unknown outcomes and reconcile with the terminal before
  retrying a trade.
- Timestamps are converted from broker server time to UTC before upload.
