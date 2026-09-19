"""Minimal human-facing ``bridge`` command dispatcher."""

from __future__ import annotations

import argparse
from typing import Sequence

try:
    from . import bridge_dashboard, bridge_doctor
except ImportError:  # pragma: no cover - direct script support
    import bridge_dashboard
    import bridge_doctor


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", add_help=False)
    sub.add_parser("dashboard", add_help=False)
    args, rest = parser.parse_known_args(argv)
    if args.command == "doctor":
        return bridge_doctor.main(rest)
    return bridge_dashboard.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
