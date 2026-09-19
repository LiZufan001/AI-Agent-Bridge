#!/usr/bin/env python3
"""Small standard-library support shared by the Worker and manual fast lane.

This module deliberately contains no protocol state-machine decisions and no
Git or Codex integration.  It owns only the low-level errors, clock, JSON/text,
and local atomic-file helpers that both execution paths need.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkerError(RuntimeError):
    """A recoverable or operational Bridge execution error."""


class CASConflict(WorkerError):
    """The canonical Bridge state changed while an operation was prepared."""


def now_dt() -> datetime:
    """Return the local timezone-aware current time used in reports/state."""

    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    return now_dt().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    """Load a canonical/support JSON object and reject non-object payloads."""

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise WorkerError(f"Expected JSON object: {path}")
    return data


def json_text(data: dict[str, Any]) -> str:
    """Render Bridge JSON in the existing stable, UTF-8-friendly format."""

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    """Replace one text file through a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def save_pending_report(
    runtime_dir: Path,
    command_id: int,
    report_text: str,
    reason: str,
) -> None:
    """Persist non-canonical report evidence when a remote CAS cannot finish."""

    runtime_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        runtime_dir / f"pending-report-{command_id:03d}.md",
        report_text,
    )
    write_text_atomic(
        runtime_dir / f"pending-report-{command_id:03d}.reason.txt",
        reason.rstrip() + "\n",
    )


def ensure_tool(name: str) -> None:
    """Require one local executable without adding configuration policy."""

    if shutil.which(name) is None:
        raise WorkerError(f"Required command not found on PATH: {name}")
