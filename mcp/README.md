# ABUTRON Own MCP Gateway — V1 (No OpenAI)

> **You asked for it: Own version, no OpenAI shit.** This is ABUTRON's self-hosted Desktop Commander-based MCP gateway, built from https://desktopcommander.app/mcp/#installation but decoupled from OpenAI tunnel-client / Plus limits.

**Upstream:** `wonderwhy-er/DesktopCommanderMCP` — 1M+ weekly npm downloads, 10k stars, `npx @wonderwhy-er/desktop-commander` / `curl install.sh | bash`
**ABUTRON change:** We vendor the same stdio core (`@wonderwhy-er/desktop-commander` 0.3.1+) but run it under ABUTRON-owned Node `v22.x` isolated path, ABUTRON allowed/blocked lists, and ABUTRON HTTP/SSE gateway — **zero OpenAI tunnel, zero Plus quota, zero OpenAI API key**.

## Two modes (your choice)

| Mode | Transport | Install | Works with |
|------|-----------|---------|------------|
| **Local stdio** | `stdio` via `node dist/index.js` | `npx @wonderwhy-er/desktop-commander setup` or our `install-abutron-mcp-own-v1.sh --local` | Claude Desktop, Cursor, Windsurf, any MCP stdio client |
| **Remote Own Gateway** | HTTP + SSE (`/mcp/sse` + `/mcp/messages`) with API-key auth | `install-abutron-mcp-own-v1.sh --gateway` → systemd `abutron-mcp-gateway.service` | Any web client that speaks MCP SSE — no OpenAI Business gate |

Compare to legacy `abutron-ops/ops/mcp_gateway`:
- Legacy = isolated Node 22.23.4 ✅ + Desktop Commander 0.3.1 ✅ + *OpenAI* `tunnel-client v0.0.20` + `abutron-mcp-tunnel.service` (outbound, needs `CONTROL_PLANE_API_KEY` + `tunnel-id`, Plus-blocked)
- **Own V1 = isolated Node ✅ + Desktop Commander core ✅ + ABUTRON `abutron-mcp-gateway` (Fastify SSE, API key in `/etc/abutron-mcp/gateway.env` 0600) — no OpenAI**

## Quick start (own local — 30s)

```bash
#On ABUTRON Ubuntu localhost (or any dev machine):
git clone https://github.com/abutronmall-netizen/ABUTRON-FX-V9
cd ABUTRON-FX-V9
bash mcp/scripts/install-abutron-mcp-own-v1.sh --local

# Verify stdio MCP (same smoke as abutron-ops 42483971911):
/opt/abutron-mcp/node/bin/node /opt/abutron-mcp/desktop-commander/dist/index.js --no-onboarding
# then send MCP initialize → notifications/initialized → tools/list
```

## Quick start (own remote gateway — self-hosted)

```bash
sudo bash mcp/scripts/install-abutron-mcp-own-v1.sh --gateway
sudo systemctl enable --now abutron-mcp-gateway.service
systemctl is-active abutron-mcp-gateway.service  # active
curl -H "Authorization: Bearer YOUR_OWN_KEY" http://localhost:3467/healthz
curl -H "Authorization: Bearer YOUR_OWN_KEY" http://localhost:3467/mcp/tools
```

No `platform.openai.com` tunnels, no `CONTROL_PLANE_API_KEY` argv leak, no Plus monthly tool quota.

## Security (ABUTRON-hardened, not permissive Desktop Commander)

- Allowed dirs only: `/opt/abutron-fx`, `/var/lib/abutron-mcp`, `/home/abutron*` (configurable in `mcp/config/abutron-mcp.config.json`)
- Blocked commands: `sudo`, `rm -rf /`, `mkfs`, `dd`, MT5 LIVE writes, CRM ON/OFF, `abutron-gh-apex-*` staging bypass
- Runs as `abutronmcp:abutronmcp`, `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp` (see systemd unit)
- LIVE always OFF — gateway never touches broker accounts

See `docs/ABUTRON_OWN_GATEWAY_ARCHITECTURE.md` for full design.

