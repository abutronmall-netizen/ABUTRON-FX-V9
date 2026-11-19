"""Issue #38: prevent the isolated no-order MT5 QA chart-limit recurrence.

This helper is only for a dedicated disposable QA MT5 data folder. The caller
must verify its own DynamicUser StateDirectory / broker identity and invoke
this BEFORE launching that QA terminal. NEVER use on a production MT5 profile.

MT5 /config Startup Expert opens a new chart. A reused QA profile accumulated
99 chartNN.chr entries, causing "open charts limit reached" and no fresh
signed exposure. Preserve the original QA profile once, then bound later
throwaway no-order QA chart state to one previous-session archive.

This source has NO order API, credential I/O, CRM or broker access.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil


def _validate_qa_chart_entries(directory: Path, *, maximum: int = 150):
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("UNTRUSTED_ISOLATED_QA_CHART_DIRECTORY")
    items = list(directory.iterdir())
    if len(items) > maximum:
        raise RuntimeError("ISOLATED_QA_CHART_SET_UNBOUNDED")
    for item in items:
        if (
            item.is_symlink()
            or not item.is_file()
            or (
                item.name != "order.wnd"
                and re.fullmatch(r"chart[0-9]{2,3}\.chr", item.name) is None
            )
            or item.stat().st_size > 2_000_000
        ):
            raise RuntimeError("UNEXPECTED_ISOLATED_QA_CHART_ARTIFACT")
    return items


def reset_isolated_probe_chart_capacity(state: Path, terminal: Path):
    """Provide an empty QA chart profile, without changing production.

    Original contents are moved (not deleted) once, so recovery remains
    possible. On later invocations, only the prior disposable QA profile may
    be retired; at most one extra prior-session profile is retained.
    """
    state = Path(state)
    terminal = Path(terminal)
    if (
        state.is_symlink()
        or not state.is_dir()
        or terminal.is_symlink()
        or not terminal.is_dir()
        or terminal.parent != state
        or terminal.name != "terminal"
    ):
        raise RuntimeError("NOT_AN_ISOLATED_ACCOUNT_TERMINAL")

    profiles = terminal / "MQL5" / "Profiles" / "Charts"
    if profiles.is_symlink():
        raise RuntimeError("QA_CHART_ROOT_SYMLINK_DENIED")
    profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    active = profiles / "Default"
    original = state / "issue38-original-pre-fix-qa-charts"
    previous = state / "issue38-previous-qa-session-charts"

    if any(p.is_symlink() for p in (active, original, previous)):
        raise RuntimeError("QA_CHART_ARCHIVE_SYMLINK_DENIED")
    if not active.exists():
        active.mkdir(mode=0o700)
        return {"original_qa_profile_preserved": original.is_dir(), "active_charts": 0}

    entries = _validate_qa_chart_entries(active)
    if not entries:
        return {"original_qa_profile_preserved": original.is_dir(), "active_charts": 0}

    if not original.exists():
        active.rename(original)
    else:
        if not original.is_dir():
            raise RuntimeError("QA_ORIGINAL_CHART_BACKUP_INVALID")
        if previous.exists():
            _validate_qa_chart_entries(previous, maximum=3)
            shutil.rmtree(previous)
        active.rename(previous)

    active.mkdir(mode=0o700)
    return {
        "original_qa_profile_preserved": original.is_dir(),
        "active_charts": 0,
    }
