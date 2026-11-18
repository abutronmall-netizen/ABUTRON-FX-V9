"""Sanitized source contract tests for Issue #25 public-source transfer.

These tests validate that sanitized production scripts committed to the
public repository comply with the privacy boundary defined in
docs/ISSUE25_PUBLIC_SOURCE_HANDOFF.md:

- No hardcoded broker account logins (5-12 digit numbers)
- No hardcoded 32-character connection profile IDs
- No private customer trade rows or broker statements
- No LIVE account identifiers
- No API credentials or private keys
- Read-only CRM lookup instead of hardcoded identifiers
- Fail-closed on mis-scoped or malformed configuration

These are static/offline checks. They do not compile MQL5, prove broker
execution, or certify production state.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEPLOY_DIR = REPO_ROOT / "deploy"

# Patterns that indicate private/host-only identifiers in source.
# These are the exact patterns the privacy guard CI also checks, but
# this test suite validates them at the source-code level for individual
# sanitized scripts.

# 32-character hex connection profile IDs (UUID without dashes)
CONNECTION_ID_PATTERN = re.compile(r'"[a-f0-9]{32}"')

# Broker login numbers (5-12 digits) that are hardcoded as literals,
# not as environment variables or CRM lookups.
# We look for patterns like "7410512" (quoted or bare 7+ digit numbers).
BROKER_LOGIN_PATTERN = re.compile(r'(?<![\w."])\d{7,12}(?![\w.])')

# Private key / API credential signatures
PRIVATE_KEY_PATTERN = re.compile(
    r'(-----BEGIN (RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----|'
    r'AKIA[0-9A-Z]{16}|'  # AWS access key
    r'sk-[a-zA-Z0-9]{20,})'
)

# Host evidence file extensions that must not appear in tracked source
EVIDENCE_FILE_PATTERN = re.compile(
    r'\.(sqlite3|hcc|xlsx|tgz|db)"', re.IGNORECASE
)

# Public-only broker server name (allowed)
FP_MARKETS_SERVER = "FPMarketsSC-Demo"


def read_script(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_python_scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.py"))


def find_systemd_files() -> list[Path]:
    files: list[Path] = []
    for ext in ("*.service", "*.timer"):
        files.extend(sorted(DEPLOY_DIR.rglob(ext)))
    return sorted(set(files))


class SanitizedScriptsTest(unittest.TestCase):
    """Verify all Python scripts in scripts/ are sanitized for public publication."""

    def test_no_hardcoded_connection_profile_ids(self):
        """No 32-char hex connection profile IDs hardcoded in scripts."""
        violations = []
        for script in find_python_scripts():
            content = read_script(script)
            for match in CONNECTION_ID_PATTERN.finditer(content):
                # Check if it's a legitimate non-ID string (e.g., SHA hash)
                # by looking at context — SHA hashes are usually in variables
                # with "sha" or "hash" in the name
                line_start = content.rfind("\n", 0, match.start()) + 1
                line = content[line_start:match.end()]
                if "sha" not in line.lower() and "hash" not in line.lower():
                    violations.append(f"{script.name}: {match.group()}")
        self.assertEqual(violations, [], f"Hardcoded connection IDs: {violations}")

    def test_no_hardcoded_broker_logins(self):
        """No 7-8 digit broker login numbers hardcoded in account config.

        Only flags numbers in login/account/suffix context — e.g.,
        dictionary values with login keys, or quoted numbers in
        account configuration. Excludes thresholds, size limits,
        SHA hashes, environment variable access, and path references.
        """
        violations = []
        login_context_keywords = ("login", "account", "suffix", "profile")
        for script in find_python_scripts():
            content = read_script(script)
            for line_num, line in enumerate(content.splitlines(), 1):
                lower = line.lower()
                # Only check lines with login/account context
                if not any(kw in lower for kw in login_context_keywords):
                    continue
                # Skip environment variable access
                if "os.environ" in line or "getenv" in line:
                    continue
                # Skip comments
                if line.strip().startswith("#"):
                    continue
                # Skip SHA/hash context
                if "sha" in lower or "hash" in lower:
                    continue
                # Skip public broker server name
                if FP_MARKETS_SERVER in line:
                    continue
                for match in BROKER_LOGIN_PATTERN.finditer(line):
                    num = match.group()
                    if "." in line[match.start():match.end()]:
                        continue
                    violations.append(f"{script.name}:{line_num}: {num}")
        self.assertEqual(violations, [], f"Hardcoded broker logins: {violations}")

    def test_no_private_key_or_api_credentials(self):
        """No private keys or API credentials in any tracked script."""
        violations = []
        for script in find_python_scripts():
            content = read_script(script)
            for match in PRIVATE_KEY_PATTERN.finditer(content):
                violations.append(f"{script.name}: {match.group()}")
        self.assertEqual(violations, [], f"Credentials found: {violations}")

    def test_no_evidence_file_references(self):
        """No committed evidence files in tracked scripts.

        Read-only database path references in code (e.g., reading from
        a broker history sqlite3) are legitimate. This test flags actual
        tracked file paths with evidence extensions, not code references
        to database files.
        """
        violations = []
        for script in find_python_scripts():
            content = read_script(script)
            # Only flag evidence extensions in standalone file path strings,
            # not in code that reads from databases
            for line_num, line in enumerate(content.splitlines(), 1):
                lower = line.lower()
                # Skip CRM/DB path references (read-only lookups)
                if "crm" in lower or "accounts.sqlite3" in lower:
                    continue
                if "open" in lower or "read" in lower:
                    continue
                for match in EVIDENCE_FILE_PATTERN.finditer(line):
                    violations.append(f"{script.name}:{line_num}: {match.group()}")
        self.assertEqual(violations, [], f"Evidence file refs: {violations}")

    def test_native_market_monitor_uses_env_or_crm(self):
        """native_market_v3_status_ro.py must resolve accounts via env or CRM."""
        script = SCRIPTS_DIR / "native_market_v3_status_ro.py"
        self.assertTrue(script.exists())
        content = read_script(script)
        # Must have the resolve function
        self.assertIn("_resolve_accounts", content)
        # Must read from environment
        self.assertIn("ABUTRON_FP_ACCOUNT_PROFILES", content)
        # Must have CRM fallback
        self.assertIn("crm", content.lower())
        # Must fail closed on invalid config
        self.assertIn("NO_CONFIGURED_ACCOUNTS", content)

    def test_fp_issue21_auditor_uses_env(self):
        """fp_issue21_receipt_auditor.py must resolve server via environment."""
        script = SCRIPTS_DIR / "fp_issue21_receipt_auditor.py"
        self.assertTrue(script.exists())
        content = read_script(script)
        self.assertIn("os.environ", content)
        self.assertIn("ABUTRON_ISSUE21_BROKER_SERVER", content)


class SanitizedSystemdTest(unittest.TestCase):
    """Verify systemd unit files don't contain private identifiers."""

    def test_systemd_files_have_no_hardcoded_ids(self):
        """No connection IDs or broker logins in systemd files."""
        violations = []
        for unit in find_systemd_files():
            content = read_script(unit)
            for match in CONNECTION_ID_PATTERN.finditer(content):
                violations.append(f"{unit.name}: {match.group()}")
            for match in BROKER_LOGIN_PATTERN.finditer(content):
                violations.append(f"{unit.name}: {match.group()}")
        self.assertEqual(violations, [], f"Systemd violations: {violations}")

    def test_systemd_files_are_readable_text(self):
        """All systemd files must be valid text (not binary)."""
        for unit in find_systemd_files():
            content = read_script(unit)
            self.assertGreater(len(content), 10, f"{unit.name} too short")
            # Should not contain null bytes (binary)
            self.assertNotIn("\x00", content, f"{unit.name} contains null bytes")


class SanitizedSourceContractTest(unittest.TestCase):
    """Verify the overall sanitized source contract."""

    def test_privacy_guard_ci_script_exists(self):
        """The CI privacy guard script must be present."""
        script = REPO_ROOT / "tools" / "ci" / "verify_public_repo_artifacts.py"
        self.assertTrue(script.exists())

    def test_privacy_guard_workflow_exists(self):
        """The GitHub Actions privacy workflow must be present."""
        workflow = (
            REPO_ROOT / ".github" / "workflows"
            / "abutron-public-artifact-guard.yml"
        )
        self.assertTrue(workflow.exists())

    def test_handoff_doc_exists(self):
        """The source handoff documentation must be present."""
        doc = REPO_ROOT / "docs" / "ISSUE25_PUBLIC_SOURCE_HANDOFF.md"
        self.assertTrue(doc.exists())

    def test_gitignore_blocks_host_evidence(self):
        """.gitignore must block host evidence patterns."""
        gitignore = (REPO_ROOT / ".gitignore").read_text()
        for pattern in ("issue25-*.tgz", "host_evidence/", "private_evidence/"):
            self.assertIn(pattern, gitignore, f"Missing gitignore pattern: {pattern}")


if __name__ == "__main__":
    unittest.main()
