#!/usr/bin/env python3
"""Hold a Codex launch until the Worker has attached this process to its scope."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _popen_options() -> dict[str, int]:
    """Keep the real Codex child console-free on Windows only."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _launch_child(command: list[str]) -> subprocess.Popen[bytes]:
    """Launch Codex with inherited stdio after the Worker releases the gate."""
    return subprocess.Popen(
        command,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        shell=False,
        **_popen_options(),
    )


def main() -> int:
    if len(sys.argv) < 5 or sys.argv[3] != "--":
        print(
            "usage: codex_process_gate.py <gate-file> <pid-file> -- <command...>",
            file=sys.stderr,
        )
        return 2
    gate_file = Path(sys.argv[1])
    pid_file = Path(sys.argv[2])
    command = sys.argv[4:]
    while not gate_file.exists():
        time.sleep(0.01)
    try:
        gate_file.unlink()
    except OSError:
        pass
    process = _launch_child(command)
    pid_file.write_text(str(process.pid), encoding="ascii")
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
