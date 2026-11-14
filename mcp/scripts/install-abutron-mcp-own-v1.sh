#!/usr/bin/env bash
# ABUTRON Own MCP Install V1 — No OpenAI
# Based on https://desktopcommander.app/mcp/#installation but ABUTRON-owned.
# Two modes: --local (stdio for Claude Desktop/Cursor) and --gateway (own HTTP/SSE gateway)
# Reference: abutron-ops install-isolated-node-v1.sh pinned v22.23.4 SHA256 1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
set -Eeuo pipefail
umask 077

MODE="${1:-}"
if [[ "$MODE" != "--local" && "$MODE" != "--gateway" ]]; then
  echo "Usage: sudo bash mcp/scripts/install-abutron-mcp-own-v1.sh --local | --gateway" >&2
  echo "  --local   : isolated Node + Desktop Commander stdio only (for Claude Desktop/Cursor)" >&2
  echo "  --gateway : above + own HTTP/SSE gateway systemd (no OpenAI tunnel-client)" >&2
  exit 2
fi

if [[ "$(id -u)" != "0" ]]; then
  echo "ROOT_REQUIRED: run with sudo from Ubuntu terminal (mirrors abutron-ops install-isolated-node-v1.sh)" >&2
  exit 2
fi
if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "UNSUPPORTED_OS_ARCH: need Linux x86_64 (Debian 12 tested)" >&2
  exit 2
fi

VERSION=v22.23.4
FILENAME=node-v22.23.4-linux-x64.tar.xz
SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
URL="https://nodejs.org/dist/${VERSION}/${FILENAME}"
ROOT=/opt/abutron-mcp
DEST="${ROOT}/node"
DCM_ROOT=/opt/abutron-mcp/desktop-commander
GATEWAY_PKG="/root/abutron-fx-v9-or-wherever/ABUTRON-FX-V9/mcp/server"

echo "=== ABUTRON Own MCP Install V1 — Mode $MODE — No OpenAI ==="
echo "    Source: https://desktopcommander.app/mcp/#installation (wonderwhy-er/DesktopCommanderMCP)"
echo "    Legacy OpenAI path was: /usr/local/bin/tunnel-client v0.0.20 + CONTROL_PLANE_API_KEY (Plus-blocked)"
echo "    Own path is: Fastify SSE gateway on 127.0.0.1:3467 + ABUTRON_MCP_API_KEY (0600)"

for bin in curl tar sha256sum mktemp; do command -v "$bin" >/dev/null || { echo "MISSING_CMD=$bin" >&2; exit 1; }; done
if [[ -L "$ROOT" || -L "$DEST" ]]; then echo "REFUSE_SYMLINK_AT_DEST" >&2; exit 1; fi

# 1) Isolated Node (same pin as abutron-ops)
if [[ -e "$DEST" ]]; then
  if [[ -f "$DEST/bin/node" && ! -L "$DEST/bin/node" && "$("$DEST/bin/node" --version)" == "$VERSION" ]]; then
    echo "ISOLATED_NODE_ALREADY_INSTALLED=$VERSION"
    chmod 0755 "$DEST/bin" "$DEST/bin/node" || true
  else
    echo "REFUSE_OVERWRITE_EXISTING_NODE: $DEST exists with different version — manual review" >&2
    exit 1
  fi
else
  if [[ -e "$ROOT" ]]; then
    [[ -d "$ROOT" && ! -L "$ROOT" ]] || { echo "ROOT_PATH_NOT_DIRECTORY" >&2; exit 1; }
    [[ "$(stat -c '%u' "$ROOT")" == "0" ]] || { echo "ROOT_NOT_OWNED" >&2; exit 1; }
  else
    install -d -o root -g root -m 0755 "$ROOT"
  fi
  TMPD=$(mktemp -d "${ROOT}/.node-download.XXXXXXXX")
  trap 'rm -rf -- "$TMPD"' EXIT
  echo "DOWNLOADING_OFFICIAL_NODE=$VERSION"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --connect-timeout 15 --max-time 180 --output "${TMPD}/${FILENAME}" "$URL"
  printf '%s  %s\n' "$SHA256" "${TMPD}/${FILENAME}" | sha256sum --check --status || { echo "NODE_SHA256_MISMATCH" >&2; exit 1; }
  echo "OFFICIAL_NODE_SHA256_VERIFIED"
  install -d -o root -g root -m 0755 "${TMPD}/runtime"
  tar -xJf "${TMPD}/${FILENAME}" -C "${TMPD}/runtime" --strip-components=1 --no-same-owner --no-same-permissions
  [[ "$("${TMPD}/runtime/bin/node" --version)" == "$VERSION" ]] || { echo "NODE_VERSION_MISMATCH" >&2; exit 1; }
  chown -R root:root "${TMPD}/runtime"; chmod -R go-w "${TMPD}/runtime"; chmod -R go+rX "${TMPD}/runtime"
  mv -T -- "${TMPD}/runtime" "$DEST"
  echo "ISOLATED_NODE_INSTALL_PASS=$("$DEST/bin/node" --version)"
  trap - EXIT
  rm -rf -- "$TMPD"
fi
echo "GLOBAL_NODE_LEFT_UNCHANGED=$(/usr/bin/node --version 2>/dev/null || echo unavailable)"

# 2) Desktop Commander core (local stdio) — same package as abutron-ops but via isolated npm
if [[ ! -f "${DCM_ROOT}/dist/index.js" || -L "${DCM_ROOT}/dist/index.js" ]]; then
  echo "INSTALLING_DESKTOP_COMMANDER_CORE=v0.3.1+ via isolated npm"
  install -d -o root -g root -m 0755 "$DCM_ROOT" 2>/dev/null || true
  # Use isolated node npm
  "$DEST/bin/npm" --prefix "$DCM_ROOT" init -y >/dev/null 2>&1 || true
  "$DEST/bin/npm" --prefix "$DCM_ROOT" install @wonderwhy-er/desktop-commander@0.3.1 --save >/dev/null
  # Harden: root-owned, no symlinks
  chown -R root:root "$DCM_ROOT"; chmod -R go-w "$DCM_ROOT"
  echo "DESKTOP_COMMANDER_INSTALL_PASS=$(ls -l "$DCM_ROOT/dist/index.js" | awk '{print $9}')"
else
  echo "DESKTOP_COMMANDER_ALREADY_INSTALLED=$DCM_ROOT/dist/index.js"
fi

# 3) Smoke test (mirrors abutron-ops abutron-dcmcp-local-smoke-v1.yml)
echo "SMOKE_TEST: MCP initialize + tools/list (no tool invocation)..."
python3 - <<'PY'
import json, os, subprocess, sys, tempfile, stat
from pathlib import Path
node=Path("/opt/abutron-mcp/node/bin/node")
server=Path("/opt/abutron-mcp/desktop-commander/dist/index.js")
for name,p in (("node",node),("dcm",server)):
    s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and not p.is_symlink() and s.st_uid==0, f"UNTRUSTED_{name}"
a={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"abutron-own-smoke","version":"1.0"}}}
b={"jsonrpc":"2.0","method":"notifications/initialized"}
c={"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
payload="\n".join(json.dumps(x,separators=(",",":")) for x in (a,b,c))+"\n"
with tempfile.TemporaryDirectory(prefix="abutron-mcp-smoke-") as home:
    env=dict(os.environ); env.update({"HOME":home,"XDG_CONFIG_HOME":home,"XDG_CACHE_HOME":home,"PATH":f"{node.parent}:/usr/bin:/bin"})
    p=subprocess.run([str(node),str(server),"--no-onboarding"], input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=12, env=env)
    decoded={}
    for line in p.stdout.splitlines():
        try:
            v=json.loads(line)
            if isinstance(v.get("id"),int) and v.get("id") in (1,2): decoded[v.get("id")]=v
        except: pass
    assert isinstance(decoded.get(1,{}).get("result"),dict) and isinstance(decoded[1]["result"].get("protocolVersion"),str), "MCP_INIT_FAILED"
    print("MCP_INIT=PASS")
    tools=decoded.get(2,{}).get("result")
    assert isinstance(tools,dict) and isinstance(tools.get("tools"),list), "MCP_TOOLS_LIST_FAILED"
    print(f"MCP_TOOLS_LIST=PASS MCP_TOOLS_COUNT={len(tools['tools'])}")
    assert 1 <= len(tools["tools"]) <= 100
    print("NO_TOOL_INVOKED=YES CHATGPT_PLUS_BLOCK_NOT_APPLICABLE=OWN_GATEWAY")
PY

if [[ "$MODE" == "--local" ]]; then
  echo "OWN_LOCAL_INSTALL_DONE — use Claude Desktop config:"
  echo '  { "mcpServers": { "abutron-own": { "command": "/opt/abutron-mcp/node/bin/node", "args": ["/opt/abutron-mcp/desktop-commander/dist/index.js", "--no-onboarding"] } } }'
  echo "No OpenAI tunnel needed. See mcp/config/abutron-mcp.config.json for allowedDirectories."
  exit 0
fi

# 4) Own gateway (HTTP/SSE) — replaces tunnel-client
echo "STAGING_OWN_GATEWAY_SYSTEMD (no OpenAI tunnel-client)..."
PKG_SRC="$(cd "$(dirname "$0")/../.." && pwd)/mcp/server"
if [[ ! -f "$PKG_SRC/src/gateway.ts" ]]; then
  echo "GATEWAY_SRC_NOT_FOUND=$PKG_SRC" >&2; exit 2
fi
# Install gateway deps with isolated node
"$DEST/bin/npm" --prefix "$PKG_SRC" install --production >/dev/null 2>&1 || "$DEST/bin/npm" --prefix "$PKG_SRC" install >/dev/null

# Service account (same as legacy: abutronmcp but own gateway)
if ! getent passwd abutronmcp >/dev/null; then
  /usr/sbin/useradd --system --user-group --home-dir /var/lib/abutron-mcp --shell /usr/sbin/nologin --no-create-home abutronmcp
fi
install -d -o abutronmcp -g abutronmcp -m 0700 /var/lib/abutron-mcp
install -d -o root -g root -m 0700 /etc/abutron-mcp
install -d -o root -g root -m 0755 /usr/local/libexec

# Own API key (not OpenAI CONTROL_PLANE_API_KEY)
ENVFILE=/etc/abutron-mcp/gateway.env
if [[ ! -f "$ENVFILE" ]]; then
  KEY=$(openssl rand -hex 32)
  printf 'ABUTRON_MCP_API_KEY=%s\n' "$KEY" > "$ENVFILE"
  chown root:root "$ENVFILE"; chmod 0600 "$ENVFILE"
  echo "OWN_API_KEY_GENERATED_SAVED_TO=$ENVFILE (0600) — copy it to your MCP client Bearer header"
  echo "YOUR_OWN_KEY=$KEY"
else
  echo "OWN_API_KEY_ALREADY_EXISTS=$ENVFILE — not overwriting"
fi

# Wrapper for gateway
WRAPPER=/usr/local/libexec/abutron-mcp-gateway-v1
cat > "$WRAPPER" <<'WRAP'
#!/bin/sh
exec /opt/abutron-mcp/node/bin/node /opt/abutron-mcp/gateway/dist/gateway.js
WRAP
# Prefer TS gateway via tsx if dist not built
if [[ -f "$PKG_SRC/dist/gateway.js" ]]; then
  echo "GATEWAY_DIST_EXISTS"
else
  # Use tsx runner for now
  cat > "$WRAPPER" <<WRAP2
#!/bin/sh
exec /opt/abutron-mcp/node/bin/node --import tsx/esm $PKG_SRC/src/gateway.ts
WRAP2
fi
chown root:root "$WRAPPER"; chmod 0755 "$WRAPPER"

# Systemd unit (own, not abutron-mcp-tunnel.service)
UNIT=/etc/systemd/system/abutron-mcp-gateway.service
if [[ -e "$UNIT" ]]; then echo "REFUSE_OVERWRITE_EXISTING_UNIT=$UNIT" >&2; exit 2; fi
cat > "$UNIT" <<SERVICE
[Unit]
Description=ABUTRON Own MCP Gateway V1 (No OpenAI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=abutronmcp
Group=abutronmcp
WorkingDirectory=/var/lib/abutron-mcp
Environment=HOME=/var/lib/abutron-mcp
Environment=XDG_CONFIG_HOME=/var/lib/abutron-mcp/.config
EnvironmentFile=/etc/abutron-mcp/gateway.env
ExecStart=${WRAPPER}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/abutron-mcp
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
SERVICE
chown root:root "$UNIT"; chmod 0644 "$UNIT"
systemctl daemon-reload
# Copy gateway src to fixed location for systemd
install -d -o root -g root -m 0755 /opt/abutron-mcp/gateway
cp -a "$PKG_SRC/src" "$PKG_SRC/package.json" /opt/abutron-mcp/gateway/ 2>/dev/null || true
if [[ -d "$PKG_SRC/dist" ]]; then cp -a "$PKG_SRC/dist" /opt/abutron-mcp/gateway/; fi
chown -R root:root /opt/abutron-mcp/gateway; chmod -R go-w /opt/abutron-mcp/gateway

echo "OWN_GATEWAY_STAGED_NOT_YET_ENABLED — enable with:"
echo "  systemctl enable --now abutron-mcp-gateway.service"
echo "  systemctl is-active abutron-mcp-gateway.service"
echo "  curl -H \"Authorization: Bearer \$(cat /etc/abutron-mcp/gateway.env | cut -d= -f2)\" http://127.0.0.1:3467/healthz"
echo "NO_OPENAI_TUNNEL_STARTED — legacy abutron-mcp-tunnel.service remains inactive/unrelated"
