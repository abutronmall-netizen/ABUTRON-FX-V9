# ABUTRON Own Gateway Architecture V1 — No OpenAI

**Goal:** Replace OpenAI `tunnel-client v0.0.20` + Plus-gated Remote MCP with ABUTRON-owned self-hosted gateway, using same Desktop Commander core from https://desktopcommander.app/mcp/#installation

## Why own vs OpenAI

| Legacy (`abutron-ops`) | Own V1 (`ABUTRON-FX-V9/mcp`) |
|---|---|
| `tunnel-client v0.0.20` at `/usr/local/bin/tunnel-client` | **No tunnel-client** — Fastify gateway at `/opt/abutron-mcp/gateway` |
| `abutron-mcp-tunnel.service` (needs `CONTROL_PLANE_API_KEY` + `tunnel-id` at `platform.openai.com`) | `abutron-mcp-gateway.service` (needs own `ABUTRON_MCP_API_KEY` in `/etc/abutron-mcp/gateway.env` 0600) |
| Plus = **blocked** — custom MCP requires Business/Enterprise per https://help.openai.com/en/articles/12584461 | Works with **any** MCP client that speaks `stdio` or `SSE` — Claude Desktop, Cursor, Windsurf, custom Python via `mcp` SDK |
| Quota: Remote Desktop Commander host relay monthly quota exhausted | No quota — you host it on your Ubuntu `localhost` |

Same hardening as legacy kept: `/opt/abutron-mcp/node` v22.23.4 pinned SHA256, `abutronmcp` unprivileged, `NoNewPrivileges`, `ProtectSystem=strict`, etc.

## Components

```
ABUTRON-FX-V9/mcp/
  config/abutron-mcp.config.json   # allowed dirs, blocked commands, LIVE=OFF
  server/
    src/gateway.ts                 # Fastify + SSE + stdio bridge to DCM
    package.json                   # @modelcontextprotocol/sdk + fastify + @wonderwhy-er/desktop-commander
  scripts/install-abutron-mcp-own-v1.sh  # --local | --gateway (root, pinned Node, smoke test)
  systemd/abutron-mcp-gateway.service    # own systemd (not abutron-mcp-tunnel.service)

On Ubuntu localhost:
  /opt/abutron-mcp/node/bin/node          # isolated Node v22.23.4 (same pin as abutron-ops)
  /opt/abutron-mcp/desktop-commander/     # DCM core 0.3.1+ (npm)
  /opt/abutron-mcp/gateway/               # gateway built from this repo
  /etc/abutron-mcp/gateway.env             # ABUTRON_MCP_API_KEY=… (0600)
  /var/lib/abutron-mcp/                   # HOME for abutronmcp, XDG_CONFIG_HOME
```

## Flows

### Local stdio (simplest, no network)
```
Claude Desktop (mcpServers.abutron-own) ──stdio──> /opt/abutron-mcp/node/bin/node /opt/abutron-mcp/desktop-commander/dist/index.js --no-onboarding
```
Config for Claude Desktop `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "abutron-own": {
      "command": "/opt/abutron-mcp/node/bin/node",
      "args": ["/opt/abutron-mcp/desktop-commander/dist/index.js", "--no-onboarding"],
      "env": { "ABUTRON_MCP_CONFIG": "/opt/abutron-fx-v9/mcp/config/abutron-mcp.config.json" }
    }
  }
}
```
Same as `npx @wonderwhy-er/desktop-commander setup` but via isolated Node + ABUTRON config.

### Remote own gateway (HTTP/SSE) — replaces OpenAI tunnel
```
Any MCP client (web) ──HTTPS via Caddy/Tailscale──> 127.0.0.1:3467
  ├─ GET /healthz (no auth)
  ├─ GET /readyz (no auth)
  ├─ GET /mcp/sse  (SSE stream, Bearer ABUTRON_MCP_API_KEY)
  └─ POST /mcp/messages {jsonrpc:2.0, method: tools/list|tools/call|...} ──stdio bridge──> DCM child
```

**No public listener:** gateway binds `127.0.0.1:3467` only. Expose via:
- `Caddy` with `reverse_proxy 127.0.0.1:3467` + auto Let's Encrypt
- `Tailscale Serve/Funnel` (`tailscale serve https --bg 3467`)
- Self-hosted `cloudflared` alternative (not OpenAI), or WireGuard
- Never bind `0.0.0.0` without TLS + auth — see `ProtectSystem` in unit.

## Smoke parity (no regression)

Legacy verified: `MCP_INIT=PASS`, `MCP_TOOLS_LIST=PASS`, 25 tools, `NO_MCP_TOOL_INVOKED=YES` (runs 42483971911 etc.)

Own V1 reproduces same check in `install-abutron-mcp-own-v1.sh`:
```python
initialize (protocolVersion 2025-03-26) → notifications/initialized → tools/list
assert 1 <= len(tools) <= 100
```

Plus: `curl -H "Authorization: Bearer $ABUTRON_MCP_API_KEY" http://127.0.0.1:3467/mcp/tools` returns same PASS.

## Security (ABUTRON extra over vanilla DCM)

Vanilla DCM is powerful but permissive — it gives AI `read/write/terminal/process` over *any* allowed dir you configure. ABUTRON adds:

- **allowedDirectories** locked to `/opt/abutron-fx*`, `/var/lib/abutron-mcp`, `/home/abutron*` — not `/` or `/etc`
- **blockedCommands** include `sudo`, `rm -rf /`, `abutron-gh-apex-source-stage*`, `crm-toggle`, MT5 LIVE writes — gateway returns `403 BLOCKED_BY_ABUTRON_POLICY`
- **LIVE ALWAYS OFF** — gateway never has broker credentials (`permissions: {}` pattern from abutron-ops)
- **No deployment:** Even with gateway, installing EX5 into active MT5 terminals, CRM flip, order send, DB schema still require the *separate* `abutron-ops` locked stage path (hash-pin + offsite backup + rollback proof)

## Migration from legacy OpenAI tunnel

1. Keep `abutron-mcp-tunnel.service` disabled (legacy) — don't run both tunnels.
2. Run own installer: `sudo bash mcp/scripts/install-abutron-mcp-own-v1.sh --gateway`
3. Revoke OpenAI runtime key at `platform.openai.com/settings/organization/api-keys` and delete tunnel at `platform.openai.com/settings/organization/tunnels` if you no longer want legacy.
4. Point clients to own endpoint: `http://YOUR_HOST:3467/mcp/sse` with `Authorization: Bearer <from /etc/abutron-mcp/gateway.env>`

Legacy docs remain in `abutron-ops/ops/mcp_gateway/` for reference, but are not needed.

## Next iterations (not in V1)

- Persistent stdio session (this V1 spawns ephemeral child per `POST /mcp/messages`; V2 keeps long-lived child + session ID in SSE)
- Tool filtering: expose only allowlisted DCM tools (e.g., `read_file`, `list_directory`) and hide dangerous ones (`execute_command` gated)
- ABUTRON trading tools as native MCP tools (alternative to DCM wrappers): `abutron_verify_host`, `abutron_verify_staging`, `abutron_derive_risk_check` — pure read-only DEMO-only

