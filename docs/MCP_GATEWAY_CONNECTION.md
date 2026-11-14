# ABUTRON MCP Gateway — Connection Guide for ABUTRON-FX-V9

Source: `abutronmall-netizen/abutron-ops` (`ops/mcp_gateway/`) — verified 2026-11-08 UTC on Ubuntu `localhost` (`abutron-prod-01-readonly` runner).

## What the gateway is

- **Local MCP server:** Desktop Commander 0.3.1 stdio at `/opt/abutron-dcmcp/node_modules/@wonderwhy-er/desktop-commander/dist/index.js` — verified MCP `initialize` + `tools/list` PASS with 25 tools (run 42483971911).
- **Isolated runtime:** Node.js v22.23.4 pinned SHA256 `1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce` under `/opt/abutron-mcp/node` (global Node remains v18.19.1).
- **Tunnel client:** Official OpenAI Linux AMD64 `tunnel-client` v0.0.20 at `/usr/local/bin/tunnel-client` (verified `help quickstart` run 42485169933).
- **Systemd unit:** `abutron-mcp-tunnel.service` — **staged, disabled/inactive**, runs as unprivileged user `abutronmcp` with `HOME=/var/lib/abutron-mcp`, outbound-only, no public listener.

> Current state as staged: **Not connected** — tunnel ID + runtime key not configured. Daemon cannot connect to OpenAI without them. See `ops/mcp_gateway/OPERATE_PRIVATE_TUNNEL_V1.md`.

## ChatGPT Plus limitation (important)

Per OpenAI docs [Connecting custom MCP servers](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt):

> Full custom MCP read/write creation is limited to **Business / Enterprise / Edu** workspaces — **not available on ChatGPT Plus**.

- Even after activating the tunnel, this **Plus** chat will **not** gain direct Desktop Commander tools.
- The hosted Remote Desktop Commander relay quota exhaustion is a separate issue — local install does not reset it.
- Recommended Plus workflow: use the owner-gated GitHub Actions runner (`ABUTRON Reviewed Runner Operations V1`) for production changes.

## What “connect” means (3 levels)

1. **Verify local MCP (read-only, no secrets):** GitHub owner opens issue `ABUTRON OPS SMOKE DESKTOP COMMANDER V1` on `abutron-ops/main`. Workflow `abutron-dcmcp-local-smoke-v1.yml` runs on self-hosted runner `abutron-diagnostics` as `abutrongha` (non-root), tests MCP `initialize` → `tools/list` with temp HOME, never invokes tools. Expected `MCP_INIT=PASS`, `MCP_TOOLS_LIST=PASS`.

2. **Stage tunnel service (root, one-time, no secrets):** Already done — unit exists inactive. Preflight checks: `stage-tunnel-service-v1.sh preflight` → `MCP_GATEWAY_PREREQUISITES_PASS`. Status: `systemctl is-active/is-enabled abutron-mcp-tunnel.service` → inactive/disabled is expected before activation.

3. **Activate private tunnel (requires you, operator-only):**

   ### a) Create tunnel in OpenAI Platform (your account)
   - Visit [Platform → Organization → Tunnels](https://platform.openai.com/settings/organization/tunnels) → create private tunnel for ABUTRON → copy exact `tunnel_id`.
   - Visit [Platform → API keys](https://platform.openai.com/settings/organization/api-keys) → create **restricted runtime key** with *only* `Tunnels Read + Use` (no admin). Do NOT paste key into chat/GitHub issues/shell args.

   ### b) One-time Ubuntu root command (on production localhost)
   ```bash
   cd /root/abutron-ops
   git fetch origin
   git pull --ff-only origin main
   bash -n ops/mcp_gateway/configure-private-tunnel-v1.sh
   bash ops/mcp_gateway/configure-private-tunnel-v1.sh YOUR_REAL_TUNNEL_ID
   # prompts for API key hidden; stores to root-owned 0600 /etc/abutron-mcp/tunnel.env
   # runs: tunnel-client init --profile abutron-production --tunnel-id ... --mcp-command /usr/local/libexec/abutron-dcmcp-agent-v1
   # runs: tunnel-client doctor --explain (must pass)
   # then: systemctl enable --now abutron-mcp-tunnel.service
   ```

   ### c) Validate (do NOT reveal secret)
   ```bash
   systemctl is-active abutron-mcp-tunnel.service
   systemctl is-enabled abutron-mcp-tunnel.service
   systemctl show abutron-mcp-tunnel.service -p User -p MainPID -p ActiveState -p SubState --no-pager
   # expected User=abutronmcp, ActiveState=active
   # health probes (local only):
   # curl http://localhost:PORT/healthz /readyz /health/mcp  (see tunnel-client docs)
   ```

   End-to-end proof = supported MCP client initializes, lists tools, executes benign read-only op through tunnel. Active systemd ≠ Plus browser connected.

## Recovery / Stop
```bash
systemctl disable --now abutron-mcp-tunnel.service
# revoke runtime key + tunnel in Platform if retiring
# never cat /etc/abutron-mcp/tunnel.env into logs
```

## For ABUTRON-FX-V9 (this repo)
This repo (`arena/873cd835-abutron-fx-v9` from `c81eede`) currently has only README. Options:

- **A)** Keep FX-V9 decoupled; use `abutron-ops` Actions for prod verification (safest, matches security invariants: no PR execution on prod, `permissions: {}`, 8-min timeout, owner+main+gating).
- **B)** Add an MCP *client* (not server) in FX-V9 that talks to a Business/Edu workspace once tunnel is active — e.g., Python `mcp` client pointing at tunnel URL, read-only tools first.
- **C)** Extend reviewed operations workflow to narrow MT5 deployment (future, requires separate root-approved stager + exact sudoers + offsite backup proof — see ops README).

No auto-install of MCP scripts via GitHub as root — per verified completion definition, changes require real `abutron-ops` host evidence, not just a successful Actions job.

## Security invariants (from abutron-ops/README.md)
- Never execute PR code on production host; no input-derived shell.
- No trading/broker/Restic/CRM credentials in runner jobs.
- Single concurrency group, fail-closed if stagers missing — missing `abutron-gh-apex-staging-verify` commands are intentional, not implicit sudo.
- Do not bypass Deriv 6207 DEMO guardian, V7 CRM switch, writer exclusivity, or 0.10/2/3/20 risk rules via Actions.

## References
- `ops/mcp_gateway/README_PLUS_LOCAL.md` — local readiness + install-isolated-node steps
- `ops/mcp_gateway/OPERATE_PRIVATE_TUNNEL_V1.md` — tunnel activation
- `ops/mcp_gateway/install-isolated-node-v1.sh` (v22.23.4 pinned)
- `ops/mcp_gateway/stage-tunnel-service-v1.sh` {preflight|stage|status|rollback}
- `ops/mcp_gateway/configure-private-tunnel-v1.sh` (interactive, key via env not argv)
- GitHub Actions: `abutron-dcmcp-local-smoke-v1.yml`, `abutron-mcp-service-stage-v1.yml`, `abutron-official-tunnel-install-v1.yml`
- Verified runs: 42482747221, 42483971911, 42483933700, 42485169933, 42485578869

---
Prepared in Arena for ABUTRON-FX-V9 — does not modify production host. For activation, run the operator steps above on the real Ubuntu `localhost`.
