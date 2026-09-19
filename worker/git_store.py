#!/usr/bin/env python3
"""Git-backed canonical persistence and compare-and-swap publication.

The store is intentionally independent from Worker orchestration, Manual
protocol decisions, Codex lifecycle, alerts, email, and machine configuration.
It talks to a local Git checkout through the system ``git`` executable and
publishes already-built canonical payloads with bounded CAS retries.
"""

from __future__ import annotations

import os
import stat
import tempfile
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from bridge_common import (
    CASConflict,
    WorkerError,
    load_json,
)


DEFAULT_PUBLISH_RETRIES = 5


_GATE_LOCKS: dict[str, threading.RLock] = {}
_GATE_GUARD = threading.Lock()
_GATE_DEPTH = threading.local()


def _checked_repository_path(repository: Path, path: Path) -> Path:
    """Validate an existing or future path without following links.

    Reject static links/reparse points and portable aliases. This is not an OS
    sandbox against a hostile same-user process changing ancestors concurrently.
    """
    if not isinstance(repository, Path) or not isinstance(path, Path):
        raise WorkerError("Persistence paths must be Path values.")
    if ".." in path.parts:
        raise WorkerError("Persistence path traversal refused.")
    # Windows abspath can erase trailing dots/spaces before validation.
    # Reject their original lexical components before normalization.
    if any(part.endswith((".", " ")) for part in path.parts):
        raise WorkerError("Persistence path contains a reserved or ambiguous component.")
    root = Path(os.path.abspath(repository))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise WorkerError("Persistence path escaped the repository.") from None
    if not relative.parts:
        raise WorkerError("Persistence target must be a file below the repository.")
    reserved = {"con", "prn", "aux", "nul"} | {
        prefix + str(number) for prefix in ("com", "lpt") for number in range(1, 10)
    }
    for part in relative.parts:
        if (part.casefold() == ".git" or part.endswith((".", " "))
                or any(ord(c) < 32 or c in '<>:"|?*\\' for c in part)
                or part.split(".", 1)[0].casefold() in reserved):
            raise WorkerError("Persistence path contains a reserved or ambiguous component.")
    for current in (*reversed(target.parents), target):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise WorkerError("Linked persistence path refused.")
        if current != target and not stat.S_ISDIR(info.st_mode):
            raise WorkerError("Persistence ancestor is not a directory.")
        if current == target and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise WorkerError("Persistence target must be a single-link regular file.")
    if not root.is_dir():
        raise WorkerError("Persistence repository directory is unavailable.")
    return target


@contextmanager
def git_mutation_gate(repository: Path) -> Iterator[None]:
    """Serialize cooperating Git mutations without writing before lock ownership."""
    lock_path = _checked_repository_path(
        repository, repository / "worker" / "runtime" / "git-store.lock"
    )
    key = str(lock_path)
    with _GATE_GUARD:
        thread_lock = _GATE_LOCKS.setdefault(key, threading.RLock())
    depth = getattr(_GATE_DEPTH, "values", {})
    thread_lock.acquire()
    current_depth = int(depth.get(key, 0))
    depth[key] = current_depth + 1
    _GATE_DEPTH.values = depth
    handle = None
    acquired = False
    try:
        if current_depth == 0:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            _checked_repository_path(repository, lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags, 0o600)
            handle = os.fdopen(fd, "r+b")
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkerError("Unsafe Git gate file refused.")
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
            # Lock the range first, including for an initially empty file.
            # Never append to or rewrite an existing lock payload while waiting.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
        yield
    finally:
        depth[key] -= 1
        try:
            if handle is not None:
                try:
                    if acquired:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            thread_lock.release()


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one local non-shell command with bounded, decoded output."""

    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        shell=False,
        **process_options,
    )
    if check and result.returncode != 0:
        raise WorkerError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stderr[-4000:]}"
        )
    return result


def git(
    repository: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a checkout, optionally returning a non-zero result."""

    return run_process(["git", *args], cwd=repository, check=check)


def get_head(repository: Path) -> str | None:
    """Return a checkout's HEAD, or ``None`` when it is not readable."""

    result = run_process(["git", "rev-parse", "HEAD"], cwd=repository)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def current_branch(repository: Path) -> str:
    result = git(repository, "branch", "--show-current")
    branch = result.stdout.strip()
    if not branch:
        raise WorkerError("Bridge worker requires a checked-out branch.")
    return branch


def tracked_dirty(repository: Path) -> bool:
    """Return whether tracked files have local modifications."""

    result = git(
        repository,
        "status",
        "--porcelain",
        "--untracked-files=no",
        check=True,
    )
    return bool(result.stdout.strip())


def sync_to_remote(
    repository: Path,
    *,
    discard_worker_commit: bool = False,
) -> None:
    """Synchronize a dedicated checkout to ``origin/<current branch>``.

    Normal polling refuses to discard local commits.  A CAS retry may opt in to
    discarding the one clean, unpushed publication attempt that this store just
    made after a rejected push.  Tracked working-tree edits are always refused.
    """

    with git_mutation_gate(repository):
        if tracked_dirty(repository):
            raise WorkerError(
                "Bridge clone has tracked local modifications. The worker requires a "
                "dedicated clean clone; commit/revert those edits before continuing."
            )

        branch = current_branch(repository)
        git(repository, "fetch", "origin")
        remote = f"origin/{branch}"
        counts = git(
            repository,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{remote}",
        ).stdout.strip().split()
        if len(counts) != 2:
            raise WorkerError("Unable to compare local bridge HEAD with remote.")
        ahead, behind = map(int, counts)

        if ahead and not discard_worker_commit:
            raise WorkerError(
                "Bridge clone contains unpushed local commits. Refusing to discard them "
                "during normal polling."
            )

        if ahead or behind:
            git(repository, "reset", "--hard", remote)


def relative_path(path: Path, repository: Path) -> str:
    """Return a Git path relative to the checkout using forward slashes."""

    return str(path.relative_to(repository)).replace("\\", "/")


def read_blob(
    repository: Path,
    relative: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read one exact HEAD blob without text decoding or EOL conversion."""

    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise WorkerError("Git blob path is not a safe repository-relative path.")
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0
    ):
        raise WorkerError("Git blob size limit is invalid.")
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", f"HEAD:{relative}"],
            cwd=str(repository),
            capture_output=True,
            timeout=15,
            shell=False,
            **options,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise WorkerError("Git blob read did not complete within the bounded limit.") from exc
    if result.returncode != 0:
        raise WorkerError(f"Git blob is not readable: {relative}")
    payload = bytes(result.stdout)
    if max_bytes is not None and len(payload) > max_bytes:
        raise WorkerError("Git blob exceeds the bounded read limit.")
    return payload


def _write_payload_atomic(path: Path, payload: str | bytes) -> None:
    """Replace one payload using an exclusively created same-directory tempfile.

    Callers enforce repository confinement. Individual replacement is atomic;
    multiple files are not a crash-atomic filesystem transaction.
    """
    if isinstance(payload, str):
        data = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        data = payload
    else:
        raise WorkerError("Canonical payload must be UTF-8 text or bytes.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bridge-write-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_payloads(
    repository: Path, payloads: dict[Path, str | bytes]
) -> list[tuple[Path, str | bytes, str]]:
    if not isinstance(payloads, dict):
        raise WorkerError("Canonical payload batch must be a dictionary.")
    prepared: list[tuple[Path, str | bytes, str]] = []
    seen: set[str] = set()
    root = Path(os.path.abspath(repository))
    for path, value in payloads.items():
        if not isinstance(value, (str, bytes)):
            raise WorkerError("Canonical payload must be UTF-8 text or bytes.")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise WorkerError("Canonical text is not valid UTF-8.") from None
        target = _checked_repository_path(root, path)
        relative = target.relative_to(root).as_posix()
        identity = relative.casefold()
        if any(identity == old or identity.startswith(old + "/")
               or old.startswith(identity + "/") for old in seen):
            raise WorkerError("Canonical payload paths overlap or alias.")
        seen.add(identity)
        prepared.append((target, value, relative))
    return prepared


def commit_payloads(
    repository: Path,
    payloads: dict[Path, str | bytes],
    message: str,
) -> bool:
    """Validate the complete batch, then write, stage and commit exact payloads.

    No payload is written for an invalid initial batch. I/O failure after a
    validated batch starts may still leave working-tree changes; CAS recovery
    and operator handling remain required. This is not an OS isolation layer.
    """
    prepared = _validated_payloads(repository, payloads)
    with git_mutation_gate(repository):
        # Revalidate after waiting for the cooperating Git mutation gate.
        prepared = _validated_payloads(repository, dict((p, v) for p, v, _ in prepared))
        binary_paths: list[str] = []
        text_paths: list[str] = []
        for path, value, relative in prepared:
            _write_payload_atomic(path, value)
            if isinstance(value, bytes):
                binary_paths.append(relative)
            else:
                text_paths.append(relative)
        if text_paths:
            git(repository, "add", "--", *text_paths)
        if binary_paths:
            git(repository, "-c", "core.autocrlf=false", "add", "--", *binary_paths)
        staged = git(repository, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            raise WorkerError("Unable to inspect staged bridge changes.")
        git(repository, "commit", "-m", message)
        return True


def retire_tracked_paths(
    repository: Path,
    retirements: list[tuple[Path, Path, bytes]],
    message: str,
    *,
    retries: int = DEFAULT_PUBLISH_RETRIES,
) -> set[Path]:
    """Move immutable non-canonical inputs to an auditable Git history.

    Each tuple is ``(source, destination, expected_blob)``.  The operation is
    deliberately separate from canonical state publication: it only removes
    already-inspected transport inputs and preserves their exact bytes under a
    history path.  A source/destination mismatch is skipped fail-closed; it
    can never overwrite an existing history record.
    """

    if not retirements:
        return set()
    if isinstance(retries, bool) or retries < 1:
        raise WorkerError("Staged request retirement requires at least one retry.")
    if len(retirements) > 128:
        raise WorkerError("Staged request retirement batch exceeds the bounded limit.")

    repository = repository.resolve()
    normalized: list[tuple[Path, Path, bytes]] = []
    seen_sources: set[Path] = set()
    for source, destination, expected_blob in retirements:
        if not isinstance(source, Path) or not isinstance(destination, Path):
            raise WorkerError("Staged request retirement paths are invalid.")
        if not isinstance(expected_blob, bytes):
            raise WorkerError("Staged request retirement requires exact bytes.")
        source = source.resolve(strict=False)
        destination = destination.resolve(strict=False)
        try:
            relative_path(source, repository)
            relative_path(destination, repository)
        except (ValueError, TypeError):
            raise WorkerError("Staged request retirement path escaped the repository.") from None
        if source == destination or source in seen_sources:
            raise WorkerError("Staged request retirement contains duplicate paths.")
        seen_sources.add(source)
        normalized.append((source, destination, expected_blob))

    retired: set[Path] = set()
    last_error = ""
    with git_mutation_gate(repository):
        for attempt in range(retries):
            sync_to_remote(repository, discard_worker_commit=attempt > 0)
            ready: list[tuple[Path, Path | None, bytes]] = []
            for source, destination, expected_blob in normalized:
                source_relative = relative_path(source, repository)
                destination_relative = relative_path(destination, repository)
                try:
                    source_blob = read_blob(
                        repository,
                        source_relative,
                        max_bytes=len(expected_blob),
                    )
                except WorkerError:
                    try:
                        destination_blob = read_blob(
                            repository,
                            destination_relative,
                            max_bytes=len(expected_blob),
                        )
                    except WorkerError:
                        continue
                    if destination_blob == expected_blob:
                        retired.add(source)
                    continue
                if source_blob != expected_blob:
                    continue
                if source.is_symlink() or not source.is_file():
                    continue
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_file():
                        continue
                    try:
                        destination_blob = read_blob(
                            repository,
                            destination_relative,
                            max_bytes=len(expected_blob),
                        )
                    except WorkerError:
                        continue
                    if destination_blob != expected_blob:
                        continue
                    ready.append((source, None, expected_blob))
                else:
                    ready.append((source, destination, expected_blob))

            if not ready:
                return retired

            add_paths: list[str] = []
            remove_paths: list[str] = []
            for source, destination, _expected_blob in ready:
                source_relative = relative_path(source, repository)
                remove_paths.append(source_relative)
                if destination is None:
                    source.unlink()
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(destination)
                    add_paths.append(relative_path(destination, repository))
            if add_paths:
                git(
                    repository,
                    "-c",
                    "core.autocrlf=false",
                    "add",
                    "--",
                    *add_paths,
                )
            git(repository, "rm", "--", *remove_paths)
            staged = git(repository, "diff", "--cached", "--quiet", check=False)
            if staged.returncode == 0:
                return retired
            if staged.returncode != 1:
                raise WorkerError("Unable to inspect staged request retirement.")
            git(repository, "commit", "-m", message)
            branch = current_branch(repository)
            pushed = git(
                repository,
                "push",
                "origin",
                f"HEAD:{branch}",
                check=False,
            )
            if pushed.returncode == 0:
                retired.update(source for source, _destination, _blob in ready)
                return retired

            last_error = pushed.stderr[-4000:] or pushed.stdout[-4000:]
            git(repository, "fetch", "origin", check=False)
            git(repository, "reset", "--hard", f"origin/{branch}", check=False)

    raise WorkerError(
        "Unable to retire staged requests after bounded retries.\n" + last_error
    )


def publish_cas(
    *,
    bridge_root: Path,
    state_path: Path,
    expected: Callable[[dict[str, Any]], bool],
    already_applied: Callable[[dict[str, Any]], bool],
    payload_builder: Callable[[dict[str, Any]], dict[Path, str | bytes]],
    message: str,
    retries: int = DEFAULT_PUBLISH_RETRIES,
    push_effect_recorder: object | None = None,
) -> dict[str, Any]:
    """Publish canonical files with remote-safe, bounded CAS retry.

    Each attempt begins by synchronizing to the latest remote branch, then
    re-reads canonical state.  ``already_applied`` is checked before ``expected``
    so a completed publication is idempotent.  A rejected push only replays the
    already-built payload logic after discarding this store's clean unpushed
    commit; it never invokes an executor or any task lifecycle operation.
    """

    if isinstance(retries, bool) or retries < 1:
        raise WorkerError("CAS publication requires at least one retry attempt.")

    last_error = ""
    with git_mutation_gate(bridge_root):
        for attempt in range(retries):
            sync_to_remote(bridge_root, discard_worker_commit=attempt > 0)
            current = load_json(state_path)

            if already_applied(current):
                return current
            if not expected(current):
                raise CASConflict(
                    "Bridge state changed before publish; refusing to overwrite newer state."
                )

            payloads = payload_builder(current)
            changed = commit_payloads(bridge_root, payloads, message)
            if not changed:
                return load_json(state_path)

            branch = current_branch(bridge_root)
            if push_effect_recorder is not None:
                # This branch is deliberately single-attempt: an exact
                # intent with an absent confirmation is reconciled read-only
                # and is never hidden behind the historical CAS retry loop.
                from vnext_runtime.git_effects import (
                    GitLsRemoteRefReader,
                    GitPushIntent,
                    GitPushReconciler,
                    PushResolutionState,
                )

                target_ref = f"refs/heads/{branch}"
                expected_object = get_head(bridge_root)

                def discard_unpublished_attempt() -> None:
                    # This discards only the clean local commit made by this
                    # publication attempt.  It never rewrites the remote ref;
                    # the pending report and exact-run WAL remain the durable
                    # evidence for the next publication-only recovery.
                    try:
                        sync_to_remote(bridge_root, discard_worker_commit=True)
                    except WorkerError:
                        pass

                if expected_object is None:
                    discard_unpublished_attempt()
                    raise WorkerError("Git push expected object could not be read")
                reader = GitLsRemoteRefReader(bridge_root)
                try:
                    precondition_object = reader.read_ref("origin", target_ref)
                except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
                    discard_unpublished_attempt()
                    raise WorkerError(
                        "Git push precondition could not be observed exactly"
                    ) from exc
                if precondition_object is None:
                    precondition_object = "0" * len(expected_object)
                recorder = getattr(push_effect_recorder, "record_push_intent", None)
                if not callable(recorder):
                    raise WorkerError("Git push effect recorder is not writable")
                try:
                    intent = recorder(
                        remote="origin",
                        target_ref=target_ref,
                        expected_object=expected_object,
                        precondition_object=precondition_object,
                    )
                except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, TypeError) as exc:
                    discard_unpublished_attempt()
                    raise WorkerError("Git push intent could not be recorded") from exc
                if not isinstance(intent, GitPushIntent):
                    discard_unpublished_attempt()
                    raise WorkerError("Git push effect recorder returned a non-exact intent")
                try:
                    pushed = git(
                        bridge_root,
                        "push",
                        "origin",
                        f"HEAD:{branch}",
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    discard_unpublished_attempt()
                    raise WorkerError("Git push did not return a bounded result") from exc
                reconciliation = GitPushReconciler(reader).reconcile(intent)
                if reconciliation.state is PushResolutionState.EXACT_APPLIED:
                    confirmed = getattr(
                        push_effect_recorder,
                        "record_push_confirmed",
                        None,
                    )
                    if not callable(confirmed):
                        raise WorkerError("Git push confirmation recorder is not writable")
                    try:
                        confirmed(reconciliation)
                    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, TypeError) as exc:
                        raise WorkerError("Git push confirmation could not be recorded") from exc
                    return load_json(state_path)
                result_label = reconciliation.state.value
                discard_unpublished_attempt()
                raise WorkerError(
                    "Git push effect was not mechanically confirmed; "
                    f"recovery classification is {result_label}; "
                    f"write_return_code={pushed.returncode}"
                )

            pushed = git(
                bridge_root,
                "push",
                "origin",
                f"HEAD:{branch}",
                check=False,
            )
            if pushed.returncode == 0:
                return load_json(state_path)

            last_error = pushed.stderr[-4000:] or pushed.stdout[-4000:]
            # A failed push means only the local publication attempt is disposable.
            # The next attempt starts with fresh remote state and re-evaluates CAS.
            git(bridge_root, "fetch", "origin", check=False)
            git(bridge_root, "reset", "--hard", f"origin/{branch}", check=False)

    raise WorkerError(
        "Unable to publish bridge state after bounded retries. Codex will NOT be "
        f"rerun automatically.\n{last_error}"
    )
