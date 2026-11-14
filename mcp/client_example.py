#!/usr/bin/env python3
"""
ABUTRON Own Gateway — Python client example (no OpenAI)
Connects to Remote Own Gateway via HTTP/SSE + JSON-RPC
Works against: Arena demo (http://127.0.0.1:3467) or Ubuntu localhost production (https://your-host/mcp)
"""
import os, json, requests

GATEWAY = os.environ.get("ABUTRON_GATEWAY_URL", "http://127.0.0.1:3467")
API_KEY = os.environ.get("ABUTRON_MCP_API_KEY", "abutron-demo-key-2024")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

def call(method, params=None, _id=1):
    body = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    # ABUTRON policy: gateway blocks sudo/rm -rf / etc. — this mimics legacy blockedCommands
    r = requests.post(f"{GATEWAY}/mcp/messages", headers=HEADERS, json=body, timeout=8)
    print(f"→ {method} [{r.status_code}]")
    print(json.dumps(r.json(), indent=2))
    return r.json()

if __name__ == "__main__":
    print(f"=== ABUTRON Own Gateway Client → {GATEWAY} ===")
    # Health (no auth)
    print(requests.get(f"{GATEWAY}/healthz", timeout=5).json())
    print(requests.get(f"{GATEWAY}/readyz", timeout=5).json())

    # MCP lifecycle (same as Desktop Commander https://desktopcommander.app/mcp/#installation)
    call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "abutron-python", "version": "1.0"}}, _id=1)
    call("tools/list", {}, _id=2)
    # Example: safe read (allowed dir from config)
    # call("tools/call", {"name": "list_directory", "arguments": {"path": "/home/user/ABUTRON-FX-V9"}}, _id=3)
    # Example: blocked command (should return BLOCKED_BY_ABUTRON_POLICY)
    # call("tools/call", {"name": "execute_command", "arguments": {"command": "sudo rm -rf /"}}, _id=99)

    print("\n✓ Connected via Remote Own Gateway — no OpenAI tunnel. Use same URL + API key on production host.")
