"""V9 DEMO 6207 production runner — backward-compatible wrapper.

Delegates to the generalized multi-account runner in
``production_deriv_demo.py``. Retained for existing service
definitions, deployments and tests that reference this module path
and its module-level attributes (``QA_TERMINAL``, ``CRM_DB``,
``resolve_crm_demo_6207_connection``, ``execution_session_enabled``).
"""
from __future__ import annotations

from pathlib import Path

from .production_deriv_demo import (
    run, write_status, CRM_DB, MODEL_SHA,
)

# Backward-compatible module-level attributes.  Tests and legacy
# service definitions monkeypatch these directly, so they must remain
# module attributes of ``production_6207`` rather than being resolved
# only inside the delegated runner.
QA_TERMINAL = Path(
    "/var/lib/private/abutron-pr37-direct-writer-demo6207-qa/terminal")


def resolve_crm_demo_6207_connection() -> str:
    """Find the current CRM Deriv DEMO 6207 account."""
    from .production_deriv_demo import resolve_crm_demo_connection
    return resolve_crm_demo_connection("6207")


def execution_session_enabled() -> bool:
    """6207 session gate — delegates to the generalized runner while
    honouring the module-level ``QA_TERMINAL``, ``CRM_DB`` and
    ``resolve_crm_demo_6207_connection`` so existing monkeypatches
    keep working.
    """
    from .production_deriv_demo import execution_session_enabled as _enabled
    return _enabled(
        "6207",
        qa_terminal=QA_TERMINAL,
        connection_id=resolve_crm_demo_6207_connection(),
        crm_db=CRM_DB,
    )


def main(argv=None) -> int:
    """CLI entry point — delegates to the generalized runner."""
    from .production_deriv_demo import main as _main
    import sys
    # Rewrite argv to add --account 6207 if not already present
    args = list(argv if argv is not None else sys.argv[1:])
    # Strip the script name if present
    if args and not args[0].startswith("-"):
        args = args[1:]
    # Ensure --account 6207 is present
    has_account = any(a == "--account" for a in args)
    if not has_account:
        args = ["--account", "6207"] + args
    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
