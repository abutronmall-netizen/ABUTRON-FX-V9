#!/usr/bin/env python3
"""
ABUTRON Arena Remote Bridge — Prod Handler V1
Runs ONLY allowlisted MCP tools. Arena -> Issue -> Prod -> Comment -> Arena

Allowlisted (read-only, LIVE=OFF, no deploy):
  read_file, list_directory, get_file_info, get_config
Allowed dirs: /opt/abutron-fx*, /opt/abutron-dcmcp, /var/lib/abutron-mcp, /home/abutron*, /tmp/abutron-mcp-smoke, + demo: repo itself
Blocked: sudo, rm -rf /, abutron-gh-apex-source-stage, crm-toggle, etc.
"""
import os, json, re, sys, stat
from pathlib import Path
import html

TITLE = os.environ.get("ISSUE_TITLE", "")
BODY = os.environ.get("ISSUE_BODY", "")
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER", "?")

# Demo repo root (FX-V9). On prod this would be /opt/abutron-fx-v9 or /opt/abutron-fx
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_DIRS = [
    Path("/opt/abutron-fx"),
    Path("/opt/abutron-fx-v9"),
    Path("/opt/abutron-dcmcp"),
    Path("/var/lib/abutron-mcp"),
    Path("/tmp/abutron-mcp-smoke"),
    Path("/home/abutron"),
    REPO_ROOT,  # demo: allow reading this repo itself
]
BLOCKED_PATTERNS = [
    r"\bsudo\b", r"rm\s+-rf\s+/", r"abutron-gh-apex-source-stage", r"abutron-gh-apex-source-rollback",
    r"crm-toggle", r"mt5-install-ex5-live", r"mkfs", r"\bdd\s+if="
]
ALLOWED_TOOLS = {"read_file", "list_directory", "get_file_info", "get_config"}

def is_allowed_path(p: Path) -> bool:
    try:
        rp = p.resolve()
    except: return False
    if rp.is_symlink(): return False  # refuse symlink at target (like abutron-ops stage checks)
    for ad in ALLOWED_DIRS:
        try:
            if rp.is_relative_to(ad.resolve()):
                return True
        except: pass
        # also allow exact match of REPO_ROOT demo
        if ad == REPO_ROOT and (rp == ad.resolve() or REPO_ROOT.resolve() in rp.parents):
            return True
    return False

def parse_request():
    # Title: "ABUTRON ARENA REMOTE <tool> <path>"
    # Body: optional JSON for args, or plain path repeat
    # Examples:
    #  Title: ABUTRON ARENA REMOTE list_directory /mcp/config
    #  Title: ABUTRON ARENA REMOTE read_file /mcp/config/abutron-mcp.config.json
    #  Body: {"path": "/mcp/config", "path": "..."} (optional)
    m = re.match(r"ABUTRON ARENA REMOTE\s+(\w+)(?:\s+(.*))?", TITLE.strip())
    if not m:
        return None, f"TITLE_PARSE_FAILED: expected 'ABUTRON ARENA REMOTE <tool> <path>', got: {TITLE!r}"
    tool, raw_path = m.group(1), (m.group(2) or "").strip()
    # Also allow JSON body to override
    body_tool = None
    body_path = None
    if BODY:
        try:
            jb = json.loads(BODY)
            if isinstance(jb, dict):
                body_tool = jb.get("tool")
                body_path = jb.get("path") or jb.get("args", {}).get("path")
        except:
            pass  # body is freeform text, ignore
    tool = body_tool or tool
    raw_path = body_path or raw_path
    return (tool, raw_path), None

def sanitize_body_for_blocked(s: str):
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, s, re.I):
            return False, f"BLOCKED_BY_ABUTRON_POLICY: matched {pat!r}"
    return True, None

def demo_resolve(path_str: str) -> Path:
    p = Path(path_str)
    # Demo helper: /mcp/*, /docs/*, /ops/* should map to REPO_ROOT, not literal /mcp
    if p.is_absolute():
        # If absolute but not under any ALLOWED_DIRS, try REPO_ROOT relative fallback (arena demo)
        if not is_allowed_path(p):
            candidate = REPO_ROOT / path_str.lstrip("/")
            if is_allowed_path(candidate):
                return candidate
        return p
    else:
        return REPO_ROOT / path_str.lstrip("/")

def do_list_directory(path_str: str):
    p = demo_resolve(path_str)
    if not is_allowed_path(p):
        return {"error": f"PATH_NOT_ALLOWED: {p} not in allowlist { [str(a) for a in ALLOWED_DIRS] }"}
    if p.is_symlink():
        return {"error": "REFUSE_SYMLINK_AT_TARGET"}
    if not p.exists():
        return {"error": f"NOT_FOUND: {p}"}
    if not p.is_dir():
        return {"error": f"NOT_A_DIRECTORY: {p}"}
    # Check ownership/permissions (prod expects root-owned, but demo allows user)
    try:
        st = p.lstat()
    except Exception as e:
        return {"error": f"LSTAT_FAILED: {e}"}
    items = []
    for child in sorted(p.iterdir()):
        try:
            cs = child.lstat()
            items.append({
                "name": child.name,
                "type": "dir" if stat.S_ISDIR(cs.st_mode) else "file" if stat.S_ISREG(cs.st_mode) else "other",
                "size": cs.st_size if stat.S_ISREG(cs.st_mode) else None,
                "symlink": child.is_symlink(),
            })
        except Exception as e:
            items.append({"name": child.name, "error": str(e)})
    return {"path": str(p), "resolved": str(p.resolve()), "type": "directory", "count": len(items), "items": items}

def do_read_file(path_str: str):
    p = demo_resolve(path_str)
    if not is_allowed_path(p):
        return {"error": f"PATH_NOT_ALLOWED: {p}"}
    if p.is_symlink():
        return {"error": "REFUSE_SYMLINK_AT_TARGET"}
    if not p.exists():
        return {"error": f"NOT_FOUND: {p}"}
    if not p.is_file():
        return {"error": f"NOT_A_FILE: {p}"}
    if p.stat().st_size > 1_000_000:
        return {"error": f"FILE_TOO_LARGE: {p.stat().st_size} > 1MB cap (abutron limit)"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"READ_FAILED: {e}"}
    # Truncate for comment size
    truncated = False
    if len(text) > 8000:
        text = text[:8000] + "\n...[TRUNCATED 8k limit]..."
        truncated = True
    return {"path": str(p), "resolved": str(p.resolve()), "type": "file", "size": p.stat().st_size, "content": text, "truncated": truncated}

def do_get_file_info(path_str: str):
    p = demo_resolve(path_str)
    if not is_allowed_path(p):
        return {"error": f"PATH_NOT_ALLOWED: {p}"}
    try:
        s = p.lstat()
        return {
            "path": str(p), "resolved": str(p.resolve()),
            "exists": p.exists(), "is_symlink": p.is_symlink(),
            "type": "dir" if stat.S_ISDIR(s.st_mode) else "file" if stat.S_ISREG(s.st_mode) else "other",
            "size": s.st_size, "mode": oct(s.st_mode), "uid": s.st_uid, "gid": s.st_gid
        }
    except Exception as e:
        return {"error": str(e)}

def do_get_config(_path_str: str):
    # Return sanitized bridge config (no secrets)
    return {
        "gateway": "abutron-own-v1",
        "allowedTools": sorted(ALLOWED_TOOLS),
        "allowedDirs": [str(a) for a in ALLOWED_DIRS],
        "blockedPatterns": BLOCKED_PATTERNS,
        "live": "ALWAYS_OFF",
        "note": "Arena -> Issue -> Prod -> Comment bridge. No sudo, no deploy, no LIVE."
    }

TOOL_DISPATCH = {
    "list_directory": do_list_directory,
    "read_file": do_read_file,
    "get_file_info": do_get_file_info,
    "get_config": do_get_config,
}

def main():
    (parsed, err) = parse_request()
    result = {}
    meta = {"title": TITLE, "issue": ISSUE_NUMBER, "repo": "abutronmall-netizen/ABUTRON-FX-V9", "bridge": "abutron-arena-remote-v1", "live": "ALWAYS_OFF"}

    if err:
        result = {"error": err, "allowedTools": sorted(ALLOWED_TOOLS)}
        meta["status"] = "TITLE_PARSE_FAILED"
    else:
        tool, path_str = parsed
        ok, block_msg = sanitize_body_for_blocked(f"{tool} {path_str} {BODY}")
        if not ok:
            result = {"error": block_msg}
            meta["status"] = "BLOCKED_BY_ABUTRON_POLICY"
        elif tool not in ALLOWED_TOOLS:
            result = {"error": f"TOOL_NOT_ALLOWLISTED: {tool} not in {sorted(ALLOWED_TOOLS)}", "hint": "Use: read_file, list_directory, get_file_info, get_config"}
            meta["status"] = "TOOL_NOT_ALLOWED"
        else:
            try:
                result = TOOL_DISPATCH[tool](path_str)
                meta["status"] = "PASS" if "error" not in result else "TOOL_ERROR"
                meta["tool"] = tool
                meta["path"] = path_str
            except Exception as e:
                result = {"error": f"HANDLER_EXCEPTION: {e}"}
                meta["status"] = "EXCEPTION"

    # Build outputs
    payload = {"meta": meta, "result": result}
    # Write JSON for gh comment artifact
    out_json = Path("/tmp/abutron-bridge-result.json")
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Write Markdown comment (sanitized, no raw secrets, html-escaped)
    comment = f"""### ABUTRON Arena Bridge Result — `{meta.get('status')}`

**Request:** `{html.escape(TITLE)}` (#{ISSUE_NUMBER})
**Tool:** `{html.escape(meta.get('tool',''))}` → `{html.escape(meta.get('path',''))}`
**Bridge:** `abutron-arena-remote-v1` — `LIVE=ALWAYS_OFF` — allowlist only

```json
{json.dumps(payload, indent=2)[:7000]}
```

*No LIVE deploy, no sudo, no CRM/MT5 write. Allowlisted tools only: `read_file, list_directory, get_file_info, get_config` under `/opt/abutron-fx*`, `/opt/abutron-dcmcp`, `REPO_ROOT`.*
"""
    # Guard comment size (GitHub limit ~65536)
    if len(comment) > 60000:
        comment = comment[:60000] + "\n...[COMMENT_TRUNCATED]..."
    Path("/tmp/abutron-bridge-comment.md").write_text(comment, encoding="utf-8")

    # Also stdout for Actions log
    print(json.dumps(payload, indent=2))
    if meta.get("status") != "PASS":
        # Don't fail job for TOOL_ERROR (so Arena gets comment), but fail for parse/block
        if meta["status"] in ("TITLE_PARSE_FAILED", "BLOCKED_BY_ABUTRON_POLICY"):
            sys.exit(2)
    # success → Action will post comment

if __name__ == "__main__":
    main()
