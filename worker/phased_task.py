#!/usr/bin/env python3
"""Strict phased-task parsing and per-execution checkpoint runtime.

This module is deliberately a small, standard-library-only execution-memory
component.  It does not know Protocol v2 state transitions, leases, GitHub,
Codex, project roadmaps, or workload sizing.  The Worker supplies the already
claimed command text and decides when a successful canonical publication makes
safe compaction possible.

The command declaration is an optional body format.  A command without the
declaration remains an ordinary command and is not given any phased runtime
files.
"""

from __future__ import annotations

import state_roots

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
FINAL_ACCEPTANCE = "FINAL_ACCEPTANCE"
PHASE_IDS = ("A", "B", "C", "D")

DECLARATION_RE = re.compile(
    r"^\s*<!--\s*bridge-phased-task\s*:\s*(.*?)\s*-->\s*$"
)
DECLARATION_HINT_RE = re.compile(r"<!--\s*bridge-phased-task\b", re.IGNORECASE)
MARKER_RE = re.compile(
    r"^\s*<!--\s*(bridge-global|bridge-phase:([A-D])|bridge-final-acceptance)"
    r"\s*:\s*(start|end)\s*-->\s*$"
)
MARKER_HINT_RE = re.compile(
    r"<!--\s*bridge-(?:global|phase|final-acceptance)\b", re.IGNORECASE
)
COMMAND_METADATA_RE = re.compile(r"^\s*<!--\s*bridge-command\s*:.*-->\s*$")
# The only human-readable content allowed in the phased header envelope.  The
# command id is deliberately recognized syntactically here; Protocol-v2
# metadata remains the authority for command identity and is parsed elsewhere.
COMMAND_TITLE_RE = re.compile(r"^#\s+Command\s+\d{3,}\b.*$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[^\x00\r\n]{1,80}$")

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "command_id",
        "run_id",
        "claim_generation",
        "snapshot_sha256",
        "phases",
        "global_sha256",
        "phase_sha256",
        "final_acceptance_sha256",
    }
)
PROGRESS_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "command_id",
        "run_id",
        "claim_generation",
        "snapshot_sha256",
        "manifest_sha256",
        "phases",
        "current_phase",
        "completed_phases",
        "final_acceptance_verified",
        "updated_at",
    }
)


class PhasedTaskError(ValueError):
    """The phased declaration or its runtime evidence is unsafe to use."""


PhaseContractError = PhasedTaskError


@dataclass(frozen=True, slots=True)
class PhasedTask:
    """The parsed, normalized sections of one phased command body."""

    schema_version: int
    phases: tuple[str, ...]
    global_text: str
    phase_sections: tuple[tuple[str, str], ...]
    final_acceptance_text: str
    source_text: str

    @property
    def phase_texts(self) -> dict[str, str]:
        return dict(self.phase_sections)

    def phase_text(self, phase: str) -> str:
        for name, text in self.phase_sections:
            if name == phase:
                return text
        raise PhasedTaskError(f"Unknown declared phase: {phase}")


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Identity bound into the local manifest and progress projection."""

    project_id: str
    command_id: int
    run_id: str
    claim_generation: int


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    run_dir: Path
    snapshot: Path
    manifest: Path
    global_projection: Path
    current_phase: Path
    progress: Path


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
    paths: RuntimePaths
    task: PhasedTask
    manifest: dict[str, Any]
    progress: dict[str, Any]


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PhasedTaskError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhasedTaskError(f"{label} must be a non-negative integer")
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PhasedTaskError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_identity(identity: ExecutionIdentity) -> ExecutionIdentity:
    if not isinstance(identity, ExecutionIdentity):
        raise PhasedTaskError("execution identity is invalid")
    if not isinstance(identity.project_id, str) or not PROJECT_ID_RE.fullmatch(
        identity.project_id
    ):
        raise PhasedTaskError("project_id is invalid")
    _require_positive_int(identity.command_id, "command_id")
    if not isinstance(identity.run_id, str) or not RUN_ID_RE.fullmatch(identity.run_id):
        raise PhasedTaskError("run_id is invalid")
    _require_nonnegative_int(identity.claim_generation, "claim_generation")
    return identity


def _normalized_section(lines: list[str], label: str) -> str:
    text = "\n".join(lines).strip()
    if not text:
        raise PhasedTaskError(f"phased task {label} section must be non-empty")
    return text


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PhasedTaskError(
                "bridge-phased-task declaration contains duplicate JSON keys"
            )
        value[key] = item
    return value


def parse_declaration(command_text: str) -> tuple[str, ...] | None:
    """Parse the optional declaration, rejecting malformed lookalikes.

    Only the four fixed phase ids, in prefix order, are supported.  The body
    parser is intentionally separate from the existing Protocol-v2 command
    metadata parser so the canonical metadata line remains unchanged.
    """

    if not isinstance(command_text, str):
        raise PhasedTaskError("command text must be a string")

    matches: list[dict[str, Any]] = []
    for line in command_text.splitlines():
        if not DECLARATION_HINT_RE.search(line):
            continue
        match = DECLARATION_RE.match(line)
        if match is None:
            raise PhasedTaskError("malformed bridge-phased-task declaration")
        try:
            value = json.loads(match.group(1), object_pairs_hook=_strict_json_object)
        except json.JSONDecodeError as exc:
            raise PhasedTaskError("malformed bridge-phased-task JSON declaration") from exc
        if not isinstance(value, dict):
            raise PhasedTaskError("bridge-phased-task declaration must be a JSON object")
        matches.append(value)

    if not matches:
        return None
    if len(matches) != 1:
        raise PhasedTaskError("expected exactly one bridge-phased-task declaration")

    value = matches[0]
    if set(value) != {"schema_version", "phases"}:
        raise PhasedTaskError(
            "bridge-phased-task declaration contains unsupported fields"
        )
    if (
        isinstance(value.get("schema_version"), bool)
        or not isinstance(value.get("schema_version"), int)
        or value.get("schema_version") != SCHEMA_VERSION
    ):
        raise PhasedTaskError("unsupported bridge-phased-task schema_version")
    phases = value.get("phases")
    if not isinstance(phases, list) or not 1 <= len(phases) <= len(PHASE_IDS):
        raise PhasedTaskError("bridge-phased-task phases must contain 1 to 4 phases")
    if any(not isinstance(phase, str) for phase in phases):
        raise PhasedTaskError("bridge-phased-task phase ids must be strings")
    expected = list(PHASE_IDS[: len(phases)])
    if phases != expected:
        raise PhasedTaskError(
            "bridge-phased-task phases must be exactly the ordered prefix A, B, C, D"
        )
    return tuple(phases)


def _marker_key(match: re.Match[str]) -> tuple[str, str]:
    kind = match.group(1)
    phase = match.group(2)
    action = match.group(3)
    if kind == "bridge-global":
        return "global", action
    if kind == "bridge-final-acceptance":
        return "final", action
    assert phase is not None
    return f"phase:{phase}", action


def parse_phased_task(command_text: str) -> PhasedTask | None:
    """Parse and strictly validate one optional phased command body."""

    phases = parse_declaration(command_text)
    has_body_marker = any(
        MARKER_HINT_RE.search(line) for line in command_text.splitlines()
    )
    if phases is None:
        if has_body_marker:
            raise PhasedTaskError(
                "phased body markers require one bridge-phased-task declaration"
            )
        return None

    sections: dict[str, str] = {}
    order: list[str] = []
    current: str | None = None
    body_lines: list[str] = []
    declared = {f"phase:{phase}" for phase in phases}
    title_seen = False
    section_seen = False

    for line in command_text.splitlines():
        # The declaration is metadata for this optional body format, not a
        # section.  It is already validated above.
        if DECLARATION_HINT_RE.search(line):
            continue

        marker_match = MARKER_RE.match(line)
        if marker_match is None:
            if MARKER_HINT_RE.search(line):
                raise PhasedTaskError("malformed or unsupported phased section marker")
            if current is not None:
                body_lines.append(line)
            elif line.strip() and not COMMAND_METADATA_RE.match(line):
                if COMMAND_TITLE_RE.fullmatch(line):
                    if title_seen:
                        raise PhasedTaskError(
                            "phased task header contains multiple command titles"
                        )
                    if section_seen:
                        raise PhasedTaskError(
                            "phased task command title must appear before its sections"
                        )
                    title_seen = True
                    continue
                raise PhasedTaskError(
                    "phased task contains non-section content outside its markers"
                )
            continue

        key, action = _marker_key(marker_match)
        if key.startswith("phase:") and key not in declared:
            raise PhasedTaskError(f"undeclared phase section: {key.removeprefix('phase:')}")
        if action == "start":
            if current is not None:
                raise PhasedTaskError("nested or overlapping phased section markers")
            if key in sections or key in order:
                raise PhasedTaskError(f"duplicate phased section: {key}")
            current = key
            body_lines = []
            order.append(key)
            section_seen = True
            continue

        if current is None:
            raise PhasedTaskError(f"phased section ended without start: {key}")
        if current != key:
            raise PhasedTaskError(
                f"phased section ended out of order: expected {current}, found {key}"
            )
        label = "GLOBAL" if key == "global" else (
            "FINAL_ACCEPTANCE" if key == "final" else key
        )
        sections[key] = _normalized_section(body_lines, label)
        current = None
        body_lines = []

    if current is not None:
        raise PhasedTaskError(f"phased section is missing its end marker: {current}")

    expected_order = ["global", *(f"phase:{phase}" for phase in phases), "final"]
    if order != expected_order:
        missing = [key for key in expected_order if key not in sections]
        undeclared = [key for key in order if key not in expected_order]
        if missing:
            raise PhasedTaskError(
                "phased task is missing section(s): " + ", ".join(missing)
            )
        if undeclared:
            raise PhasedTaskError(
                "phased task contains undeclared section(s): " + ", ".join(undeclared)
            )
        raise PhasedTaskError("phased task section order does not match declaration")

    return PhasedTask(
        schema_version=SCHEMA_VERSION,
        phases=phases,
        global_text=sections["global"],
        phase_sections=tuple((phase, sections[f"phase:{phase}"]) for phase in phases),
        final_acceptance_text=sections["final"],
        source_text=command_text,
    )


# Short aliases make the pure parser convenient to callers without creating a
# second parser implementation.
parse = parse_phased_task


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _projection_bytes(text: str) -> bytes:
    if not isinstance(text, str):
        raise PhasedTaskError("projection text must be a string")
    return (text.rstrip() + "\n").encode("utf-8")


def _json_bytes(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=f".{os.getpid()}.{uuid.uuid4().hex}.tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class _CheckpointLock:
    """Fail-closed per-run lock for concurrent helper invocations."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "_CheckpointLock":
        try:
            self.handle = self.path.open("x", encoding="ascii", newline="")
            self.handle.write(str(os.getpid()))
            self.handle.flush()
        except FileExistsError as exc:
            raise PhasedTaskError("phased checkpoint is already being updated") from exc
        except OSError as exc:
            raise PhasedTaskError("phased checkpoint lock could not be acquired") from exc
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            self.handle.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _paths(run_dir: Path) -> RuntimePaths:
    run = Path(run_dir).resolve()
    return RuntimePaths(
        run_dir=run,
        snapshot=run / "task-snapshot.md",
        manifest=run / "task-manifest.json",
        global_projection=run / "global.md",
        current_phase=run / "current-phase.md",
        progress=run / "phase-progress.json",
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_run_dir(
    run_dir: Path,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    bridge_root: Path | None = None,
) -> Path:
    """Require the exact ``worker/runtime/<project>/runs/<run>`` shape."""

    run = Path(run_dir).expanduser().resolve()
    if (run_id is not None and run.name != run_id) or not RUN_ID_RE.fullmatch(run.name):
        raise PhasedTaskError("run-dir must name a valid run-id")
    if run.parent.name.casefold() != "runs":
        raise PhasedTaskError("run-dir must be inside a runs directory")
    candidate_project = run.parent.parent.name
    if not PROJECT_ID_RE.fullmatch(candidate_project):
        raise PhasedTaskError("run-dir project component is invalid")
    if project_id is not None and candidate_project != project_id:
        raise PhasedTaskError("run-dir project identity does not match manifest")
    if run.parent.parent.parent.name.casefold() != "runtime":
        raise PhasedTaskError("run-dir must be inside worker/runtime")
    if run.parent.parent.parent.parent.name.casefold() != "worker":
        raise PhasedTaskError("run-dir must be inside a Worker runtime tree")
    if bridge_root is not None:
        allowed_root = (Path(bridge_root).expanduser().resolve() / "worker" / "runtime")
        if not _within(run, allowed_root):
            raise PhasedTaskError("run-dir is outside the supplied Worker runtime tree")
    if run.exists() and not run.is_dir():
        raise PhasedTaskError("run-dir is not a directory")
    return run


def runtime_paths(run_dir: Path, *, bridge_root: Path | None = None) -> RuntimePaths:
    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    return _paths(run)


def _manifest_for(task: PhasedTask, identity: ExecutionIdentity, snapshot: bytes) -> dict[str, Any]:
    _safe_identity(identity)
    global_bytes = _projection_bytes(task.global_text)
    phase_hashes = {
        phase: _sha256(_projection_bytes(task.phase_text(phase)))
        for phase in task.phases
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": identity.project_id,
        "command_id": identity.command_id,
        "run_id": identity.run_id,
        "claim_generation": identity.claim_generation,
        "snapshot_sha256": _sha256(snapshot),
        "phases": list(task.phases),
        "global_sha256": _sha256(global_bytes),
        "phase_sha256": phase_hashes,
        "final_acceptance_sha256": _sha256(
            _projection_bytes(task.final_acceptance_text)
        ),
    }


def _validate_phases(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(PHASE_IDS):
        raise PhasedTaskError(f"{label} must contain 1 to 4 phases")
    expected = list(PHASE_IDS[: len(value)])
    if value != expected:
        raise PhasedTaskError(f"{label} must be the ordered prefix A, B, C, D")
    return tuple(value)


def validate_manifest(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PhasedTaskError("task manifest must be a JSON object")
    if set(data) != MANIFEST_FIELDS:
        raise PhasedTaskError("task manifest contains unsupported or missing fields")
    if (
        isinstance(data.get("schema_version"), bool)
        or not isinstance(data.get("schema_version"), int)
        or data.get("schema_version") != SCHEMA_VERSION
    ):
        raise PhasedTaskError("unsupported task manifest schema_version")
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise PhasedTaskError("task manifest project_id is invalid")
    command_id = _require_positive_int(data.get("command_id"), "manifest command_id")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise PhasedTaskError("task manifest run_id is invalid")
    claim_generation = _require_nonnegative_int(
        data.get("claim_generation"), "manifest claim_generation"
    )
    snapshot_sha256 = _require_sha(data.get("snapshot_sha256"), "snapshot_sha256")
    phases = _validate_phases(data.get("phases"), "manifest phases")
    global_sha256 = _require_sha(data.get("global_sha256"), "global_sha256")
    final_sha256 = _require_sha(
        data.get("final_acceptance_sha256"), "final_acceptance_sha256"
    )
    phase_sha256 = data.get("phase_sha256")
    if not isinstance(phase_sha256, Mapping) or set(phase_sha256) != set(phases):
        raise PhasedTaskError("manifest phase_sha256 keys do not match phases")
    normalized_phase_hashes = {
        phase: _require_sha(phase_sha256[phase], f"phase_sha256[{phase}]")
        for phase in phases
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "command_id": command_id,
        "run_id": run_id,
        "claim_generation": claim_generation,
        "snapshot_sha256": snapshot_sha256,
        "phases": list(phases),
        "global_sha256": global_sha256,
        "phase_sha256": normalized_phase_hashes,
        "final_acceptance_sha256": final_sha256,
    }


def validate_progress(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PhasedTaskError("phase progress must be a JSON object")
    if set(data) != PROGRESS_FIELDS:
        raise PhasedTaskError("phase progress contains unsupported or missing fields")
    if (
        isinstance(data.get("schema_version"), bool)
        or not isinstance(data.get("schema_version"), int)
        or data.get("schema_version") != SCHEMA_VERSION
    ):
        raise PhasedTaskError("unsupported phase progress schema_version")
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise PhasedTaskError("phase progress project_id is invalid")
    command_id = _require_positive_int(data.get("command_id"), "progress command_id")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise PhasedTaskError("phase progress run_id is invalid")
    claim_generation = _require_nonnegative_int(
        data.get("claim_generation"), "progress claim_generation"
    )
    snapshot_sha256 = _require_sha(data.get("snapshot_sha256"), "progress snapshot_sha256")
    manifest_sha256 = _require_sha(data.get("manifest_sha256"), "progress manifest_sha256")
    phases = _validate_phases(data.get("phases"), "progress phases")
    completed = data.get("completed_phases")
    if not isinstance(completed, list) or completed != list(phases[: len(completed)]):
        raise PhasedTaskError("completed_phases must be an ordered phase prefix")
    if len(completed) > len(phases):
        raise PhasedTaskError("completed_phases contains an unknown phase")
    current = data.get("current_phase")
    expected_current = (
        phases[len(completed)] if len(completed) < len(phases) else FINAL_ACCEPTANCE
    )
    if current != expected_current:
        raise PhasedTaskError(
            f"phase progress current_phase must be {expected_current}, found {current}"
        )
    final_verified = data.get("final_acceptance_verified")
    if not isinstance(final_verified, bool):
        raise PhasedTaskError("final_acceptance_verified must be boolean")
    if final_verified and current != FINAL_ACCEPTANCE:
        raise PhasedTaskError("final acceptance cannot be verified before all phases")
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not TIMESTAMP_RE.fullmatch(updated_at):
        raise PhasedTaskError("progress updated_at is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "command_id": command_id,
        "run_id": run_id,
        "claim_generation": claim_generation,
        "snapshot_sha256": snapshot_sha256,
        "manifest_sha256": manifest_sha256,
        "phases": list(phases),
        "current_phase": current,
        "completed_phases": list(completed),
        "final_acceptance_verified": final_verified,
        "updated_at": updated_at,
    }


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhasedTaskError(f"{label} cannot be read safely") from exc
    if not isinstance(raw, dict):
        raise PhasedTaskError(f"{label} must be a JSON object")
    return raw, raw_bytes


def load_manifest(
    run_dir: Path, *, bridge_root: Path | None = None
) -> dict[str, Any]:
    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    raw, _raw_bytes = _read_json(_paths(run).manifest, "task manifest")
    manifest = validate_manifest(raw)
    validate_run_dir(
        run,
        project_id=manifest["project_id"],
        run_id=manifest["run_id"],
        bridge_root=bridge_root,
    )
    return manifest


def load_progress(
    run_dir: Path, *, bridge_root: Path | None = None
) -> dict[str, Any]:
    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    raw, _raw_bytes = _read_json(_paths(run).progress, "phase progress")
    progress = validate_progress(raw)
    validate_run_dir(
        run,
        project_id=progress["project_id"],
        run_id=progress["run_id"],
        bridge_root=bridge_root,
    )
    return progress


def _read_task_snapshot(paths: RuntimePaths, manifest: Mapping[str, Any]) -> tuple[bytes, PhasedTask]:
    try:
        snapshot = paths.snapshot.read_bytes()
        text = snapshot.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PhasedTaskError("task snapshot cannot be read safely") from exc
    if _sha256(snapshot) != manifest["snapshot_sha256"]:
        raise PhasedTaskError("task snapshot SHA-256 does not match manifest")
    task = parse_phased_task(text)
    if task is None:
        raise PhasedTaskError("task snapshot no longer contains a phased declaration")
    return snapshot, task


def _inspect_runtime(
    run_dir: Path, *, bridge_root: Path | None = None
) -> RuntimeInspection:
    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    paths = _paths(run)
    raw_manifest, raw_manifest_bytes = _read_json(paths.manifest, "task manifest")
    manifest = validate_manifest(raw_manifest)
    validate_run_dir(
        run,
        project_id=manifest["project_id"],
        run_id=manifest["run_id"],
        bridge_root=bridge_root,
    )
    snapshot, task = _read_task_snapshot(paths, manifest)
    identity = ExecutionIdentity(
        project_id=manifest["project_id"],
        command_id=manifest["command_id"],
        run_id=manifest["run_id"],
        claim_generation=manifest["claim_generation"],
    )
    expected_manifest = _manifest_for(task, identity, snapshot)
    if manifest != expected_manifest:
        raise PhasedTaskError("task manifest does not match the immutable snapshot")

    raw_progress, _raw_progress_bytes = _read_json(paths.progress, "phase progress")
    progress = validate_progress(raw_progress)
    if (
        progress["project_id"] != manifest["project_id"]
        or progress["command_id"] != manifest["command_id"]
        or progress["run_id"] != manifest["run_id"]
        or progress["claim_generation"] != manifest["claim_generation"]
        or progress["snapshot_sha256"] != manifest["snapshot_sha256"]
        or progress["phases"] != manifest["phases"]
        or progress["manifest_sha256"] != _sha256(raw_manifest_bytes)
    ):
        raise PhasedTaskError("phase progress identity or manifest hash is invalid")

    try:
        global_bytes = paths.global_projection.read_bytes()
        current_bytes = paths.current_phase.read_bytes()
    except OSError as exc:
        raise PhasedTaskError("phased runtime projection is missing or unreadable") from exc
    if _sha256(global_bytes) != manifest["global_sha256"]:
        raise PhasedTaskError("global.md does not match the immutable task snapshot")
    current_expected = (
        task.final_acceptance_text
        if progress["current_phase"] == FINAL_ACCEPTANCE
        else task.phase_text(progress["current_phase"])
    )
    if current_bytes != _projection_bytes(current_expected):
        raise PhasedTaskError("current-phase.md does not match phase progress")

    # Future phases are intentionally never materialized as separate files.
    if any(path.name.startswith("phase-") and path.suffix == ".md" for path in run.iterdir()):
        raise PhasedTaskError("future phase projection files are not allowed")

    return RuntimeInspection(
        paths=paths,
        task=task,
        manifest=manifest,
        progress=progress,
    )


def verify_runtime(
    run_dir: Path, *, bridge_root: Path | None = None
) -> RuntimeInspection:
    """Verify snapshot, manifest, progress, and current projection together."""

    return _inspect_runtime(run_dir, bridge_root=bridge_root)


def verify_snapshot_hash(
    run_dir: Path, *, bridge_root: Path | None = None
) -> str:
    """Verify the immutable snapshot and return its digest."""

    inspection = _inspect_runtime(run_dir, bridge_root=bridge_root)
    return inspection.manifest["snapshot_sha256"]


def _initial_progress(manifest: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": manifest["project_id"],
        "command_id": manifest["command_id"],
        "run_id": manifest["run_id"],
        "claim_generation": manifest["claim_generation"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "manifest_sha256": _sha256(manifest_bytes),
        "phases": list(manifest["phases"]),
        "current_phase": manifest["phases"][0],
        "completed_phases": [],
        "final_acceptance_verified": False,
        "updated_at": _now_iso(),
    }


def prepare_runtime(
    run_dir: Path,
    command_text: str | bytes,
    identity: ExecutionIdentity,
    *,
    bridge_root: Path | None = None,
) -> RuntimeInspection:
    """Create the bounded per-run snapshot, manifest, and initial projection."""

    _safe_identity(identity)
    if isinstance(command_text, bytes):
        snapshot = bytes(command_text)
        try:
            text = snapshot.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PhasedTaskError("phased command is not valid UTF-8") from exc
    elif isinstance(command_text, str):
        text = command_text
        snapshot = text.encode("utf-8")
    else:
        raise PhasedTaskError("phased command text must be UTF-8 text or bytes")
    task = parse_phased_task(text)
    if task is None:
        raise PhasedTaskError("prepare_runtime requires a phased command")

    run = validate_run_dir(
        run_dir,
        project_id=identity.project_id,
        run_id=identity.run_id,
        bridge_root=bridge_root,
    )
    if run.exists():
        raise PhasedTaskError("phased run directory already exists")
    run.mkdir(parents=True, exist_ok=False)
    paths = _paths(run)
    manifest = _manifest_for(task, identity, snapshot)
    manifest_bytes = _json_bytes(manifest)
    progress = _initial_progress(manifest, manifest_bytes)

    # The snapshot is written as the exact claimed command bytes.  All other
    # text is a normalized, single-current projection with a trailing LF.
    _atomic_write_bytes(paths.snapshot, snapshot)
    _atomic_write_bytes(paths.manifest, manifest_bytes)
    _atomic_write_bytes(paths.global_projection, _projection_bytes(task.global_text))
    _atomic_write_bytes(
        paths.current_phase,
        _projection_bytes(task.phase_text(task.phases[0])),
    )
    _atomic_write_bytes(paths.progress, _json_bytes(progress))
    return _inspect_runtime(run, bridge_root=bridge_root)


initialize_runtime = prepare_runtime


def _write_transition(
    inspection: RuntimeInspection,
    next_progress: dict[str, Any],
    next_projection: str,
    *,
    bridge_root: Path | None = None,
) -> RuntimeInspection:
    paths = inspection.paths
    old_progress = paths.progress.read_bytes()
    old_projection = paths.current_phase.read_bytes()
    try:
        _atomic_write_bytes(paths.current_phase, _projection_bytes(next_projection))
        _atomic_write_bytes(paths.progress, _json_bytes(next_progress))
    except BaseException:
        # Try to restore the prior coherent pair.  If restoration itself is
        # unavailable, the next helper invocation fails closed on mismatch.
        try:
            _atomic_write_bytes(paths.current_phase, old_projection)
            _atomic_write_bytes(paths.progress, old_progress)
        except BaseException:
            pass
        raise
    return _inspect_runtime(paths.run_dir, bridge_root=bridge_root)


def advance_phase(
    run_dir: Path,
    phase: str,
    *,
    bridge_root: Path | None = None,
) -> RuntimeInspection:
    """Atomically checkpoint exactly the current phase and project the next."""

    if not isinstance(phase, str) or phase not in PHASE_IDS:
        raise PhasedTaskError("phase must be one of A, B, C, or D")
    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    paths = _paths(run)
    with _CheckpointLock(paths.run_dir / "phase-progress.lock"):
        inspection = _inspect_runtime(run, bridge_root=bridge_root)
        progress = inspection.progress
        current = progress["current_phase"]
        if current == FINAL_ACCEPTANCE:
            raise PhasedTaskError("all declared phases are already complete")
        if current != phase:
            raise PhasedTaskError(
                f"phase transition must advance current phase {current}, not {phase}"
            )
        completed = list(progress["completed_phases"])
        completed.append(phase)
        index = len(completed)
        next_phase = (
            inspection.task.phases[index]
            if index < len(inspection.task.phases)
            else FINAL_ACCEPTANCE
        )
        next_progress = dict(progress)
        next_progress.update(
            {
                "current_phase": next_phase,
                "completed_phases": completed,
                "final_acceptance_verified": False,
                "updated_at": _now_iso(),
            }
        )
        next_projection = (
            inspection.task.final_acceptance_text
            if next_phase == FINAL_ACCEPTANCE
            else inspection.task.phase_text(next_phase)
        )
        return _write_transition(
            inspection,
            next_progress,
            next_projection,
            bridge_root=bridge_root,
        )


advance = advance_phase


def complete_final(
    run_dir: Path, *, bridge_root: Path | None = None
) -> RuntimeInspection:
    """Verify the final projection and mark final acceptance as complete."""

    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    paths = _paths(run)
    with _CheckpointLock(paths.run_dir / "phase-progress.lock"):
        inspection = _inspect_runtime(run, bridge_root=bridge_root)
        progress = inspection.progress
        if progress["current_phase"] != FINAL_ACCEPTANCE:
            raise PhasedTaskError(
                "complete-final requires every declared phase to be completed"
            )
        if progress["final_acceptance_verified"]:
            raise PhasedTaskError("final acceptance is already verified")
        updated = dict(progress)
        updated["final_acceptance_verified"] = True
        updated["updated_at"] = _now_iso()
        _atomic_write_bytes(paths.progress, _json_bytes(updated))
        return _inspect_runtime(run, bridge_root=bridge_root)


complete_final_acceptance = complete_final


def safe_compact_successful_run(
    run_dir: Path,
    *,
    outcome: str,
    canonical_publication_succeeded: bool,
    pending_publication: bool,
    recovery_unresolved: bool,
    conflict: bool,
    bridge_root: Path | None = None,
) -> bool:
    """Delete large phased text only after every publication safety gate.

    The small manifest/progress pair and normal lifecycle logs remain subject
    to the Worker-owned bounded run retention.  A false precondition is a
    safe no-op; a malformed supposedly-successful runtime raises instead of
    deleting evidence.
    """

    flags = (
        canonical_publication_succeeded,
        pending_publication,
        recovery_unresolved,
        conflict,
    )
    if any(not isinstance(flag, bool) for flag in flags):
        raise PhasedTaskError("compact safety gates must be boolean")
    if (
        outcome != "SUCCESS"
        or not canonical_publication_succeeded
        or pending_publication
        or recovery_unresolved
        or conflict
    ):
        return False

    run = validate_run_dir(run_dir, bridge_root=bridge_root)
    inspection = _inspect_runtime(run, bridge_root=bridge_root)
    if not inspection.progress["final_acceptance_verified"]:
        raise PhasedTaskError("successful phased compaction requires verified final acceptance")
    for path in (
        inspection.paths.snapshot,
        inspection.paths.global_projection,
        inspection.paths.current_phase,
    ):
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise PhasedTaskError("phased evidence is already compacted or incomplete") from exc
    return True


compact_successful_run = safe_compact_successful_run


def _progress_json(inspection: RuntimeInspection) -> str:
    return json.dumps(inspection.progress, ensure_ascii=False, separators=(",", ":"))


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-Agent-Bridge phased task checkpoint helper")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    advance_parser = subparsers.add_parser("advance", help="complete the current phase")
    advance_parser.add_argument("--run-dir", required=True)
    advance_parser.add_argument("--bridge-root")
    advance_parser.add_argument("--phase", required=True)

    final_parser = subparsers.add_parser(
        "complete-final", help="verify and complete final acceptance"
    )
    final_parser.add_argument("--run-dir", required=True)
    final_parser.add_argument("--bridge-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    # A CLI invocation without an explicit root is still bound to the Bridge
    # checkout that owns this helper; callers in another checkout must opt in
    # to that checkout with --bridge-root.
    bridge_root = state_roots.resolve_state_root(args.bridge_root, for_write=True)
    try:
        if args.operation == "advance":
            inspection = advance_phase(
                Path(args.run_dir),
                args.phase,
                bridge_root=bridge_root,
            )
        else:
            inspection = complete_final(
                Path(args.run_dir),
                bridge_root=bridge_root,
            )
    except (OSError, PhasedTaskError) as exc:
        print(f"PHASED_TASK_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(_progress_json(inspection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
