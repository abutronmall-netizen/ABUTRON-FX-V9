#!/usr/bin/env python3
"""
ABUTRON Arena Client — Arena --(api.github.com)--> GitHub Issue
Creates "ABUTRON ARENA REMOTE <tool> <path>" issue, waits for prod comment, reads JSON.

Usage inside Arena (api.github.com allowed):
  python3 mcp/bridge/arena_client.py list_directory /mcp/config
  python3 mcp/bridge/arena_client.py read_file /mcp/config/abutron-mcp.config.json
  python3 mcp/bridge/arena_client.py get_file_info /mcp/bridge/prod_handler.py
  python3 mcp/bridge/arena_client.py get_config ""

Env: GH_TOKEN via `gh auth status` (Arena's GitHub connection). No extra secret.
"""
import os, sys, json, time, subprocess, shlex

REPO = "abutronmall-netizen/ABUTRON-FX-V9"
POLL_SECS = 75
POLL_INTERVAL = 5

def gh(*args, capture=True):
    cmd = ["gh", "api"] + list(args)
    # gh api respects GH_TOKEN / gh auth
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode != 0:
        print(f"gh api failed: {' '.join(shlex.quote(a) for a in cmd)}\n{r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout

def create_issue(tool, path):
    title = f"ABUTRON ARENA REMOTE {tool} {path}".strip()
    body = json.dumps({"tool": tool, "path": path, "from": "arena-client", "repo": REPO})
    # Use gh issue create (creates via api.github.com)
    cmd = ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", body]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"gh issue create failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)
    # gh issue create prints URL like https://github.com/.../issues/123
    url = r.stdout.strip()
    print(f"Created: {url}")
    # Extract number
    num = url.rstrip("/").split("/")[-1]
    try:
        return int(num)
    except:
        # fallback: list recent issues
        out = gh(f"repos/{REPO}/issues", "--jq", ".[0].number")
        return int(out.strip())

def wait_for_comment(issue_number, since_seconds_ago=80):
    print(f"Waiting for prod comment on #{issue_number} (up to {POLL_SECS}s)...")
    deadline = time.time() + POLL_SECS
    while time.time() < deadline:
        # Read comments via GraphQL-ish REST
        comments_json = gh(f"repos/{REPO}/issues/{issue_number}/comments", "--jq", ".")
        try:
            comments = json.loads(comments_json)
        except:
            comments = []
        for c in reversed(comments):  # newest first
            body = c.get("body","")
            if "ABUTRON Arena Bridge Result" in body and "abutron-arena-remote-v1" in body:
                print(f"\n✓ Prod replied (comment {c.get('id')}) at {c.get('created_at')}:")
                # Extract JSON block
                import re
                m = re.search(r"```json\n(.*?)\n```", body, re.S)
                if m:
                    payload = json.loads(m.group(1))
                    print(json.dumps(payload, indent=2))
                    return payload
                else:
                    print(body[:4000])
                    return {"raw": body}
        time.sleep(POLL_INTERVAL)
        print(".", end="", flush=True)
    print(f"\nTimeout: no bridge comment yet. Check https://github.com/{REPO}/issues/{issue_number}")
    sys.exit(2)

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <tool> <path>")
        print("  tools: list_directory, read_file, get_file_info, get_config")
        print("  e.g.: python3 mcp/bridge/arena_client.py list_directory /mcp/config")
        sys.exit(2)
    tool = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    # Validate allowlist client-side (mirrors prod)
    if tool not in {"list_directory","read_file","get_file_info","get_config"}:
        print(f"TOOL_NOT_ALLOWED: {tool}", file=sys.stderr); sys.exit(2)
    issue_number = create_issue(tool, path)
    payload = wait_for_comment(issue_number)
    # Exit code reflects prod status
    status = payload.get("meta",{}).get("status") if isinstance(payload, dict) else None
    if status == "PASS":
        sys.exit(0)
    elif payload.get("result",{}).get("error"):
        print(f"Prod returned error: {payload['result']['error']}", file=sys.stderr)
        sys.exit(1)
    else:
        sys.exit(0 if status=="PASS" else 1)

if __name__ == "__main__":
    main()
