# Remote Own Gateway — LIVE (Arena Demo)

**Arena demo is running right now — proof of your own version without OpenAI:**

- **Gateway:** ABUTRON Own V1 @ `0.0.0.0:3467` (no OpenAI `tunnel-client`)
- **Core:** `@wonderwhy-er/desktop-commander` 0.3.3 via local npm (same as https://desktopcommander.app/mcp/#installation `npx @wonderwhy-er/desktop-commander setup`)
- **Live Preview:** Check Arena's "ABUTRON Own MCP Gateway" live preview panel → `http://127.0.0.1:3467/healthz` is proxied to `https://3467-xxx.e2b.app`
- **Auth:** `Authorization: Bearer abutron-demo-key-2024` (prod uses `/etc/abutron-mcp/gateway.env` 0600)

## Verified right now

```
GET /healthz → {"status":"ok","gateway":"abutron-own-v1","upstream":"desktop-commander","node":"v22.22.3"}
GET /mcp/tools (with Bearer) → {"gateway":"abutron-own-v1","mcpInit":"PASS","mcpToolsList":"PASS","mcpToolsCount":25,"noToolInvoked":true}
POST /mcp/messages {initialize} → {result:{protocolVersion:"2025-03-26", serverInfo:{name:"desktop-commander",version:"0.3.3"}}}
POST /mcp/messages {tools/list} → 25 tools (get_config, read_file, write_file, list_directory, start_process, etc.)
POST /mcp/messages {tools/call: sudo rm -rf /} → {"error":"BLOCKED_BY_ABUTRON_POLICY","blocked":"sudo"} ✅
```

Matches `abutron-ops` legacy verified runs 42483971911: 25 tools, stdio PASS, but **without** OpenAI Plus gate.

## How to connect YOUR server (Ubuntu localhost)

1. On **production Ubuntu root terminal** (where `abutron-ops` lives):
```bash
cd /root/ABUTRON-FX-V9  # or clone this repo
git fetch origin arena/873cd835-abutron-fx-v9
git checkout arena/873cd835-abutron-fx-v9
sudo bash mcp/scripts/install-abutron-mcp-own-v1.sh --gateway
sudo systemctl enable --now abutron-mcp-gateway.service
systemctl is-active abutron-mcp-gateway.service  # active
cat /etc/abutron-mcp/gateway.env  # ABUTRON_MCP_API_KEY=... (copy for remote clients)
curl -H "Authorization: Bearer $ABUTRON_MCP_API_KEY" http://127.0.0.1:3467/healthz
```

2. Expose **privately** (gateway binds 127.0.0.1 only by default — legacy tunnel was also outbound-only):
```bash
# Option A: Tailscale (recommended — no port forward)
sudo tailscale serve https --bg 3467
# → https://your-host.tail-scale.ts.net/mcp/sse

# Option B: Caddy + Let's Encrypt
caddy reverse-proxy --from your-domain.com --to 127.0.0.1:3467 --header "Authorization Bearer $KEY"

# Option C: WireGuard / SSH tunnel
ssh -L 3467:127.0.0.1:3467 abutron@prod-host
```

3. Connect from **any MCP client** (no OpenAI):

**Python (see mcp/client_example.py):**
```bash
ABUTRON_GATEWAY_URL=https://your-host/mcp ABUTRON_MCP_API_KEY=... python3 mcp/client_example.py
```

**Node/Claude/Cursor web via SSE:**
- URL: `https://your-host/mcp/sse`
- Header: `Authorization: Bearer YOUR_ABUTRON_KEY`
- Body: JSON-RPC `{jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2025-03-26",capabilities:{},clientInfo:{name:"abutron"}}}`

**Claude Desktop local (no network):**
```json
{ "mcpServers": { "abutron-own": { "command": "/opt/abutron-mcp/node/bin/node", "args": ["/opt/abutron-mcp/desktop-commander/dist/index.js","--no-onboarding"] } } }
```

## Deploy work via gateway? Same rule

Gateway ≠ deploy. Even with own gateway active, **LIVE trading deploy still locked** until root installs `abutron-gh-apex-source-stage-v1` + exact sudoers + hash-pin + offsite backup proof. See `docs/ABUTRON_OWN_GATEWAY_ARCHITECTURE.md` § Security.

