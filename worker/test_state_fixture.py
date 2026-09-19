"""Synthetic entrypoint fixtures; not production authority code."""
import json
import subprocess
from pathlib import Path

def make_state(root: Path, *, git: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for directory in ("projects", "supervisor"):
        (root / directory).mkdir(exist_ok=True)
    (root / "bridge-state.json").write_text(json.dumps({"schema_version":1,
        "protocol_version":2,"kind":"synthetic-state","runtime_relative":"worker/runtime"}) + "\n")
    if git and not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root.resolve()
