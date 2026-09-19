"""Local service rendezvous identity; never derive the advertised ID from a path.

The filename is a private local lookup key, not an HTTP value or access token.
This does not isolate mutually hostile processes under the same OS account.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import state_roots


def service_instance_id(root: Path, *, create: bool = True) -> str | None:
    uid = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    directory = Path(tempfile.gettempdir()) / ("bridge-console-identity-" + uid)
    state_roots.assert_no_links(directory)
    if not directory.exists():
        if not create:
            return None
        directory.mkdir(mode=0o700, exist_ok=True)
    state_roots.assert_no_links(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise state_roots.StateRootError("console identity directory refused")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise state_roots.StateRootError("console identity permissions refused")
    # This lookup digest remains local. Only a fresh random value leaves this module.
    key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()
    path = directory / (key + ".id")
    state_roots.assert_no_links(path)
    if not path.exists():
        if not create:
            return None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write((secrets.token_hex(32) + "\n").encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
    info = path.lstat()
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise state_roots.StateRootError("console identity permissions refused")
    try:
        value = state_roots.read_regular_bytes(path, max_bytes=65).decode("ascii")
    except (UnicodeDecodeError, OSError):
        raise state_roots.StateRootError("console identity unavailable") from None
    if not re.fullmatch(r"[0-9a-f]{64}\n", value):
        raise state_roots.StateRootError("console identity invalid")
    return value.rstrip("\n")
