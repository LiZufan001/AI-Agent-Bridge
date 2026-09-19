"""Rollback-safe self-maintenance planning primitives.

This module is deliberately an outer-control planning boundary.  It can read
Git identities, build a reconciliation/promotion/rollback plan, persist a
small piece of local adoption evidence, and gate local admission during a
controlled restart.  It never mutates a Git checkout, publishes Protocol-v2
state, launches a Worker, or restarts a process.

The separation is important for self-maintenance: the Worker being replaced
cannot be the authority that proves its own replacement healthy or performs a
rollback after it stops responding.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from threading import Condition, RLock
from typing import Mapping, Sequence


PROMOTION_RESTART_EXIT_CODE = 75
EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_HEALTH_CRITERIA = 16
MAX_HEALTH_CRITERION_LENGTH = 128
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_RELATIVE_PATH_RE = re.compile(r"^[^/\\][^\r\n\x00]*$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,127}$")
_HEALTH_ATTRIBUTES = {
    "launcher_alive": "launcher_alive",
    "worker_healthy": "worker_healthy",
    "protocol_ready": "protocol_ready",
    "recovery_clear": "recovery_clear",
}
_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:|cookie\s*:|(?:token|secret|password|api[_-]?key)\s*[=:]|"
    r"ghp_|github_pat_|sk-(?:proj-)?|-----begin)"
)
_BLOB_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class AdoptionError(RuntimeError):
    """Base class for fail-closed adoption planning errors."""


class AdoptionValidationError(AdoptionError, ValueError):
    """Raised when an adoption identity or evidence value is unsafe."""


class CandidateValidationError(AdoptionValidationError):
    """Raised when the accepted Candidate identity cannot be frozen safely."""


class MainHeadStaleError(AdoptionError):
    """Raised when the latest-main identity changed during revalidation."""


class ReconciliationConflictError(AdoptionError):
    """Raised when Candidate changes cannot be reconciled safely."""

    def __init__(self, conflicts: tuple["ReconciliationConflict", ...]) -> None:
        self.conflicts = conflicts
        paths = ", ".join(conflict.path for conflict in conflicts[:8])
        suffix = "" if len(conflicts) <= 8 else ", ..."
        super().__init__(f"reconciliation rejected for protected/conflicting paths: {paths}{suffix}")


class PromotionRejectedError(AdoptionError):
    """Raised when a non-force promotion precondition is not true."""


class RollbackRejectedError(AdoptionError):
    """Raised when a forward rollback cannot preserve history safely."""


class EvidenceIntegrityError(AdoptionError):
    """Raised when durable local adoption evidence is absent or tampered."""


class AdoptionDisabledError(AdoptionError):
    """Raised when controlled or unattended adoption is not explicitly armed."""


class AdmissionBarrierError(AdoptionError):
    """Raised when a local admission barrier is used after closure."""


class AdoptionMode(str, Enum):
    """The mode recorded in evidence; neither mode changes live state here."""

    MANUAL = "manual"
    UNATTENDED = "unattended"


class ConflictCategory(str, Enum):
    """Categories whose ambiguity must stop a reconciliation."""

    PROTOCOL_AUTHORITY = "protocol_authority"
    PROJECT_CONTROL_PLANE = "project_control_plane"
    PROTECTED_PATH = "protected_path"
    LAUNCHER_SAFETY = "launcher_safety"
    RECOVERY_IDENTITY = "recovery_identity"
    SOURCE_OVERLAP = "source_overlap"


class BarrierState(str, Enum):
    """Local Coordinator lifecycle; never a Protocol-v2 project state."""

    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    DRAINED = "DRAINED"
    CLOSED = "CLOSED"


class HealthGateStatus(str, Enum):
    """Outer-controller health gate states during a controlled restart."""

    WAITING_FOR_RESTART = "WAITING_FOR_RESTART"
    PROBATION = "PROBATION"
    READY = "READY"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


def validate_sha(value: object, label: str = "sha") -> str:
    """Validate and normalize one full Git commit SHA."""

    if not isinstance(value, str) or _SHA_RE.fullmatch(value.strip()) is None:
        raise AdoptionValidationError(f"{label}_invalid")
    return value.strip().lower()


def validate_branch(value: object, label: str = "branch") -> str:
    """Validate a branch name without permitting ref-expression tricks."""

    if not isinstance(value, str):
        raise AdoptionValidationError(f"{label}_invalid")
    branch = value.strip()
    if (
        _BRANCH_RE.fullmatch(branch) is None
        or branch.casefold() == "main"
        or branch.startswith((".", "/"))
        or branch.endswith((".", "/"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise AdoptionValidationError(f"{label}_forbidden")
    return branch


def _validate_main_branch(value: object, label: str = "branch") -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdoptionValidationError(f"{label}_invalid")
    branch = value.strip()
    if (
        _BRANCH_RE.fullmatch(branch) is None
        or branch.casefold() != "main"
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise AdoptionValidationError(f"{label}_forbidden")
    return branch


def _validate_git_ref(value: object, label: str = "git_ref") -> str:
    text = _safe_text(value, label, limit=256)
    if text.startswith(("-", ".")) or ".." in text or "@{" in text:
        raise AdoptionValidationError(f"{label}_forbidden")
    return text


def _safe_text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str):
        raise AdoptionValidationError(f"{label}_invalid")
    text = value.strip()
    if not text or len(text) > limit or "\x00" in text or "\r" in text or "\n" in text:
        raise AdoptionValidationError(f"{label}_invalid")
    if _SECRET_RE.search(text):
        raise AdoptionValidationError(f"{label}_unsafe")
    return text


def _safe_token(value: object, label: str, *, limit: int = 128) -> str:
    text = _safe_text(value, label, limit=limit)
    if _SAFE_TOKEN_RE.fullmatch(text) is None:
        raise AdoptionValidationError(f"{label}_unsafe")
    return text


def _parse_time(value: object, label: str) -> datetime:
    text = _safe_text(value, label, limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdoptionValidationError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise AdoptionValidationError(f"{label}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    )


def _canonical_root(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AdoptionValidationError("repository_root_invalid") from exc


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Read-only identity observed from one repository checkout."""

    root: Path
    branch: str
    head_sha: str
    remote: str | None
    clean: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "head_sha", validate_sha(self.head_sha, "head_sha"))
        if not isinstance(self.branch, str) or not self.branch.strip():
            raise AdoptionValidationError("repository_branch_invalid")
        if self.remote is not None:
            _safe_text(self.remote, "repository_remote", limit=512)
        if not isinstance(self.clean, bool):
            raise AdoptionValidationError("repository_clean_invalid")


class GitInspector:
    """Bounded read-only Git inspection; no mutating Git subcommand is exposed."""

    def __init__(self, root: Path) -> None:
        self.root = _canonical_root(root)
        self._blob_sha_cache: dict[tuple[str, str], str | None] = {}
        self._blob_bytes_cache: dict[tuple[str, str], bytes | None] = {}

    def _run(self, *args: str) -> str:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdoptionValidationError("git_inspection_failed") from exc
        if completed.returncode != 0:
            raise AdoptionValidationError("git_inspection_failed")
        return completed.stdout.strip()

    def resolve_commit(self, ref: str) -> str:
        """Resolve a ref to a commit without updating any ref."""

        ref_text = _validate_git_ref(ref)
        return validate_sha(self._run("rev-parse", "--verify", f"{ref_text}^{{commit}}"), "resolved_sha")

    def inspect(self) -> RepositorySnapshot:
        """Read branch, HEAD, remote and worktree cleanliness."""

        reported_root = _canonical_root(Path(self._run("rev-parse", "--show-toplevel")))
        if reported_root != self.root:
            raise AdoptionValidationError("git_root_mismatch")
        branch = self._run("branch", "--show-current")
        if not branch:
            raise AdoptionValidationError("git_detached_head")
        try:
            remote_output = self._run("config", "--get", "remote.origin.url")
        except AdoptionValidationError:
            # A temporary/local repository may intentionally have no origin.
            # Other identity reads above still fail closed when Git itself is
            # unavailable or the checkout is not a repository.
            remote_output = ""
        clean = not bool(self._run("status", "--porcelain=v1", "--untracked-files=all"))
        return RepositorySnapshot(
            root=self.root,
            branch=branch,
            head_sha=self.resolve_commit("HEAD"),
            remote=remote_output or None,
            clean=clean,
        )

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Return Git ancestry using a read-only merge-base query."""

        first = validate_sha(ancestor, "ancestor_sha")
        second = validate_sha(descendant, "descendant_sha")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), "merge-base", "--is-ancestor", first, second],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdoptionValidationError("git_inspection_failed") from exc
        if completed.returncode not in (0, 1):
            raise AdoptionValidationError("git_inspection_failed")
        return completed.returncode == 0

    def merge_base(self, first: str, second: str) -> str:
        """Return the immutable merge base of two commit identities."""

        left = validate_sha(first, "first_sha")
        right = validate_sha(second, "second_sha")
        return validate_sha(self._run("merge-base", left, right), "merge_base_sha")

    def _run_bytes(self, *args: str) -> bytes:
        """Run one bounded read-only Git command and return raw stdout."""

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdoptionValidationError("git_inspection_failed") from exc
        if completed.returncode != 0:
            raise AdoptionValidationError("git_inspection_failed")
        return completed.stdout

    def blob_sha(self, commit: str, path: str) -> str | None:
        """Return one exact file blob identity, or ``None`` when absent.

        ``ls-tree`` is used instead of treating every ``rev-parse`` failure as
        a missing file.  A broken repository therefore remains an inspection
        failure rather than being silently interpreted as a deletion.
        """

        resolved_commit = validate_sha(commit, "commit_sha")
        normalized_path = _normalize_relative_path(path)
        cache_key = (resolved_commit, normalized_path)
        if cache_key in self._blob_sha_cache:
            return self._blob_sha_cache[cache_key]
        output = self._run_bytes(
            "ls-tree",
            "-z",
            "--full-tree",
            resolved_commit,
            "--",
            normalized_path,
        )
        if not output:
            self._blob_sha_cache[cache_key] = None
            return None
        entry = output.split(b"\x00", 1)[0]
        header, separator, _entry_path = entry.partition(b"\t")
        if not separator:
            raise AdoptionValidationError("git_tree_entry_invalid")
        fields = header.split()
        if len(fields) != 3 or fields[1] != b"blob":
            raise AdoptionValidationError("git_tree_entry_not_file")
        try:
            blob = fields[2].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AdoptionValidationError("git_blob_sha_invalid") from exc
        if _BLOB_SHA_RE.fullmatch(blob) is None:
            raise AdoptionValidationError("git_blob_sha_invalid")
        normalized_blob = blob.lower()
        self._blob_sha_cache[cache_key] = normalized_blob
        return normalized_blob

    def blob_bytes(self, commit: str, path: str) -> bytes | None:
        """Read one exact file from a commit without touching the worktree."""

        resolved_commit = validate_sha(commit, "commit_sha")
        normalized_path = _normalize_relative_path(path)
        cache_key = (resolved_commit, normalized_path)
        if cache_key in self._blob_bytes_cache:
            return self._blob_bytes_cache[cache_key]
        blob = self.blob_sha(resolved_commit, normalized_path)
        if blob is None:
            self._blob_bytes_cache[cache_key] = None
            return None
        content = self._run_bytes("cat-file", "blob", blob)
        self._blob_bytes_cache[cache_key] = content
        return content

    def three_way_merge_conflict(
        self,
        base_commit: str,
        candidate_commit: str,
        main_commit: str,
        path: str,
    ) -> bool:
        """Check one file with Git's merge-file algorithm in a temp fixture.

        The repository's refs, index and worktree are never modified.
        A result of ``True`` means Git did not prove a clean merge; malformed
        or unavailable repository data also fails closed.
        """

        base = self.blob_bytes(base_commit, path)
        candidate = self.blob_bytes(candidate_commit, path)
        main = self.blob_bytes(main_commit, path)
        if candidate == main:
            return False
        if base is None:
            return candidate != main
        if candidate == base or main == base:
            return False
        if candidate is None or main is None:
            return True
        with tempfile.TemporaryDirectory(prefix="bridge-reconcile-merge-") as temporary:
            root = Path(temporary)
            ours = root / "candidate"
            ancestor = root / "base"
            theirs = root / "main"
            ours.write_bytes(candidate)
            ancestor.write_bytes(base)
            theirs.write_bytes(main)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                completed = subprocess.run(
                    [
                        "git",
                        "merge-file",
                        "--quiet",
                        "--diff3",
                        "--stdout",
                        str(ours),
                        str(ancestor),
                        str(theirs),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                    creationflags=creationflags,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AdoptionValidationError("git_merge_inspection_failed") from exc
        # ``git merge-file`` may return the number of conflict regions rather
        # than a boolean on some Git builds.  Any non-zero result is unsafe;
        # only an exact zero result is a mechanically clean merge.
        return completed.returncode != 0

    def changed_paths(self, base_sha: str, head_sha: str) -> tuple[str, ...]:
        """Return bounded, normalized changed paths between two commits."""

        base = validate_sha(base_sha, "base_sha")
        head = validate_sha(head_sha, "head_sha")
        output = self._run("diff", "--name-only", "--no-renames", base, head, "--")
        paths = tuple(sorted({_normalize_relative_path(line) for line in output.splitlines() if line.strip()}))
        if len(paths) > 10_000:
            raise AdoptionValidationError("changed_path_set_too_large")
        return paths

    def path_touched_since(self, base_sha: str, head_sha: str, path: str) -> bool:
        """Return whether an intervening commit touched one exact path.

        A tree diff cannot distinguish an untouched path from one changed and
        later reverted.  The narrow one-sided Protocol proof uses this history
        query so it never overwrites a newer authority merely because the
        final blobs happen to match the merge base.
        """

        base = validate_sha(base_sha, "base_sha")
        head = validate_sha(head_sha, "head_sha")
        normalized_path = _normalize_relative_path(path)
        output = self._run(
            "log",
            "--format=",
            "--name-only",
            f"{base}..{head}",
            "--",
            normalized_path,
        )
        return normalized_path in {
            _normalize_relative_path(line)
            for line in output.splitlines()
            if line.strip()
        }


@dataclass(frozen=True, slots=True)
class CandidateFreeze:
    """Fixed Candidate construction identity for one maintenance phase."""

    candidate_root: Path
    candidate_branch: str
    bootstrap_base_sha: str
    accepted_candidate_sha: str
    frozen_at: str
    repository: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_root", Path(self.candidate_root).resolve())
        object.__setattr__(self, "candidate_branch", validate_branch(self.candidate_branch, "candidate_branch"))
        object.__setattr__(self, "bootstrap_base_sha", validate_sha(self.bootstrap_base_sha, "bootstrap_base_sha"))
        object.__setattr__(self, "accepted_candidate_sha", validate_sha(self.accepted_candidate_sha, "accepted_candidate_sha"))
        _parse_time(self.frozen_at, "frozen_at")
        if self.repository is not None:
            _safe_text(self.repository, "repository", limit=512)


@dataclass(frozen=True, slots=True)
class MainRevalidation:
    """The exact live-main ref identity observed at a reconciliation boundary."""

    ref: str
    expected_main_sha: str
    observed_main_sha: str
    revalidated_at: str
    branch: str = "main"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", _validate_git_ref(self.ref, "main_ref"))
        object.__setattr__(self, "expected_main_sha", validate_sha(self.expected_main_sha, "expected_main_sha"))
        object.__setattr__(self, "observed_main_sha", validate_sha(self.observed_main_sha, "observed_main_sha"))
        if self.expected_main_sha != self.observed_main_sha:
            raise MainHeadStaleError("latest main changed during revalidation")
        object.__setattr__(self, "branch", _validate_main_branch(self.branch, "main_branch"))
        _parse_time(self.revalidated_at, "revalidated_at")

    @property
    def latest_main_sha(self) -> str:
        """Return the revalidated main identity under its semantic name."""

        return self.observed_main_sha


def freeze_candidate(
    root: Path,
    *,
    bootstrap_base_sha: str,
    candidate_branch: str,
    accepted_candidate_sha: str | None = None,
    repository: str | None = None,
    now: datetime | None = None,
    inspector: GitInspector | None = None,
) -> CandidateFreeze:
    """Freeze and validate Candidate SHA/branch ancestry without mutation."""

    base = validate_sha(bootstrap_base_sha, "bootstrap_base_sha")
    branch = validate_branch(candidate_branch, "candidate_branch")
    git = inspector or GitInspector(root)
    if git.root != _canonical_root(root):
        raise CandidateValidationError("candidate_inspector_root_mismatch")
    snapshot = git.inspect()
    if snapshot.branch != branch:
        raise CandidateValidationError("candidate_branch_mismatch")
    if not snapshot.clean:
        raise CandidateValidationError("candidate_worktree_dirty")
    accepted = validate_sha(
        snapshot.head_sha if accepted_candidate_sha is None else accepted_candidate_sha,
        "accepted_candidate_sha",
    )
    if accepted != snapshot.head_sha:
        raise CandidateValidationError("candidate_head_changed")
    try:
        resolved = git.resolve_commit(accepted)
    except AdoptionValidationError as exc:
        raise CandidateValidationError("accepted_candidate_missing") from exc
    if resolved != accepted:
        raise CandidateValidationError("accepted_candidate_not_commit")
    try:
        is_descendant = git.is_ancestor(base, accepted)
    except AdoptionValidationError as exc:
        raise CandidateValidationError("candidate_ancestry_unavailable") from exc
    if not is_descendant or base == accepted:
        raise CandidateValidationError("candidate_not_derived_from_bootstrap")
    expected_repository = (
        _safe_text(repository, "repository", limit=512) if repository is not None else None
    )
    if expected_repository is not None and (snapshot.remote or "").strip() != expected_repository:
        raise CandidateValidationError("candidate_repository_mismatch")
    timestamp = _iso(now or datetime.now(timezone.utc))
    return CandidateFreeze(
        candidate_root=snapshot.root,
        candidate_branch=branch,
        bootstrap_base_sha=base,
        accepted_candidate_sha=accepted,
        frozen_at=timestamp,
        repository=expected_repository if expected_repository is not None else snapshot.remote,
    )


def revalidate_latest_main(
    root: Path,
    expected_main_sha: str,
    *,
    ref: str = "main",
    now: datetime | None = None,
    inspector: GitInspector | None = None,
) -> MainRevalidation:
    """Re-read the selected main ref and reject a changed integration base."""

    expected = validate_sha(expected_main_sha, "expected_main_sha")
    git = inspector or GitInspector(root)
    observed = git.resolve_commit(ref)
    if observed != expected:
        raise MainHeadStaleError("latest main head is no longer the expected head")
    return MainRevalidation(
        ref=ref,
        expected_main_sha=expected,
        observed_main_sha=observed,
        revalidated_at=_iso(now or datetime.now(timezone.utc)),
    )


def _normalize_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise AdoptionValidationError("changed_path_invalid")
    path = value.replace("\\", "/").strip("/")
    if not path or not _RELATIVE_PATH_RE.fullmatch(path):
        raise AdoptionValidationError("changed_path_invalid")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AdoptionValidationError("changed_path_invalid")
    return "/".join(parts)


def protected_path_category(path: str) -> ConflictCategory | None:
    """Classify a path that cannot be ambiguously adopted from Candidate."""

    normalized = _normalize_relative_path(path).casefold()
    parts = normalized.split("/")
    if (
        normalized == "protocol.md"
        or normalized.startswith("protocol/")
        or normalized == "worker/protocol_core.py"
    ):
        return ConflictCategory.PROTOCOL_AUTHORITY
    if parts and parts[0] == "projects":
        return ConflictCategory.PROJECT_CONTROL_PLANE
    if normalized.startswith("worker/runtime/") or normalized in {
        "worker/runtime",
        "worker/config.local.json",
    }:
        return ConflictCategory.PROTECTED_PATH
    if normalized in {
        "worker/windows_worker_launcher.py",
        "worker/bridge_worker_hardened.py",
        "worker/bridge_worker.py",
        "worker/codex_lifecycle.py",
    }:
        return ConflictCategory.LAUNCHER_SAFETY
    if normalized in {
        "worker/recovery_journal.py",
        "worker/recovery_resolution.py",
        "worker/vnext_runtime/services/recovery.py",
    }:
        return ConflictCategory.RECOVERY_IDENTITY
    if any(part in {".secrets", "secrets", "local-secrets", "local_only", "local-only"} for part in parts):
        return ConflictCategory.PROTECTED_PATH
    basename = parts[-1]
    if (
        basename in {"credentials.json", "credentials.ini", "secrets.json", "secrets.yaml", "secrets.yml", "token.json", "token.txt"}
        or (basename.startswith(".env") and basename not in {".env.example", ".env.sample"})
        or basename.endswith((".pem", ".key", ".p12", ".pfx", ".secret", ".secrets", ".local.json", ".local.yaml", ".local.yml"))
    ):
        return ConflictCategory.PROTECTED_PATH
    return None


class ReconciliationResolution(str, Enum):
    """Auditable per-path outcomes for an explicit reconciliation policy."""

    SHARED_BASELINE = "shared_baseline_tree"
    STABLE_ONLY = "stable_only_after_merge_base"
    CANDIDATE_ONLY = "candidate_only_forward"
    IDENTICAL_TREE = "identical_tip_tree"
    CLEAN_THREE_WAY = "clean_three_way"
    DOCUMENTATION_SECTION_UNION = "documentation_section_union"
    DOCUMENTATION_LATEST_MAIN = "documentation_latest_main_superset"
    BOUNDARY_FORWARD = "boundary_source_forward"


def _optional_blob_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _BLOB_SHA_RE.fullmatch(value.strip()) is None:
        raise AdoptionValidationError(f"{label}_invalid")
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """One path-level decision made by a named reconciliation rule."""

    path: str
    resolution: ReconciliationResolution
    rule_id: str
    reason: str
    category: ConflictCategory | None = None
    merge_base_blob_sha: str | None = None
    candidate_blob_sha: str | None = None
    latest_main_blob_sha: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_relative_path(self.path))
        if not isinstance(self.resolution, ReconciliationResolution):
            object.__setattr__(self, "resolution", ReconciliationResolution(self.resolution))
        object.__setattr__(self, "rule_id", _safe_token(self.rule_id, "reconciliation_rule"))
        object.__setattr__(self, "reason", _safe_token(self.reason, "reconciliation_reason"))
        if self.category is not None and not isinstance(self.category, ConflictCategory):
            object.__setattr__(self, "category", ConflictCategory(self.category))
        for name in ("merge_base_blob_sha", "candidate_blob_sha", "latest_main_blob_sha"):
            object.__setattr__(self, name, _optional_blob_sha(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    """Explicit allowlist for the Phase-8.5 source reconciliation boundary.

    The policy never permits an effective ``projects/**``, runtime-local,
    secret, or unknown protected-path overwrite.  Protocol-v2 paths are
    admitted only through the four named clean-merge paths below, an exact
    no-op tip, or a one-sided proof that includes untouched main history.  All
    other Protocol/kernel conflicts remain fail-closed.  Launcher/recovery
    source changes are allowed only when they are one-sided after the real
    merge base and pass the narrow source-contract proof in
    ``plan_reconciliation``.
    """

    policy_id: str = "phase85.explicit.v1"
    clean_three_way_paths: tuple[str, ...] = (
        "docs/ROADMAP.md",
        "docs/SELF_MAINTENANCE_BOOTSTRAP.md",
        "worker/README.md",
    )
    documentation_section_union_paths: tuple[str, ...] = ()
    documentation_latest_main_paths: tuple[str, ...] = (
        "policies/supervisor.md",
    )
    protocol_kernel_clean_three_way_paths: tuple[str, ...] = (
        "PROTOCOL.md",
        "protocol/v2/README.md",
        "protocol/v2/command.schema.json",
      "protocol/v2/report.schema.json",
    )
    boundary_forward_paths: tuple[str, ...] = (
        "worker/bridge_worker.py",
        "worker/bridge_worker_hardened.py",
        "worker/windows_worker_launcher.py",
        "worker/vnext_runtime/services/recovery.py",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _safe_token(self.policy_id, "policy_id"))
        names = (
            "clean_three_way_paths",
            "documentation_section_union_paths",
            "documentation_latest_main_paths",
            "protocol_kernel_clean_three_way_paths",
            "boundary_forward_paths",
        )
        for name in names:
            values = tuple(sorted({_normalize_relative_path(path) for path in getattr(self, name)}))
            object.__setattr__(self, name, values)
        known_clean = {
            "docs/ROADMAP.md",
            "docs/SELF_MAINTENANCE_BOOTSTRAP.md",
            "worker/README.md",
        }
        known_section_union: set[str] = set()
        known_latest_main = {"policies/supervisor.md"}
        known_protocol_kernel = {
            "PROTOCOL.md",
            "protocol/v2/README.md",
            "protocol/v2/command.schema.json",
            "protocol/v2/report.schema.json",
        }
        if set(self.clean_three_way_paths) - known_clean:
            raise AdoptionValidationError("reconciliation_policy_clean_path_unknown")
        if set(self.documentation_section_union_paths) - known_section_union:
            raise AdoptionValidationError("reconciliation_policy_section_path_unknown")
        if set(self.documentation_latest_main_paths) - known_latest_main:
            raise AdoptionValidationError("reconciliation_policy_latest_main_path_unknown")
        if set(self.protocol_kernel_clean_three_way_paths) - known_protocol_kernel:
            raise AdoptionValidationError("reconciliation_policy_protocol_path_unknown")
        for path in (
            *self.clean_three_way_paths,
            *self.documentation_section_union_paths,
            *self.documentation_latest_main_paths,
        ):
            if protected_path_category(path) is not None:
                raise AdoptionValidationError("reconciliation_policy_protected_document_path")
        for path in self.protocol_kernel_clean_three_way_paths:
            if protected_path_category(path) is not ConflictCategory.PROTOCOL_AUTHORITY:
                raise AdoptionValidationError("reconciliation_policy_protocol_path_invalid")
        for path in self.boundary_forward_paths:
            category = protected_path_category(path)
            if category not in {ConflictCategory.LAUNCHER_SAFETY, ConflictCategory.RECOVERY_IDENTITY}:
                raise AdoptionValidationError("reconciliation_policy_boundary_path_invalid")

    @classmethod
    def phase_85(cls) -> "ReconciliationPolicy":
        """Return the reviewed, narrow Phase-8.5 policy."""

        return cls()


def _markdown_headings(content: bytes, label: str) -> tuple[str, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdoptionValidationError(f"{label}_not_utf8") from exc
    return tuple(
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
    )


def _documentation_latest_main_proof(path: str, candidate: bytes, main: bytes) -> bool:
    """Prove that latest Stable contains the candidate's required contract."""

    candidate_text = candidate.decode("utf-8")
    main_text = main.decode("utf-8")
    candidate_lower = candidate_text.casefold()
    main_lower = main_text.casefold()
    required = {
        "protocol v2",
        "max_plans_per_pass",
        "max_ready_buffer",
        "at most one",
        "worker",
    }
    # Keep the check phrase-based: the latest policy may reword explanatory
    # prose, but it must retain every normative contract phrase used by the
    # accepted Candidate.
    if any(token in candidate_lower and token not in main_lower for token in required):
        return False
    required_by_path = {
        "policies/supervisor.md": ("portfolio scheduling", "required read set"),
    }
    if any(marker not in main_lower for marker in required_by_path.get(path, ())):
        return False
    return True


def _documentation_section_union_proof(
    base: bytes,
    candidate: bytes,
    main: bytes,
) -> bool:
    """No public document has special section-union merge authority."""

    return False


def _boundary_source_proof(path: str, content: bytes) -> bool:
    """Apply a small immutable source-contract proof to protected code."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lowered = text.casefold()
    if path == "worker/bridge_worker.py":
        return all(
            marker in text
            for marker in ("class WorkerCoordinator", "RunScope", "RecoveryCoordinator", "HostResourceArbiter")
        )
    if path == "worker/bridge_worker_hardened.py":
        return all(
            marker in text
            for marker in ("WorkerCoordinator", "begin_draining", "WORKER_RESTART_CODE", "worker_health")
        )
    if path == "worker/windows_worker_launcher.py":
        return all(
            marker in text
            for marker in ("create_lifetime_scope", "create_handoff_controller", "WORKER_RESTART_CODE")
        )
    if path == "worker/vnext_runtime/services/recovery.py":
        forbidden_imports = (
            "import executor",
            "from executor",
            "import codex_lifecycle",
            "from codex_lifecycle",
            "vnext_runtime.providers",
        )
        if any(marker in lowered for marker in forbidden_imports):
            return False
        return all(marker in text for marker in ("class RecoveryIdentity", "class RecoveryCoordinator", "automatic_rerun"))
    return False


@dataclass(frozen=True, slots=True)
class ReconciliationConflict:
    """One bounded reason a Candidate tree cannot be adopted automatically."""

    path: str
    category: ConflictCategory
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_relative_path(self.path))
        if not isinstance(self.category, ConflictCategory):
            object.__setattr__(self, "category", ConflictCategory(self.category))
        object.__setattr__(self, "reason", _safe_token(self.reason, "conflict_reason"))


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """A non-mutating, history-preserving plan for later controlled integration."""

    bootstrap_base_sha: str
    previous_stable_sha: str
    accepted_candidate_sha: str
    latest_main_sha: str
    candidate_changed_paths: tuple[str, ...]
    live_changed_paths: tuple[str, ...]
    preserve_live_history: bool = True
    force_operations_allowed: bool = False
    action: str = "merge_candidate_on_latest_main_without_reset"
    main_revalidated_at: str | None = None
    merge_base_sha: str | None = None
    candidate_delta_paths: tuple[str, ...] = ()
    live_delta_paths: tuple[str, ...] = ()
    decisions: tuple[ReconciliationDecision, ...] = ()
    policy_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("bootstrap_base_sha", "previous_stable_sha", "accepted_candidate_sha", "latest_main_sha"):
            object.__setattr__(self, name, validate_sha(getattr(self, name), name))
        normalized_candidate = tuple(sorted({_normalize_relative_path(path) for path in self.candidate_changed_paths}))
        normalized_live = tuple(sorted({_normalize_relative_path(path) for path in self.live_changed_paths}))
        object.__setattr__(self, "candidate_changed_paths", normalized_candidate)
        object.__setattr__(self, "live_changed_paths", normalized_live)
        if not isinstance(self.preserve_live_history, bool) or not self.preserve_live_history:
            raise AdoptionValidationError("live_history_preservation_required")
        if not isinstance(self.force_operations_allowed, bool) or self.force_operations_allowed:
            raise AdoptionValidationError("force_operations_forbidden")
        object.__setattr__(self, "action", _safe_token(self.action, "reconciliation_action"))
        if self.main_revalidated_at is not None:
            _parse_time(self.main_revalidated_at, "main_revalidated_at")
        if self.merge_base_sha is not None:
            object.__setattr__(self, "merge_base_sha", validate_sha(self.merge_base_sha, "merge_base_sha"))
        for name in ("candidate_delta_paths", "live_delta_paths"):
            normalized = tuple(sorted({_normalize_relative_path(path) for path in getattr(self, name)}))
            object.__setattr__(self, name, normalized)
        normalized_decisions = tuple(sorted(self.decisions, key=lambda item: item.path))
        if any(not isinstance(item, ReconciliationDecision) for item in normalized_decisions):
            raise AdoptionValidationError("reconciliation_decision_invalid")
        object.__setattr__(self, "decisions", normalized_decisions)
        if self.policy_id is not None:
            object.__setattr__(self, "policy_id", _safe_token(self.policy_id, "reconciliation_policy_id"))

    @property
    def mechanically_reconciled(self) -> bool:
        """Return whether every recorded Candidate path has a safe rule."""

        return bool(self.decisions) and all(
            decision.resolution is not None for decision in self.decisions
        )


def plan_reconciliation(
    candidate: CandidateFreeze,
    main: MainRevalidation,
    *,
    candidate_changed_paths: Sequence[str],
    main_changed_paths: Sequence[str],
    previous_stable_sha: str | None = None,
    repository: GitInspector | None = None,
    policy: ReconciliationPolicy | None = None,
    merge_base_sha: str | None = None,
) -> ReconciliationPlan:
    """Build a conservative plan that keeps all intervening main history.

    With no repository/policy arguments this retains the original conservative
    API and rejects every source overlap.  The explicit Phase-8.5 path uses the
    real merge base and a named allowlist, so a fixed bootstrap comparison can
    no longer masquerade as a live semantic conflict.
    """

    if not isinstance(candidate, CandidateFreeze) or not isinstance(main, MainRevalidation):
        raise AdoptionValidationError("reconciliation_identity_invalid")
    previous = validate_sha(previous_stable_sha or main.latest_main_sha, "previous_stable_sha")
    candidate_paths = tuple(sorted({_normalize_relative_path(path) for path in candidate_changed_paths}))
    live_paths = tuple(sorted({_normalize_relative_path(path) for path in main_changed_paths}))
    if (repository is None) != (policy is None):
        raise AdoptionValidationError("reconciliation_policy_repository_pair_required")
    if repository is not None and policy is not None:
        return _plan_explicit_reconciliation(
            candidate,
            main,
            candidate_paths=candidate_paths,
            live_paths=live_paths,
            previous_stable_sha=previous,
            repository=repository,
            policy=policy,
            merge_base_sha=merge_base_sha,
        )
    live_set = set(live_paths)
    conflicts: list[ReconciliationConflict] = []
    for path in candidate_paths:
        category = protected_path_category(path)
        if category is not None:
            conflicts.append(
                ReconciliationConflict(
                    path,
                    category,
                    "candidate_change_touches_adoption_boundary",
                )
            )
        elif path in live_set:
            conflicts.append(
                ReconciliationConflict(
                    path,
                    ConflictCategory.SOURCE_OVERLAP,
                    "candidate_and_live_main_both_changed_path",
                )
            )
    if conflicts:
        raise ReconciliationConflictError(tuple(sorted(conflicts, key=lambda item: (item.path, item.category.value))))
    return ReconciliationPlan(
        bootstrap_base_sha=candidate.bootstrap_base_sha,
        previous_stable_sha=previous,
        accepted_candidate_sha=candidate.accepted_candidate_sha,
        latest_main_sha=main.latest_main_sha,
        candidate_changed_paths=candidate_paths,
        live_changed_paths=live_paths,
        main_revalidated_at=main.revalidated_at,
    )


def _decision(
    *,
    path: str,
    resolution: ReconciliationResolution,
    rule_id: str,
    reason: str,
    category: ConflictCategory | None,
    repository: GitInspector,
    merge_base: str,
    candidate_sha: str,
    main_sha: str,
) -> ReconciliationDecision:
    return ReconciliationDecision(
        path=path,
        resolution=resolution,
        rule_id=rule_id,
        reason=reason,
        category=category,
        merge_base_blob_sha=repository.blob_sha(merge_base, path),
        candidate_blob_sha=repository.blob_sha(candidate_sha, path),
        latest_main_blob_sha=repository.blob_sha(main_sha, path),
    )


def _plan_explicit_reconciliation(
    candidate: CandidateFreeze,
    main: MainRevalidation,
    *,
    candidate_paths: tuple[str, ...],
    live_paths: tuple[str, ...],
    previous_stable_sha: str,
    repository: GitInspector,
    policy: ReconciliationPolicy,
    merge_base_sha: str | None,
) -> ReconciliationPlan:
    """Plan an explicit, proof-carrying integration against latest ``main``."""

    if repository.root != candidate.candidate_root:
        raise AdoptionValidationError("reconciliation_inspector_root_mismatch")
    if repository.resolve_commit(main.ref) != main.latest_main_sha:
        raise MainHeadStaleError("latest main moved before reconciliation")
    if not repository.is_ancestor(candidate.bootstrap_base_sha, main.latest_main_sha):
        raise ReconciliationConflictError(
            (
                ReconciliationConflict(
                    "main",
                    ConflictCategory.SOURCE_OVERLAP,
                    "latest_main_not_derived_from_bootstrap",
                ),
            )
        )
    calculated_merge_base = repository.merge_base(
        candidate.accepted_candidate_sha,
        main.latest_main_sha,
    )
    if merge_base_sha is not None and validate_sha(merge_base_sha, "merge_base_sha") != calculated_merge_base:
        raise MainHeadStaleError("merge base changed during reconciliation")
    merge_base = calculated_merge_base
    inspected_candidate_paths = repository.changed_paths(
        candidate.bootstrap_base_sha,
        candidate.accepted_candidate_sha,
    )
    inspected_live_paths = repository.changed_paths(
        candidate.bootstrap_base_sha,
        main.latest_main_sha,
    )
    if set(inspected_candidate_paths) != set(candidate_paths):
        raise AdoptionValidationError("candidate_changed_paths_not_current")
    if set(inspected_live_paths) != set(live_paths):
        raise AdoptionValidationError("main_changed_paths_not_current")
    candidate_delta = set(repository.changed_paths(merge_base, candidate.accepted_candidate_sha))
    live_delta = set(repository.changed_paths(merge_base, main.latest_main_sha))
    conflicts: list[ReconciliationConflict] = []
    decisions: list[ReconciliationDecision] = []
    for path in candidate_paths:
        category = protected_path_category(path)
        merge_blob = repository.blob_sha(merge_base, path)
        candidate_blob = repository.blob_sha(candidate.accepted_candidate_sha, path)
        main_blob = repository.blob_sha(main.latest_main_sha, path)
        if path not in candidate_delta:
            if candidate_blob == merge_blob == main_blob:
                decisions.append(
                    _decision(
                        path=path,
                        resolution=ReconciliationResolution.SHARED_BASELINE,
                        rule_id="phase85.shared_merge_base_tree.v1",
                        reason="candidate_change_already_in_common_merge_base",
                        category=category,
                        repository=repository,
                        merge_base=merge_base,
                        candidate_sha=candidate.accepted_candidate_sha,
                        main_sha=main.latest_main_sha,
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.STABLE_ONLY,
                    rule_id="phase85.stable_latest_tree.v1",
                    reason="candidate_tree_unchanged_after_merge_base",
                    category=category,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if path not in live_delta:
            if category is not None:
                if (
                    category is ConflictCategory.PROTOCOL_AUTHORITY
                    and path in policy.protocol_kernel_clean_three_way_paths
                    and not repository.path_touched_since(
                        merge_base,
                        main.latest_main_sha,
                        path,
                    )
                ):
                    if repository.three_way_merge_conflict(
                        merge_base,
                        candidate.accepted_candidate_sha,
                        main.latest_main_sha,
                        path,
                    ):
                        conflicts.append(
                            ReconciliationConflict(
                                path,
                                category,
                                "protected_protocol_three_way_conflict",
                            )
                        )
                        continue
                    decisions.append(
                        _decision(
                            path=path,
                            resolution=ReconciliationResolution.CLEAN_THREE_WAY,
                            rule_id="phase85.protected_protocol_clean_three_way.v1",
                            reason="main_history_untouched_clean_protocol_three_way",
                            category=category,
                            repository=repository,
                            merge_base=merge_base,
                            candidate_sha=candidate.accepted_candidate_sha,
                            main_sha=main.latest_main_sha,
                        )
                    )
                    continue
                if path not in policy.boundary_forward_paths or not _boundary_source_proof(
                    path,
                    repository.blob_bytes(candidate.accepted_candidate_sha, path) or b"",
                ):
                    conflicts.append(
                        ReconciliationConflict(
                            path,
                            category,
                            "candidate_only_protected_boundary_requires_explicit_proof",
                        )
                    )
                    continue
                decisions.append(
                    _decision(
                        path=path,
                        resolution=ReconciliationResolution.BOUNDARY_FORWARD,
                        rule_id="phase85.boundary_candidate_only_forward.v1",
                        reason="protected_boundary_one_sided_and_contract_proven",
                        category=category,
                        repository=repository,
                        merge_base=merge_base,
                        candidate_sha=candidate.accepted_candidate_sha,
                        main_sha=main.latest_main_sha,
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.CANDIDATE_ONLY,
                    rule_id="phase85.candidate_only_forward.v1",
                    reason="candidate_only_after_merge_base",
                    category=None,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if candidate_blob == main_blob and category not in {
            ConflictCategory.PROJECT_CONTROL_PLANE,
            ConflictCategory.PROTECTED_PATH,
        }:
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.IDENTICAL_TREE,
                    rule_id="phase85.identical_tip_tree.v1",
                    reason="candidate_and_latest_main_blobs_identical",
                    category=category,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if path in policy.protocol_kernel_clean_three_way_paths:
            if repository.three_way_merge_conflict(
                merge_base,
                candidate.accepted_candidate_sha,
                main.latest_main_sha,
                path,
            ):
                conflicts.append(
                    ReconciliationConflict(
                        path,
                        ConflictCategory.PROTOCOL_AUTHORITY,
                        "protected_protocol_three_way_conflict",
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.CLEAN_THREE_WAY,
                    rule_id="phase85.protected_protocol_clean_three_way.v1",
                    reason="git_merge_file_proved_clean_protocol_three_way",
                    category=category,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if category is not None:
            conflicts.append(
                ReconciliationConflict(
                    path,
                    category,
                    "protected_path_changed_on_both_sides",
                )
            )
            continue
        if path in policy.clean_three_way_paths:
            if repository.three_way_merge_conflict(
                merge_base,
                candidate.accepted_candidate_sha,
                main.latest_main_sha,
                path,
            ):
                conflicts.append(
                    ReconciliationConflict(
                        path,
                        ConflictCategory.SOURCE_OVERLAP,
                        "approved_three_way_rule_found_conflict",
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.CLEAN_THREE_WAY,
                    rule_id="phase85.documentation_clean_three_way.v1",
                    reason="git_merge_file_proved_clean_three_way",
                    category=None,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if path in policy.documentation_section_union_paths:
            base_bytes = repository.blob_bytes(merge_base, path)
            candidate_bytes = repository.blob_bytes(candidate.accepted_candidate_sha, path)
            main_bytes = repository.blob_bytes(main.latest_main_sha, path)
            if (
                base_bytes is None
                or candidate_bytes is None
                or main_bytes is None
                or not _documentation_section_union_proof(base_bytes, candidate_bytes, main_bytes)
            ):
                conflicts.append(
                    ReconciliationConflict(
                        path,
                        ConflictCategory.SOURCE_OVERLAP,
                        "documentation_section_union_proof_failed",
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.DOCUMENTATION_SECTION_UNION,
                    rule_id="phase85.documentation_section_union.v1",
                    reason="latest_main_plus_candidate_only_phase_section",
                    category=None,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        if path in policy.documentation_latest_main_paths:
            candidate_bytes = repository.blob_bytes(candidate.accepted_candidate_sha, path)
            main_bytes = repository.blob_bytes(main.latest_main_sha, path)
            if (
                candidate_bytes is None
                or main_bytes is None
                or not _documentation_latest_main_proof(path, candidate_bytes, main_bytes)
            ):
                conflicts.append(
                    ReconciliationConflict(
                        path,
                        ConflictCategory.SOURCE_OVERLAP,
                        "latest_main_documentation_superset_proof_failed",
                    )
                )
                continue
            decisions.append(
                _decision(
                    path=path,
                    resolution=ReconciliationResolution.DOCUMENTATION_LATEST_MAIN,
                    rule_id="phase85.documentation_latest_main_superset.v1",
                    reason="latest_main_retains_candidate_normative_contract",
                    category=None,
                    repository=repository,
                    merge_base=merge_base,
                    candidate_sha=candidate.accepted_candidate_sha,
                    main_sha=main.latest_main_sha,
                )
            )
            continue
        conflicts.append(
            ReconciliationConflict(
                path,
                ConflictCategory.SOURCE_OVERLAP,
                "unapproved_source_overlap",
            )
        )
    if conflicts:
        raise ReconciliationConflictError(
            tuple(sorted(conflicts, key=lambda item: (item.path, item.category.value)))
        )
    if repository.resolve_commit(main.ref) != main.latest_main_sha:
        raise MainHeadStaleError("latest main moved during reconciliation")
    return ReconciliationPlan(
        bootstrap_base_sha=candidate.bootstrap_base_sha,
        previous_stable_sha=previous_stable_sha,
        accepted_candidate_sha=candidate.accepted_candidate_sha,
        latest_main_sha=main.latest_main_sha,
        candidate_changed_paths=candidate_paths,
        live_changed_paths=live_paths,
        main_revalidated_at=main.revalidated_at,
        merge_base_sha=merge_base,
        candidate_delta_paths=tuple(sorted(candidate_delta)),
        live_delta_paths=tuple(sorted(live_delta)),
        decisions=tuple(decisions),
        policy_id=policy.policy_id,
    )


@dataclass(frozen=True, slots=True)
class PromotionPlan:
    """A non-force promotion precondition, not a command to move a ref."""

    target_branch: str
    expected_integration_head_sha: str
    previous_stable_sha: str
    accepted_candidate_sha: str
    reconciled_adoption_sha: str
    revalidated_at: str
    force_push_allowed: bool = False
    action: str = "fast_forward_only_outer_controller"

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_branch", _validate_main_branch(self.target_branch, "target_branch"))
        for name in ("expected_integration_head_sha", "previous_stable_sha", "accepted_candidate_sha", "reconciled_adoption_sha"):
            object.__setattr__(self, name, validate_sha(getattr(self, name), name))
        _parse_time(self.revalidated_at, "revalidated_at")
        if self.force_push_allowed:
            raise AdoptionValidationError("force_push_forbidden")
        object.__setattr__(self, "action", _safe_token(self.action, "promotion_action"))


def build_promotion_plan(
    reconciliation: ReconciliationPlan,
    reconciled_adoption_sha: str,
    *,
    revalidated_at: str | None = None,
    target_branch: str = "main",
) -> PromotionPlan:
    """Bind promotion to the exact main head that was freshly revalidated."""

    if not isinstance(reconciliation, ReconciliationPlan):
        raise PromotionRejectedError("reconciliation_plan_invalid")
    return PromotionPlan(
        target_branch=target_branch,
        expected_integration_head_sha=reconciliation.latest_main_sha,
        previous_stable_sha=reconciliation.previous_stable_sha,
        accepted_candidate_sha=reconciliation.accepted_candidate_sha,
        reconciled_adoption_sha=validate_sha(reconciled_adoption_sha, "reconciled_adoption_sha"),
        revalidated_at=(
            revalidated_at
            or reconciliation.main_revalidated_at
            or _iso(datetime.now(timezone.utc))
        ),
    )


class PromotionGuard:
    """Validate a plan immediately before an outer controller may act."""

    def validate(
        self,
        plan: PromotionPlan,
        *,
        current_integration_head_sha: str,
        adoption_is_descendant: bool,
        force: bool = False,
    ) -> None:
        """Reject stale, non-descendant, or force-style promotion attempts."""

        if not isinstance(plan, PromotionPlan):
            raise PromotionRejectedError("promotion_plan_invalid")
        if force or plan.force_push_allowed:
            raise PromotionRejectedError("force_promotion_unavailable")
        current = validate_sha(current_integration_head_sha, "current_integration_head_sha")
        if current != plan.expected_integration_head_sha:
            raise PromotionRejectedError("integration_head_stale")
        if not adoption_is_descendant:
            raise PromotionRejectedError("promotion_not_fast_forwardable")

    def validate_with_git(
        self,
        plan: PromotionPlan,
        *,
        repository: GitInspector,
        current_integration_ref: str = "main",
        force: bool = False,
    ) -> None:
        """Perform the same guard using a read-only Git inspector."""

        current = repository.resolve_commit(current_integration_ref)
        descendant = repository.is_ancestor(current, plan.reconciled_adoption_sha)
        self.validate(
            plan,
            current_integration_head_sha=current,
            adoption_is_descendant=descendant,
            force=force,
        )


@dataclass(frozen=True, slots=True)
class RollbackIdentity:
    """Known-good outer-controller identity independent of the adopted Worker."""

    known_good_sha: str
    branch: str = "main"
    controller: str = "outer_launcher"

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_good_sha", validate_sha(self.known_good_sha, "known_good_sha"))
        object.__setattr__(self, "branch", _validate_main_branch(self.branch, "rollback_branch"))
        object.__setattr__(self, "controller", _safe_token(self.controller, "rollback_controller"))

    def to_dict(self) -> dict[str, object]:
        """Return the bounded serializable identity."""

        return {
            "known_good_sha": self.known_good_sha,
            "branch": self.branch,
            "controller": self.controller,
        }


@dataclass(frozen=True, slots=True)
class ForwardRollbackPlan:
    """A new forward revert operation that preserves control-plane history."""

    current_adoption_sha: str
    known_good_sha: str
    integration_parent_sha: str
    target_branch: str
    operation: str = "create_forward_revert_commit"
    preserves_control_plane_history: bool = True
    depends_on_adopted_worker: bool = False
    uses_reset: bool = False
    uses_force_push: bool = False
    no_blind_rerun: bool = True

    def __post_init__(self) -> None:
        for name in ("current_adoption_sha", "known_good_sha", "integration_parent_sha"):
            object.__setattr__(self, name, validate_sha(getattr(self, name), name))
        object.__setattr__(self, "target_branch", _validate_main_branch(self.target_branch, "rollback_branch"))
        object.__setattr__(self, "operation", _safe_token(self.operation, "rollback_operation"))
        if (
            not self.preserves_control_plane_history
            or self.depends_on_adopted_worker
            or self.uses_reset
            or self.uses_force_push
            or not self.no_blind_rerun
        ):
            raise RollbackRejectedError("rollback_must_be_forward_and_outer_controlled")


def plan_forward_rollback(
    promotion: PromotionPlan,
    *,
    current_adoption_sha: str | None = None,
    adopted_head_is_ancestor: bool = False,
    stable_is_ancestor_of_integration: bool = True,
) -> ForwardRollbackPlan:
    """Construct rollback without reset, force-push, or adopted-Worker help.

    ``current_adoption_sha`` may be a later control-plane HEAD when the caller
    explicitly proves that the adopted HEAD is its ancestor.  That is how the
    outer controller preserves new history created after adoption.
    """

    if not isinstance(promotion, PromotionPlan):
        raise RollbackRejectedError("promotion_plan_invalid")
    current = validate_sha(current_adoption_sha or promotion.reconciled_adoption_sha, "current_adoption_sha")
    if current != promotion.reconciled_adoption_sha and not adopted_head_is_ancestor:
        raise RollbackRejectedError("adopted_head_identity_changed")
    if not isinstance(adopted_head_is_ancestor, bool):
        raise RollbackRejectedError("adopted_head_ancestry_unverified")
    if not stable_is_ancestor_of_integration:
        raise RollbackRejectedError("known_good_history_not_verified")
    return ForwardRollbackPlan(
        current_adoption_sha=current,
        known_good_sha=promotion.previous_stable_sha,
        integration_parent_sha=promotion.expected_integration_head_sha,
        target_branch=promotion.target_branch,
    )


@dataclass(frozen=True, slots=True)
class AdoptionPolicy:
    """Explicit gates for later controlled adoption; defaults are disabled."""

    controlled_adoption_enabled: bool = False
    unattended_adoption_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.controlled_adoption_enabled, bool) or not isinstance(self.unattended_adoption_enabled, bool):
            raise AdoptionValidationError("adoption_policy_flags_invalid")

    @classmethod
    def from_mapping(cls, config: Mapping[str, object] | None) -> "AdoptionPolicy":
        """Parse only explicit boolean adoption flags, failing closed otherwise."""

        if config is None:
            return cls()
        raw = config.get("adoption", {})
        if not isinstance(raw, Mapping):
            raise AdoptionValidationError("adoption_config_invalid")
        allowed = {"controlled_adoption_enabled", "unattended_adoption_enabled"}
        if set(raw) - allowed:
            raise AdoptionValidationError("adoption_config_unsupported_fields")
        values: dict[str, bool] = {}
        for key in allowed:
            value = raw.get(key, False)
            if not isinstance(value, bool):
                raise AdoptionValidationError(f"{key}_invalid")
            values[key] = value
        return cls(**values)


class AdoptionGate:
    """Require explicit controlled mode before any future live adapter acts."""

    def __init__(self, policy: AdoptionPolicy | None = None) -> None:
        self.policy = policy or AdoptionPolicy()

    def authorize(self, mode: AdoptionMode, *, explicit_controlled_mode: bool = False) -> None:
        """Authorize a later adapter; this method itself performs no side effect."""

        try:
            selected = mode if isinstance(mode, AdoptionMode) else AdoptionMode(mode)
        except (TypeError, ValueError) as exc:
            raise AdoptionDisabledError("adoption_mode_invalid") from exc
        if not explicit_controlled_mode or not self.policy.controlled_adoption_enabled:
            raise AdoptionDisabledError("controlled_adoption_disabled")
        if selected is AdoptionMode.UNATTENDED and not self.policy.unattended_adoption_enabled:
            raise AdoptionDisabledError("unattended_adoption_disabled")

    def can_authorize(self, mode: AdoptionMode, *, explicit_controlled_mode: bool = False) -> bool:
        """Return a conservative boolean without raising to callers doing preflight."""

        try:
            self.authorize(mode, explicit_controlled_mode=explicit_controlled_mode)
        except AdoptionDisabledError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """Secret-free outer-controller observation for a post-exit-75 gate."""

    observed_at: str
    exit_code: int | None
    worker_started_at: str | None
    heartbeat_at: str | None
    launcher_alive: bool
    worker_healthy: bool
    protocol_ready: bool
    recovery_clear: bool

    def __post_init__(self) -> None:
        _parse_time(self.observed_at, "observed_at")
        if self.exit_code is not None and (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)):
            raise AdoptionValidationError("health_exit_code_invalid")
        if self.worker_started_at is not None:
            _parse_time(self.worker_started_at, "worker_started_at")
        if self.heartbeat_at is not None:
            _parse_time(self.heartbeat_at, "heartbeat_at")
        for name in ("launcher_alive", "worker_healthy", "protocol_ready", "recovery_clear"):
            if not isinstance(getattr(self, name), bool):
                raise AdoptionValidationError(f"{name}_invalid")


@dataclass(frozen=True, slots=True)
class HealthGateDecision:
    """Result of one health-gate evaluation; claims are allowed only at READY."""

    status: HealthGateStatus
    claims_allowed: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, HealthGateStatus):
            object.__setattr__(self, "status", HealthGateStatus(self.status))
        if self.claims_allowed != (self.status is HealthGateStatus.READY):
            raise AdoptionValidationError("health_claim_gate_inconsistent")
        normalized = tuple(_safe_token(reason, "health_reason") for reason in self.reasons)
        object.__setattr__(self, "reasons", normalized)


@dataclass(frozen=True, slots=True)
class HealthGateContract:
    """Launcher/outer-controller contract for exit-75 restart and probation."""

    restart_requested_at: str
    health_deadline: str
    probation_seconds: int
    health_criteria: tuple[str, ...]
    restart_exit_code: int = PROMOTION_RESTART_EXIT_CODE

    def __post_init__(self) -> None:
        requested = _parse_time(self.restart_requested_at, "restart_requested_at")
        deadline = _parse_time(self.health_deadline, "health_deadline")
        if deadline < requested:
            raise AdoptionValidationError("health_deadline_before_restart")
        if isinstance(self.probation_seconds, bool) or not isinstance(self.probation_seconds, int) or not 0 <= self.probation_seconds <= 86_400:
            raise AdoptionValidationError("probation_seconds_invalid")
        if self.restart_exit_code != PROMOTION_RESTART_EXIT_CODE:
            raise AdoptionValidationError("restart_exit_code_invalid")
        criteria = tuple(_safe_token(item, "health_criterion", limit=MAX_HEALTH_CRITERION_LENGTH) for item in self.health_criteria)
        if not criteria or len(criteria) > MAX_HEALTH_CRITERIA:
            raise AdoptionValidationError("health_criteria_invalid")
        if any(item not in _HEALTH_ATTRIBUTES for item in criteria):
            raise AdoptionValidationError("health_criterion_unsupported")
        object.__setattr__(self, "health_criteria", tuple(dict.fromkeys(criteria)))

    def evaluate(self, observation: HealthObservation) -> HealthGateDecision:
        """Keep claims blocked until restart, health and probation all pass."""

        if not isinstance(observation, HealthObservation):
            raise AdoptionValidationError("health_observation_invalid")
        observed = _parse_time(observation.observed_at, "observed_at")
        deadline = _parse_time(self.health_deadline, "health_deadline")
        requested = _parse_time(self.restart_requested_at, "restart_requested_at")
        if observed > deadline:
            return HealthGateDecision(HealthGateStatus.EXPIRED, False, ("health_deadline_expired",))
        if observation.exit_code not in (None, self.restart_exit_code):
            return HealthGateDecision(HealthGateStatus.FAILED, False, ("unexpected_restart_exit_code",))
        if observation.worker_started_at is None:
            return HealthGateDecision(HealthGateStatus.WAITING_FOR_RESTART, False, ("worker_restart_not_observed",))
        started = _parse_time(observation.worker_started_at, "worker_started_at")
        if started <= requested:
            return HealthGateDecision(HealthGateStatus.WAITING_FOR_RESTART, False, ("worker_start_precedes_restart",))
        if observation.heartbeat_at is None:
            return HealthGateDecision(HealthGateStatus.PROBATION, False, ("worker_heartbeat_missing",))
        heartbeat = _parse_time(observation.heartbeat_at, "heartbeat_at")
        if heartbeat < started:
            return HealthGateDecision(HealthGateStatus.FAILED, False, ("heartbeat_precedes_worker_start",))
        failed = tuple(
            name
            for name in self.health_criteria
            if not bool(getattr(observation, _HEALTH_ATTRIBUTES[name]))
        )
        if failed:
            return HealthGateDecision(HealthGateStatus.FAILED, False, failed)
        if observed < started + timedelta(seconds=self.probation_seconds):
            return HealthGateDecision(HealthGateStatus.PROBATION, False, ("probation_incomplete",))
        return HealthGateDecision(HealthGateStatus.READY, True, ("health_and_probation_verified",))


class AdmissionLease:
    """One local admission token released when its run/RunScope finishes."""

    def __init__(self, barrier: "AdmissionBarrier", admission_id: str) -> None:
        self._barrier = barrier
        self.admission_id = admission_id
        self._closed = False

    @property
    def active(self) -> bool:
        """Return whether this token still contributes to the drain count."""

        return not self._closed

    def close(self) -> bool:
        """Release exactly this admission token; other runs are untouched."""

        if self._closed:
            return False
        self._closed = True
        return self._barrier._release(self.admission_id, self)

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


class AdmissionBarrier:
    """Thread-safe local DRAINING barrier for claims and active RunScopes."""

    def __init__(self, *, max_active: int | None = None) -> None:
        if max_active is not None and (isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1):
            raise AdmissionBarrierError("max_active_invalid")
        self._condition = Condition(RLock())
        self._state = BarrierState.RUNNING
        self._max_active = max_active
        self._leases: dict[str, AdmissionLease] = {}

    @property
    def state(self) -> BarrierState:
        """Return the local lifecycle state."""

        with self._condition:
            return self._state

    @property
    def active_count(self) -> int:
        """Return the number of admitted runs not yet released."""

        with self._condition:
            return len(self._leases)

    @property
    def restart_ready(self) -> bool:
        """Return true only after DRAINING reaches zero active runs."""

        with self._condition:
            return self._state in {BarrierState.DRAINED, BarrierState.CLOSED} and not self._leases

    def active_ids(self) -> tuple[str, ...]:
        """Return bounded deterministic active admission identities."""

        with self._condition:
            return tuple(sorted(self._leases))

    def try_admit(self, admission_id: str) -> AdmissionLease | None:
        """Admit one run, or return none when draining/full/duplicated."""

        identifier = _safe_text(admission_id, "admission_id", limit=256)
        with self._condition:
            if self._state is not BarrierState.RUNNING:
                return None
            if identifier in self._leases:
                return None
            if self._max_active is not None and len(self._leases) >= self._max_active:
                return None
            lease = AdmissionLease(self, identifier)
            self._leases[identifier] = lease
            return lease

    def begin_draining(self, timeout_seconds: float | None = None) -> bool:
        """Stop new admissions and wait without terminating existing runs."""

        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds < 0
        ):
            raise AdmissionBarrierError("drain_timeout_invalid")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._condition:
            if self._state is BarrierState.CLOSED:
                return not self._leases
            if self._state is BarrierState.RUNNING:
                self._state = BarrierState.DRAINING
                self._condition.notify_all()
            while self._leases:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(timeout=remaining)
            self._state = BarrierState.DRAINED
            self._condition.notify_all()
            return True

    def wait_until_drained(self, timeout_seconds: float | None = None) -> bool:
        """Wait for a previously requested drain without reopening admission."""

        return self.begin_draining(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        """Close only after all active runs have released their own leases."""

        with self._condition:
            if self._leases:
                raise AdmissionBarrierError("cannot_close_with_active_runs")
            self._state = BarrierState.CLOSED
            self._condition.notify_all()

    def _release(self, admission_id: str, lease: AdmissionLease) -> bool:
        with self._condition:
            current = self._leases.get(admission_id)
            if current is not lease:
                return False
            del self._leases[admission_id]
            if self._state is BarrierState.DRAINING and not self._leases:
                self._state = BarrierState.DRAINED
            self._condition.notify_all()
            return True


@dataclass(frozen=True, slots=True)
class AdoptionAttemptEvidence:
    """Bounded secret-safe identity/health evidence for one attempt."""

    attempt_id: str
    recorded_at: str
    bootstrap_base_sha: str
    previous_stable_sha: str
    accepted_candidate_sha: str
    latest_main_sha: str
    reconciled_adoption_sha: str
    health_deadline: str
    health_criteria: tuple[str, ...]
    rollback_identity: RollbackIdentity
    mode: AdoptionMode
    status: str = "PLANNED"
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", _safe_token(self.attempt_id, "attempt_id"))
        _parse_time(self.recorded_at, "recorded_at")
        for name in (
            "bootstrap_base_sha",
            "previous_stable_sha",
            "accepted_candidate_sha",
            "latest_main_sha",
            "reconciled_adoption_sha",
        ):
            object.__setattr__(self, name, validate_sha(getattr(self, name), name))
        recorded = _parse_time(self.recorded_at, "recorded_at")
        deadline = _parse_time(self.health_deadline, "health_deadline")
        if deadline < recorded:
            raise AdoptionValidationError("health_deadline_before_evidence")
        criteria = tuple(_safe_token(item, "health_criterion", limit=MAX_HEALTH_CRITERION_LENGTH) for item in self.health_criteria)
        if not criteria or len(criteria) > MAX_HEALTH_CRITERIA:
            raise AdoptionValidationError("health_criteria_invalid")
        if any(item not in _HEALTH_ATTRIBUTES for item in criteria):
            raise AdoptionValidationError("health_criterion_unsupported")
        object.__setattr__(self, "health_criteria", tuple(dict.fromkeys(criteria)))
        if not isinstance(self.rollback_identity, RollbackIdentity):
            raise AdoptionValidationError("rollback_identity_invalid")
        if self.rollback_identity.known_good_sha != self.previous_stable_sha:
            raise AdoptionValidationError("rollback_identity_does_not_bind_previous_stable")
        if not isinstance(self.mode, AdoptionMode):
            try:
                object.__setattr__(self, "mode", AdoptionMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise AdoptionValidationError("adoption_mode_invalid") from exc
        object.__setattr__(self, "status", _safe_token(self.status, "evidence_status"))
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _safe_token(self.failure_reason, "failure_reason"),
            )

    def to_payload(self) -> dict[str, object]:
        """Return the exact fixed-field payload covered by the integrity hash."""

        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "recorded_at": self.recorded_at,
            "bootstrap_base_sha": self.bootstrap_base_sha,
            "previous_stable_sha": self.previous_stable_sha,
            "accepted_candidate_sha": self.accepted_candidate_sha,
            "latest_main_sha": self.latest_main_sha,
            "reconciled_adoption_sha": self.reconciled_adoption_sha,
            "health_deadline": self.health_deadline,
            "health_criteria": list(self.health_criteria),
            "rollback_identity": self.rollback_identity.to_dict(),
            "mode": self.mode.value,
            "status": self.status,
            "failure_reason": self.failure_reason,
        }

    def integrity_sha256(self) -> str:
        """Return the digest used by the durable evidence envelope."""

        return sha256(_canonical_json(self.to_payload())).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "AdoptionAttemptEvidence":
        """Validate a fixed evidence payload without accepting unknown fields."""

        required = {
            "schema_version",
            "attempt_id",
            "recorded_at",
            "bootstrap_base_sha",
            "previous_stable_sha",
            "accepted_candidate_sha",
            "latest_main_sha",
            "reconciled_adoption_sha",
            "health_deadline",
            "health_criteria",
            "rollback_identity",
            "mode",
        }
        optional = {"status", "failure_reason"}
        if (
            set(payload) - required - optional
            or not required.issubset(set(payload))
            or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        ):
            raise EvidenceIntegrityError("evidence_schema_invalid")
        criteria_raw = payload["health_criteria"]
        rollback_raw = payload["rollback_identity"]
        if not isinstance(criteria_raw, (list, tuple)) or not isinstance(rollback_raw, Mapping):
            raise EvidenceIntegrityError("evidence_shape_invalid")
        try:
            rollback = RollbackIdentity(
                known_good_sha=rollback_raw.get("known_good_sha"),
                branch=rollback_raw.get("branch", "main"),
                controller=rollback_raw.get("controller", "outer_launcher"),
            )
            return cls(
                attempt_id=payload["attempt_id"],
                recorded_at=payload["recorded_at"],
                bootstrap_base_sha=payload["bootstrap_base_sha"],
                previous_stable_sha=payload["previous_stable_sha"],
                accepted_candidate_sha=payload["accepted_candidate_sha"],
                latest_main_sha=payload["latest_main_sha"],
                reconciled_adoption_sha=payload["reconciled_adoption_sha"],
                health_deadline=payload["health_deadline"],
                health_criteria=tuple(criteria_raw),
                rollback_identity=rollback,
                mode=payload["mode"],
                status=payload.get("status", "PLANNED"),
                failure_reason=payload.get("failure_reason"),
            )
        except (AdoptionValidationError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("evidence_payload_invalid") from exc


class AdoptionEvidenceStore:
    """Atomically persist and verify local adoption evidence only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def write(self, evidence: AdoptionAttemptEvidence) -> None:
        """Write one bounded integrity envelope with replace-and-fsync semantics."""

        if not isinstance(evidence, AdoptionAttemptEvidence):
            raise EvidenceIntegrityError("evidence_object_invalid")
        payload = evidence.to_payload()
        envelope: dict[str, object] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "payload": payload,
            "payload_sha256": evidence.integrity_sha256(),
        }
        encoded = _canonical_json(envelope) + b"\n"
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise EvidenceIntegrityError("evidence_too_large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def read(self) -> AdoptionAttemptEvidence:
        """Read, size-bound and verify the exact evidence envelope."""

        try:
            if self.path.stat().st_size > MAX_EVIDENCE_BYTES:
                raise EvidenceIntegrityError("evidence_too_large")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except EvidenceIntegrityError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceIntegrityError("evidence_unreadable") from exc
        if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "payload", "payload_sha256"}:
            raise EvidenceIntegrityError("evidence_envelope_invalid")
        if raw.get("schema_version") != EVIDENCE_SCHEMA_VERSION or not isinstance(raw.get("payload"), Mapping):
            raise EvidenceIntegrityError("evidence_envelope_invalid")
        payload = raw["payload"]
        expected = raw.get("payload_sha256")
        if not isinstance(expected, str) or _SHA_RE.fullmatch(expected) is None and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EvidenceIntegrityError("evidence_digest_invalid")
        actual = sha256(_canonical_json(payload)).hexdigest()
        if actual != expected:
            raise EvidenceIntegrityError("evidence_digest_mismatch")
        return AdoptionAttemptEvidence.from_payload(payload)

    load = read


__all__ = [
    "AdoptionAttemptEvidence",
    "AdoptionDisabledError",
    "AdoptionError",
    "AdoptionEvidenceStore",
    "AdoptionGate",
    "AdoptionMode",
    "AdoptionPolicy",
    "AdoptionValidationError",
    "AdmissionBarrier",
    "AdmissionBarrierError",
    "AdmissionLease",
    "BarrierState",
    "CandidateFreeze",
    "CandidateValidationError",
    "ConflictCategory",
    "EvidenceIntegrityError",
    "ForwardRollbackPlan",
    "GitInspector",
    "HealthGateContract",
    "HealthGateDecision",
    "HealthGateStatus",
    "HealthObservation",
    "MainHeadStaleError",
    "MainRevalidation",
    "PromotionGuard",
    "PromotionPlan",
    "PromotionRejectedError",
    "ReconciliationConflict",
    "ReconciliationConflictError",
    "ReconciliationDecision",
    "ReconciliationPlan",
    "ReconciliationPolicy",
    "ReconciliationResolution",
    "RepositorySnapshot",
    "RollbackIdentity",
    "RollbackRejectedError",
    "build_promotion_plan",
    "freeze_candidate",
    "plan_forward_rollback",
    "plan_reconciliation",
    "protected_path_category",
    "revalidate_latest_main",
    "validate_branch",
    "validate_sha",
]
