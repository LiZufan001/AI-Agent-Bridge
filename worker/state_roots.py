"""Thin Engine / State location contract; never implements protocol transitions.

Only entry points resolve locations. Existing persistence/recovery functions keep
receiving one explicit `bridge_root`, now the State Git root. Source inspection,
imports, assets and engine updates continue to use `engine_root()`.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Mapping

STATE_ENV = "AI_AGENT_BRIDGE_STATE_ROOT"
MARKER = "bridge-state.json"
BINDING = Path("worker/deployment.local.json")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class StateRootError(RuntimeError):
    """The requested authority is missing, ambiguous, or unsafe."""


def engine_root() -> Path:
    return Path(__file__).resolve().parents[1]


def fixture_root() -> Path:
    return engine_root() / "tests/fixtures/synthetic-state"


def overlaps(first: Path, second: Path) -> bool:
    a, b = first.resolve(), second.resolve()
    return a == b or a in b.parents or b in a.parents


def _object(path: Path, max_bytes: int = 65536) -> dict:
    def pairs(items: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise StateRootError("duplicate configuration key")
            result[key] = value
        return result
    try:
        value = json.loads(read_regular_bytes(path, max_bytes=max_bytes).decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(StateRootError("nonfinite JSON")))
    except (OSError, ValueError) as exc:
        raise StateRootError("configuration unavailable") from None
    if not isinstance(value, dict):
        raise StateRootError("configuration must be an object")
    return value


def assert_no_links(path: Path) -> None:
    """Refuse static links/reparse points before resolving or reading any child.

    This is a cooperating-process boundary, not a sandbox against an attacker
    swapping ancestor directories concurrently under the same OS identity.
    """
    if ".." in path.parts:
        raise StateRootError("State path traversal refused")
    absolute = Path(os.path.abspath(path))
    for component in (*reversed(absolute.parents), absolute):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise StateRootError("State path unavailable") from None
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise StateRootError("linked State path is not supported")
        if component != absolute and not stat.S_ISDIR(info.st_mode):
            raise StateRootError("State ancestor is not a directory")


def read_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Bounded read of a single-link regular file; never open a known link/FIFO.

    O_NOFOLLOW protects the final component where supported. Descriptor identity
    and type are rechecked, but hostile concurrent ancestor swaps are not isolated.
    No path or file contents are included in the outward error message.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise StateRootError("invalid State read limit")
    fd = None
    try:
        assert_no_links(path)
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > max_bytes):
            raise StateRootError("State file type, link count or size refused")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        after = os.fstat(fd)
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                or after.st_size > max_bytes
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or getattr(after, "st_file_attributes", 0) & 0x400):
            raise StateRootError("State file changed or is unsafe")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise StateRootError("State file exceeds read limit")
        return payload
    except OSError:
        raise StateRootError("State file unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def marker(root: Path) -> dict:
    value = _object(root / MARKER)
    if (value.get("schema_version") != 1 or type(value.get("schema_version")) is not int
            or value.get("protocol_version") != 2
            or value.get("kind") not in {"private-state", "synthetic-state"}
            or value.get("runtime_relative") != "worker/runtime"):
        raise StateRootError("unsupported State marker")
    return value


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=15, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateRootError("State Git identity unavailable") from exc
    return result.stdout.strip()


def assert_independent_git(root: Path) -> None:
    gitdir = root / ".git"
    if not gitdir.is_dir() or gitdir.is_symlink():
        raise StateRootError("State requires an independent clone, not a linked worktree")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise StateRootError("State must be the Git top level")
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    if common.resolve() != gitdir.resolve():
        raise StateRootError("shared Git common directory refused")


def resolve_state_root(explicit: str | Path | None = None, *, for_write: bool = False,
                       environ: Mapping[str, str] | None = None) -> Path:
    """CLI > environment > marked sibling (read only) > bundled fixture (read only).

    Mutation never discovers a sibling or falls back to bundled test data.
    There is deliberately no `projects` fallback under the running Engine.
    """
    env = os.environ if environ is None else environ
    value = explicit if explicit is not None else env.get(STATE_ENV)
    if not value:
        if for_write:
            raise StateRootError("explicit --state-root or AI_AGENT_BRIDGE_STATE_ROOT required for mutation")
        sibling = engine_root().with_name(engine_root().name + "-State")
        value = sibling if (sibling / MARKER).is_file() else fixture_root()
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise StateRootError("State root must be absolute")
    assert_no_links(path)
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise StateRootError("State root is not a directory")
    marker(root)
    bundled = root == fixture_root().resolve()
    if overlaps(root, engine_root()) and not (bundled and not for_write):
        raise StateRootError("Engine and writable State roots must be disjoint")
    for directory in ("projects", "supervisor", "worker"):
        assert_no_links(root / directory)
    if not (root / "projects").is_dir() or not (root / "supervisor").is_dir():
        raise StateRootError("State layout incomplete")
    if for_write:
        assert_independent_git(root)
    return root


def validate_control_contract(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"repository", "branch", "path", "fail_closed_mode"}:
        raise StateRootError("Owner control binding shape invalid")
    repo, branch, path = value.get("repository"), value.get("branch"), value.get("path")
    if (not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo)
            or not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", branch)
            or ".." in branch or branch.startswith("/")
            or not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,256}", path)
            or ".." in path or path.startswith("/")
            or value.get("fail_closed_mode") != "paused"):
        raise StateRootError("Owner control binding invalid")
    return dict(value)


def deployment_binding(root: Path) -> dict:
    value = _object(root / BINDING)
    if value.get("schema_version") != 1 or type(value.get("schema_version")) is not int:
        raise StateRootError("deployment binding version invalid")
    validate_control_contract(value.get("execution_control"))
    return value


def expected_control(root: Path, synthetic_default: dict) -> dict:
    """A marked private State must match an Owner-installed, Git-ignored binding.

    Unmarked temporary repositories remain supported by low-level regression
    fixtures, not production CLI entry points. Their contract is synthetic.
    """
    if (root / MARKER).exists() and marker(root)["kind"] == "private-state":
        return validate_control_contract(deployment_binding(root).get("execution_control"))
    return validate_control_contract(synthetic_default)


def inherit_local_binding(source: Path, clone: Path) -> None:
    """Manual CAS clones inherit only the non-secret Owner binding, never config/secrets."""
    if (source / MARKER).exists() and marker(source)["kind"] == "private-state":
        binding = deployment_binding(source)
        target = clone / BINDING
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise StateRootError("deployment binding must not be Git-tracked")
        target.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")


def require_split_runtime_policy(root: Path, config: dict) -> None:
    """Retain the old controlled-adoption implementation, but do not run it split.

    It couples code promotion to the same Git authority as canonical state.
    Split Engine deployment is an explicit, quiescent operator operation.
    """
    if not (root / MARKER).exists():
        return
    adoption = config.get("adoption", {})
    if not isinstance(adoption, dict) or any(adoption.get(k, False) for k in (
            "controlled_adoption_enabled", "unattended_adoption_enabled")):
        raise StateRootError("in-process code adoption is disabled for split repositories")
    if marker(root)["kind"] == "private-state":
        bootstrap = _object(root / "supervisor/bootstrap.json")
        if validate_control_contract(bootstrap.get("execution_control")) != expected_control(root, {}):
            raise StateRootError("State control location differs from the Owner binding")


def guard_execution_workdir(state: Path, workdir: Path, project_config: dict) -> None:
    if not (state / MARKER).exists():
        return
    if overlaps(state, workdir) or overlaps(engine_root(), workdir):
        raise StateRootError("executor workdir must not overlap State or running Engine")
    if project_config.get("self_maintenance", {}).get("enabled"):
        # A same-user full-access process is not an isolation boundary. Never
        # claim that a post-run fingerprint prevents private State mutation.
        raise StateRootError("split self-maintenance requires an isolated executor; disabled pending Owner acceptance")
