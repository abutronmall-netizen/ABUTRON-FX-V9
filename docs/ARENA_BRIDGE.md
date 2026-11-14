# Arena ↔ Prod Bridge — ABUTRON ARENA REMOTE V1

**You asked for:**
```
Arena --(api.github.com)--> GitHub Issue ("ABUTRON ARENA REMOTE read_file /opt/abutron-fx/manifest.json")
Prod Runner (poll GitHub) --(outbound)--> runs ONLY allowlisted MCP tools
Prod --(api.github.com)--> posts sanitized JSON as Issue Comment
Arena --(api.github.com)--> reads comment
```

This is **implemented in this branch** (`arena/873cd835-abutron-fx-v9`). No OpenAI, no tunnel needed for the bridge — GitHub API is the allowed relay.

## Files in this branch

| Path | Purpose |
|---|---|
| `.github/workflows/abutron-arena-remote-bridge-v1.yml` | **Demo** for `ABUTRON-FX-V9` (runs on `ubuntu-latest`, anyone can test now) — triggers on `ABUTRON ARENA REMOTE*` issues by `abutronmall-netizen` only |
| `mcp/bridge/prod_handler.py` | Allowlist engine: `read_file`, `list_directory`, `get_file_info`, `get_config` under `REPO_ROOT`, `/opt/abutron-fx*`, `/var/lib/abutron-mcp` only; blocks `sudo`, `rm -rf /`, `abutron-gh-apex-source-stage*`, `crm-toggle`, `mt5-install-ex5-live`; `LIVE=ALWAYS_OFF` |
| `mcp/bridge/arena_client.py` | **Arena side** CLI: `python3 mcp/bridge/arena_client.py <tool> <path>` → creates issue via `gh api` + polls comment |
| `ops/bridge/abutron-ops-arena-remote-v1.yml` | **Prod** version for `abutron-ops` (runs on `self-hosted, linux, x64, abutron-diagnostics` as `abutrongha`) — copy to `abutron-ops/.github/workflows/` via reviewed PR |
| `docs/ARENA_BRIDGE.md` | This doc |

## How it works (security first, like `abutron-ops` invariants)

1. **Arena** (this sandbox, `api.github.com` is allowlisted) creates issue:
   ```
   Title: ABUTRON ARENA REMOTE list_directory /mcp/config
   Body:  {"tool":"list_directory","path":"/mcp/config","from":"arena-client"}
   Repo:  abutronmall-netizen/ABUTRON-FX-V9 (demo) or abutron-ops (prod)
   ```

2. **Workflow fires** `if: startsWith(title, 'ABUTRON ARENA REMOTE') && actor == abutronmall-netizen && repo == expected` — no PR code runs on prod, `permissions: {issues:write, contents:read}`, `concurrency: single`, `timeout-minutes: 3` (same pattern as `abutron-ops/abutron-reviewed-operations-v1.yml`)

3. **Prod handler** (`mcp/bridge/prod_handler.py` logic):
   * Parses `tool` + `path` from title/body JSON
   * Blocks `BLOCKED_PATTERNS` (sudo, rm -rf /, stage/rollback, CRM toggle)
   * Enforces `ALLOWED_TOOLS = {read_file, list_directory, get_file_info, get_config}` — any other tool → `TOOL_NOT_ALLOWLISTED`
   * Enforces `ALLOWED_DIRS` with `Path.resolve()` + symlink refusal (same as `stage-tunnel-service-v1.sh` checks)
   * Dispatches to Python impl (not `execute_command`) — no shell, no deploy, no LIVE
   * Writes `/tmp/abutron-bridge-result.json` + `/tmp/abutron-bridge-comment.md` (sanitized, html-escaped, 7k JSON cap)

4. **Prod posts comment** via `gh issue comment $ISSUE_NUMBER --body-file /tmp/abutron-bridge-comment.md` — JSON inside ```json block includes `meta.status` (`PASS`, `TOOL_ERROR`, `BLOCKED_BY_ABUTRON_POLICY`, `TITLE_PARSE_FAILED`) + `result`

5. **Arena polls** `GET /repos/{repo}/issues/{num}/comments` until `ABUTRON Arena Bridge Result` appears (up to 75s), parses JSON, returns exit code `0` for `PASS`.

## Try it now (demo repo, no prod needed)

In this Arena sandbox (already authenticated `gh`):

```bash
# List this repo's bridge dir via demo workflow (ubuntu-latest runner, not self-hosted prod)
python3 mcp/bridge/arena_client.py list_directory /mcp/bridge

# Read a file
python3 mcp/bridge/arena_client.py read_file /mcp/config/abutron-mcp.config.json

# Get config (allowlist)
python3 mcp/bridge/arena_client.py get_config ""

# Blocked demo (should return BLOCKED_BY_ABUTRON_POLICY)
python3 mcp/bridge/arena_client.py read_file "sudo rm -rf /"  # or via title trick — blocked
```

Each creates `https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/<N>` → workflow runs → comment appears → client prints `{"meta":{"status":"PASS"}, "result":{...}}`

## Upgrade to prod (`abutron-ops` self-hosted runner)

1. On prod Ubuntu, copy our ready file:
   ```bash
   cd /root/abutron-ops
   git fetch origin
   git checkout -b feat/abutron-arena-remote-bridge
   cp /root/ABUTRON-FX-V9/ops/bridge/abutron-ops-arena-remote-v1.yml .github/workflows/abutron-arena-remote-bridge-v1.yml
   git add .github/workflows/abutron-arena-remote-bridge-v1.yml
   git commit -m "feat: ABUTRON Arena Remote Bridge V1 — allowlisted read-only (self-hosted)"
   gh pr create --title "ABUTRON Arena Remote Bridge V1" --body "Allowlisted read-only bridge for Arena via api.github.com"
   # Wait for CI offline checks (ops-offline-contract-tests) → merge by abutronmall-netizen
   ```

2. Then Arena can target prod:
   ```bash
   # Edit mcp/bridge/arena_client.py REPO = "abutronmall-netizen/abutron-ops"
   # Or pass --repo flag (add it)
   python3 mcp/bridge/arena_client.py list_directory /opt/abutron-fx  # on prod allowedDirs
   ```

No secrets in issues — `gateway.env`/`tunnel.env` never posted, LIVE remains OFF, deploy stays locked behind `abutron-gh-apex-source-stage-v1` + hash-pin + backup proof.

## Relation to your own Remote Own Gateway

* **Own Gateway** (`mcp/server/src/gateway.ts` on `127.0.0.1:3467`) is for **you** (laptop/Tailscale) → prod direct SSE, no GitHub.
* **Arena Bridge** is for **Arena** → prod via GitHub API (the only egress Arena has). Both share same `allowedTools`/`allowedDirs`/`blockedCommands` policy — gateway never adds deploy power.

