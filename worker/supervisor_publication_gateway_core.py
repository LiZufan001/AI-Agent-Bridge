#!/usr/bin/env python3
"""Local bridge for staged Scheduled Supervisor command publications.

The Scheduled ChatGPT Supervisor can write a bounded, non-canonical request to
GitHub, but it cannot invoke the Windows Worker's deterministic parsers.  This
module is the local-only boundary that consumes that request, validates the
exact command bytes, and reuses the existing Git-backed Protocol-v2 CAS store.

Staged requests are immutable transport input.  They never become a second
state machine and they never authorize a claim, report, recovery transition,
or executor side effect by themselves.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import bridge_common
import state_roots
import git_store
import phased_task
import protocol_core


SCHEMA_VERSION = 1
STAGED_PUBLICATION_ROOT = Path("worker") / "staged-publications"
REQUEST_DIRECTORY_NAME = "requests"
HISTORY_DIRECTORY_NAME = "history"
MAX_SCAN_ENTRIES = 128
MAX_REQUESTS_PER_POLL = 32
MAX_REQUEST_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 512 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_COMMAND_HISTORY_ENTRIES = 4096

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_NAME_RE = re.compile(
    r"^request-([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$", re.IGNORECASE
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,127}$")
_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:|cookie\s*:|(?:token|secret|password|api[_-]?key)\s*[=:]|"
    r"ghp_|github_pat_|sk-(?:proj-)?|-----begin)"
)

_REQUEST_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "request_id",
        "project_id",
        "command_id",
        "source",
        "kind",
        "based_on_report",
        "expected_generation",
        "command_sha256",
        "command_content",
        "created_at",
    }
)
_REQUEST_OPTIONAL_FIELDS = frozenset({"portfolio_pass_id"})
_REQUEST_ALLOWED_FIELDS = _REQUEST_REQUIRED_FIELDS | _REQUEST_OPTIONAL_FIELDS
_STAGED_NORMAL_FIELDS = frozenset(
    {"manual_request_id", "supersedes_command_id", "withdraws_command_id"}
)

_SAFE_REASONS = frozenset(
    {
        "published",
        "already_applied",
        "invalid_request",
        "invalid_request_json",
        "request_not_object",
        "request_fields_invalid",
        "request_size_exceeded",
        "request_id_invalid",
        "request_id_filename_mismatch",
        "project_id_invalid",
        "command_id_invalid",
        "based_on_report_invalid",
        "expected_generation_invalid",
        "source_invalid",
        "kind_invalid",
        "hash_invalid",
        "hash_mismatch",
        "command_content_invalid",
        "command_size_or_bytes_invalid",
        "command_secret_pattern_rejected",
        "created_at_invalid",
        "created_at_timezone_required",
        "portfolio_pass_id_invalid",
        "malformed_command_metadata",
        "staged_command_relation_not_allowed",
        "metadata_command_id_mismatch",
        "metadata_source_mismatch",
        "metadata_kind_mismatch",
        "metadata_based_on_report_mismatch",
        "metadata_expected_generation_mismatch",
        "source_kind_not_allowed_for_staging",
        "unsupported_schema_version",
        "request_path_invalid",
        "request_file_invalid",
        "request_not_tracked",
        "project_path_invalid",
        "project_not_found",
        "state_invalid",
        "generation_invalid",
        "latest_command_invalid",
        "latest_report_invalid",
        "project_mismatch",
        "not_report_ready",
        "wrong_generation",
        "wrong_based_on_report",
        "command_id_not_next",
        "command_conflict",
        "preflight_failed",
        "request_changed",
        "command_history_invalid",
        "active_project",
        "stale",
        "cas_race",
        "error_safe",
    }
)
_RETIREABLE_OUTCOMES = frozenset(
    {
        "published",
        "already_applied",
        "invalid",
        "stale",
        "active_project",
        "cas_race",
        "error_safe",
    }
)


class StagedPublicationError(ValueError):
    """A staged request is malformed or cannot be safely consumed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in _SAFE_REASONS else "invalid_request"
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class StagedPublicationRequest:
    """Validated transport envelope plus the exact command bytes it carries."""

    schema_version: int
    request_id: str
    project_id: str
    command_id: int
    source: str
    kind: str
    based_on_report: int
    expected_generation: int
    command_sha256: str
    command_content: str
    created_at: str
    portfolio_pass_id: str | None
    command_bytes: bytes
    metadata: Mapping[str, Any]
    raw_bytes: bytes

    @classmethod
    def from_bytes(
        cls,
        raw_bytes: bytes,
        *,
        filename_request_id: str,
    ) -> "StagedPublicationRequest":
        if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_REQUEST_BYTES:
            raise StagedPublicationError("request_size_exceeded")
        try:
            text = raw_bytes.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_strict_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise StagedPublicationError("invalid_request_json") from None
        if not isinstance(value, dict):
            raise StagedPublicationError("request_not_object")
        missing = _REQUEST_REQUIRED_FIELDS - set(value)
        unknown = set(value) - _REQUEST_ALLOWED_FIELDS
        if missing or unknown:
            raise StagedPublicationError("request_fields_invalid")

        schema_version = _bounded_int(
            value.get("schema_version"), "schema_version", minimum=1
        )
        if schema_version != SCHEMA_VERSION:
            raise StagedPublicationError("unsupported_schema_version")
        request_id = _safe_identifier(value.get("request_id"), "request_id")
        if request_id != filename_request_id:
            raise StagedPublicationError("request_id_filename_mismatch")
        project_id = _safe_identifier(
            value.get("project_id"), "project_id", project=True
        )
        command_id = _bounded_int(value.get("command_id"), "command_id", minimum=1)
        based_on_report = _bounded_int(
            value.get("based_on_report"), "based_on_report", minimum=0
        )
        expected_generation = _bounded_int(
            value.get("expected_generation"), "expected_generation", minimum=1
        )
        source = _safe_enum(
            value.get("source"), protocol_core.COMMAND_SOURCES, "source"
        )
        kind = _safe_enum(value.get("kind"), protocol_core.COMMAND_KINDS, "kind")
        command_sha256 = value.get("command_sha256")
        if not isinstance(command_sha256, str) or _SHA256_RE.fullmatch(command_sha256) is None:
            raise StagedPublicationError("hash_invalid")
        command_content = value.get("command_content")
        if not isinstance(command_content, str) or not command_content:
            raise StagedPublicationError("command_content_invalid")
        try:
            command_bytes = command_content.encode("utf-8")
        except UnicodeEncodeError:
            raise StagedPublicationError("command_content_invalid") from None
        if (
            not command_bytes
            or len(command_bytes) > MAX_COMMAND_BYTES
            or b"\x00" in command_bytes
        ):
            raise StagedPublicationError("command_size_or_bytes_invalid")
        if _SECRET_RE.search(command_content):
            raise StagedPublicationError("command_secret_pattern_rejected")
        if sha256(command_bytes).hexdigest() != command_sha256:
            raise StagedPublicationError("hash_mismatch")

        created_at = value.get("created_at")
        _validate_timestamp(created_at)
        portfolio_pass_id = value.get("portfolio_pass_id")
        if portfolio_pass_id is not None:
            portfolio_pass_id = _safe_value(portfolio_pass_id, "portfolio_pass_id")

        try:
            metadata = protocol_core.parse_command_metadata(command_content)
            protocol_core.validate_command_metadata(metadata)
        except protocol_core.ProtocolViolation:
            raise StagedPublicationError("malformed_command_metadata") from None
        if set(metadata) & _STAGED_NORMAL_FIELDS:
            raise StagedPublicationError("staged_command_relation_not_allowed")
        for field, expected in (
            ("command_id", command_id),
            ("source", source),
            ("kind", kind),
            ("based_on_report", based_on_report),
            ("expected_generation", expected_generation),
        ):
            if metadata.get(field) != expected:
                raise StagedPublicationError(f"metadata_{field}_mismatch")
        if not _is_supported_publication_pair(source, kind):
            raise StagedPublicationError("source_kind_not_allowed_for_staging")

        return cls(
            schema_version=schema_version,
            request_id=request_id,
            project_id=project_id,
            command_id=command_id,
            source=source,
            kind=kind,
            based_on_report=based_on_report,
            expected_generation=expected_generation,
            command_sha256=command_sha256,
            command_content=command_content,
            created_at=created_at,
            portfolio_pass_id=portfolio_pass_id,
            command_bytes=command_bytes,
            metadata=metadata,
            raw_bytes=raw_bytes,
        )


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """Bounded local outcome; raw request/command content is never included."""

    request_id: str
    project_id: str | None
    command_id: int | None
    outcome: str
    reason: str


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StagedPublicationError(f"{label}_invalid")
    return value


def _safe_identifier(value: Any, label: str, *, project: bool = False) -> str:
    pattern = _SAFE_PROJECT_ID_RE if project else _SAFE_ID_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StagedPublicationError(f"{label}_invalid")
    return value


def _safe_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_VALUE_RE.fullmatch(value) is None:
        raise StagedPublicationError(f"{label}_invalid")
    return value


def _safe_enum(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise StagedPublicationError(f"{label}_invalid")
    return value


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not (1 <= len(value) <= 64):
        raise StagedPublicationError("created_at_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StagedPublicationError("created_at_invalid") from None
    if parsed.tzinfo is None:
        raise StagedPublicationError("created_at_timezone_required")


def _is_supported_publication_pair(source: str, kind: str) -> bool:
    return (source, kind) in {
        ("scheduled_chatgpt", "EXECUTE"),
        ("finalizer", "FINALIZE"),
    }


def _target_status(kind: str) -> str:
    return "FINALIZING" if kind == "FINALIZE" else "COMMAND_READY"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _state_fingerprint(state: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _json_text(state: Mapping[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = state_roots.read_regular_bytes(path, max_bytes=MAX_STATE_BYTES)
        value = json.loads(
            raw_bytes.decode("utf-8"), object_pairs_hook=_strict_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite State JSON")),
        )
    except (OSError, ValueError, state_roots.StateRootError):
        raise StagedPublicationError("state_invalid") from None
    if not isinstance(value, dict):
        raise StagedPublicationError("state_invalid")
    return value


def validate_exact_command_for_publication(
    command_bytes: bytes,
    *,
    state: Mapping[str, Any],
    target_status: str,
    command_id: int,
) -> dict[str, Any]:
    """Run the exact pure preflight shared by staged and local Supervisor paths.

    ``state`` must already be the projected post-publication state.  This makes
    the Worker claim validator observe the same ``expected_generation`` and
    ``based_on_report`` boundary that the CAS payload will publish.
    """

    if target_status not in {"COMMAND_READY", "FINALIZING"}:
        raise protocol_core.ProtocolViolation("unsupported publication target status")
    try:
        command_text = command_bytes.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise protocol_core.ProtocolViolation("command bytes are not valid UTF-8") from exc
    metadata = protocol_core.parse_command_metadata(command_text)
    protocol_core.validate_command_metadata(metadata)
    if metadata.get("command_id") != command_id:
        raise protocol_core.ProtocolViolation("command metadata id does not match publication id")
    if target_status == "FINALIZING":
        if metadata.get("source") != "finalizer" or metadata.get("kind") != "FINALIZE":
            raise protocol_core.ProtocolViolation(
                "FINALIZING requires a finalizer FINALIZE command"
            )
    elif metadata.get("kind") != "EXECUTE":
        raise protocol_core.ProtocolViolation("COMMAND_READY requires an EXECUTE command")
    protocol_core.validate_pending_command(
        state=state,
        command_id=command_id,
        meta=metadata,
    )
    # This is intentionally the Worker parser, not a gateway-specific marker
    # check.  A malformed phased body must be rejected before canonical writes.
    phased_task.parse_phased_task(command_text)
    return metadata


class SupervisorPublicationGateway:
    """Consume bounded staged requests and publish at most one item per project."""

    def __init__(
        self,
        bridge_root: Path,
        *,
        request_directory: Path | None = None,
        max_requests_per_poll: int = MAX_REQUESTS_PER_POLL,
    ) -> None:
        if (
            isinstance(max_requests_per_poll, bool)
            or not 1 <= max_requests_per_poll <= MAX_SCAN_ENTRIES
        ):
            raise ValueError("max_requests_per_poll must be between 1 and the scan bound")
        # Validate lexical paths before resolve() can erase link evidence.
        try:
            state_roots.assert_no_links(Path(bridge_root))
            for path in (
                Path(bridge_root) / STAGED_PUBLICATION_ROOT / REQUEST_DIRECTORY_NAME,
                Path(bridge_root) / STAGED_PUBLICATION_ROOT / HISTORY_DIRECTORY_NAME,
            ):
                state_roots.assert_no_links(path)
            if request_directory is not None:
                state_roots.assert_no_links(Path(request_directory))
        except state_roots.StateRootError:
            raise ValueError("unsafe staged publication root") from None
        self.bridge_root = Path(bridge_root).resolve()
        self.request_directory = (
            Path(request_directory).resolve()
            if request_directory is not None
            else (
                self.bridge_root / STAGED_PUBLICATION_ROOT / REQUEST_DIRECTORY_NAME
            ).resolve()
        )
        self.history_directory = (
            self.bridge_root / STAGED_PUBLICATION_ROOT / HISTORY_DIRECTORY_NAME
        ).resolve()
        expected_root = (
            self.bridge_root / STAGED_PUBLICATION_ROOT / REQUEST_DIRECTORY_NAME
        ).resolve()
        try:
            self.request_directory.relative_to(expected_root)
        except ValueError as exc:
            raise ValueError(
                "staged publication requests must stay under the canonical request directory"
            ) from exc
        self.max_requests_per_poll = max_requests_per_poll

    def poll(self, *, project_id: str | None = None) -> tuple[GatewayResult, ...]:
        """Scan and consume staged requests without raising project-local errors."""

        if project_id is not None:
            _safe_identifier(project_id, "project_id", project=True)
        results: list[GatewayResult] = []
        published_projects: set[str] = set()
        retirements: list[tuple[Path, Path, bytes]] = []
        for path, filename_id in self._request_paths():
            if len(results) >= self.max_requests_per_poll:
                break
            raw_bytes: bytes | None = None
            try:
                raw_bytes = self._read_staged_blob(path)
                request = StagedPublicationRequest.from_bytes(
                    raw_bytes,
                    filename_request_id=filename_id,
                )
            except StagedPublicationError as exc:
                results.append(
                    GatewayResult(filename_id, None, None, "invalid", exc.reason)
                )
                if raw_bytes is not None:
                    retirement = self._retirement_for(
                        path,
                        filename_id,
                        raw_bytes,
                        "invalid",
                    )
                    if retirement is not None:
                        retirements.append(retirement)
                continue
            if project_id is not None and request.project_id != project_id:
                continue
            if request.project_id in published_projects:
                continue
            result = self._consume_one(path, request)
            results.append(result)
            if result.outcome == "published":
                published_projects.add(request.project_id)
            if result.outcome in _RETIREABLE_OUTCOMES:
                retirement = self._retirement_for(
                    path,
                    request.request_id,
                    request.raw_bytes,
                    result.outcome,
                )
                if retirement is not None:
                    retirements.append(retirement)
        if retirements:
            try:
                git_store.retire_tracked_paths(
                    self.bridge_root,
                    retirements,
                    "bridge: retire staged supervisor publication requests",
                )
            except (OSError, ValueError, git_store.WorkerError):
                # Canonical publication outcome is already final.  Leaving an
                # immutable request in the inbox is safe: the next poll will
                # observe the exact state and retry only this non-canonical
                # lifecycle move.
                pass
        return tuple(results)

    def _retirement_for(
        self,
        request_path: Path,
        request_id: str,
        raw_bytes: bytes,
        outcome: str,
    ) -> tuple[Path, Path, bytes] | None:
        if outcome not in _RETIREABLE_OUTCOMES:
            return None
        if not self._is_direct_child(request_path, self.request_directory):
            return None
        if request_id != "invalid-filename" and _SAFE_ID_RE.fullmatch(request_id):
            history_name = f"request-{request_id}.json"
        else:
            raw_identity = sha256(
                raw_bytes + b"\x00" + request_path.name.encode("utf-8", "surrogatepass")
            ).hexdigest()
            history_name = f"raw-{raw_identity}.json"
        outcome_directory = self.history_directory / outcome
        if self.history_directory.is_symlink() or outcome_directory.is_symlink():
            return None
        if self.history_directory.exists() and not self.history_directory.is_dir():
            return None
        if outcome_directory.exists() and not outcome_directory.is_dir():
            return None
        return request_path, outcome_directory / history_name, raw_bytes

    def _request_paths(self) -> tuple[tuple[Path, str], ...]:
        directory = self.request_directory
        if not directory.is_dir():
            return ()
        paths: list[tuple[Path, str]] = []
        try:
            with os.scandir(directory) as entries:
                examined = 0
                for entry in entries:
                    if examined >= MAX_SCAN_ENTRIES:
                        break
                    examined += 1
                    name = entry.name
                    if not name.startswith("request-") or not name.casefold().endswith(
                        ".json"
                    ):
                        continue
                    match = _REQUEST_NAME_RE.fullmatch(name)
                    # Never echo an unvalidated filename into health/log output.
                    filename_id = match.group(1) if match else "invalid-filename"
                    paths.append((Path(entry.path), filename_id))
        except OSError:
            return ()
        return tuple(sorted(paths, key=lambda item: item[0].name.casefold()))

    def _read_staged_blob(self, path: Path) -> bytes:
        if not self._is_direct_child(path, self.request_directory):
            raise StagedPublicationError("request_path_invalid")
        try:
            if path.is_symlink() or not path.is_file():
                raise StagedPublicationError("request_file_invalid")
            raw_bytes = git_store.read_blob(
                self.bridge_root,
                git_store.relative_path(path, self.bridge_root),
                max_bytes=MAX_REQUEST_BYTES,
            )
        except StagedPublicationError:
            raise
        except (OSError, git_store.WorkerError, ValueError):
            raise StagedPublicationError("request_not_tracked") from None
        if len(raw_bytes) > MAX_REQUEST_BYTES:
            raise StagedPublicationError("request_size_exceeded")
        return raw_bytes

    @staticmethod
    def _is_direct_child(path: Path, directory: Path) -> bool:
        try:
            return path.resolve(strict=False).parent == directory.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False

    def _project_root(self, project_id: str) -> Path:
        _safe_identifier(project_id, "project_id", project=True)
        project_root = self.bridge_root / "projects" / project_id
        try:
            state_roots.assert_no_links(project_root)
            for name in ("commands", "reports"):
                state_roots.assert_no_links(project_root / name)
        except state_roots.StateRootError:
            raise StagedPublicationError("project_path_invalid") from None
        if not project_root.is_dir():
            raise StagedPublicationError("project_not_found")
        return project_root

    def _consume_one(
        self,
        request_path: Path,
        request: StagedPublicationRequest,
    ) -> GatewayResult:
        try:
            project_root = self._project_root(request.project_id)
            state_path = project_root / "state.json"
            commands_dir = project_root / "commands"
            reports_dir = project_root / "reports"
            if project_root.is_symlink() or commands_dir.is_symlink() or reports_dir.is_symlink():
                return self._result(request, "invalid", "project_path_invalid")
            if not commands_dir.is_dir() or not reports_dir.is_dir():
                return self._result(request, "invalid", "project_path_invalid")
            if state_path.is_symlink() or not state_path.is_file():
                return self._result(request, "invalid", "state_invalid")
            state = _read_state(state_path)
            if state.get("protocol_version") != protocol_core.PROTOCOL_VERSION:
                return self._result(request, "invalid", "state_invalid")
            if state.get("project_id") != request.project_id:
                return self._result(request, "invalid", "project_mismatch")
            if self._already_applied(project_root, state, request):
                return self._result(request, "already_applied", "already_applied")

            status = state.get("status")
            if (
                status in protocol_core.PENDING_STATES
                or status == "CODEX_RUNNING"
                or state.get("active_run") is not None
            ):
                return self._result(request, "active_project", "active_project")
            if status != "REPORT_READY":
                return self._result(request, "stale", "not_report_ready")

            generation = _bounded_int(state.get("generation"), "generation", minimum=0)
            latest_command = _bounded_int(
                state.get("latest_command"), "latest_command", minimum=0
            )
            latest_report = _bounded_int(
                state.get("latest_report"), "latest_report", minimum=0
            )
            if request.expected_generation != generation + 1:
                return self._result(request, "stale", "wrong_generation")
            if request.based_on_report != latest_report:
                return self._result(request, "stale", "wrong_based_on_report")
            if request.command_id != latest_command + 1:
                return self._result(request, "stale", "command_id_not_next")

            command_path = (
                project_root
                / "commands"
                / f"command-{request.command_id:03d}.md"
            )
            report_path = (
                project_root
                / "reports"
                / f"report-{request.command_id:03d}.md"
            )
            if command_path.is_symlink() or report_path.is_symlink():
                return self._result(request, "invalid", "project_path_invalid")
            if command_path.exists() or report_path.exists():
                return self._result(request, "invalid", "command_conflict")
            if not self._command_history_is_bounded_and_monotonic(
                project_root, request.command_id
            ):
                return self._result(request, "invalid", "command_history_invalid")

            target_status = _target_status(request.kind)
            projected = dict(state)
            projected["status"] = target_status
            projected["generation"] = request.expected_generation
            projected["latest_command"] = request.command_id
            projected["last_reviewed_report"] = request.based_on_report
            projected["active_run"] = None
            try:
                validate_exact_command_for_publication(
                    request.command_bytes,
                    state=projected,
                    target_status=target_status,
                    command_id=request.command_id,
                )
            except (protocol_core.ProtocolViolation, phased_task.PhasedTaskError):
                return self._result(request, "invalid", "preflight_failed")

            initial_state_fingerprint = _state_fingerprint(state)
            initial_request_bytes = self._read_staged_blob(request_path)
            if initial_request_bytes != request.raw_bytes:
                return self._result(request, "cas_race", "request_changed")
            already_applied_seen = False

            def request_is_unchanged() -> bool:
                try:
                    return self._read_staged_blob(request_path) == initial_request_bytes
                except StagedPublicationError:
                    return False

            def state_is_eligible(current: dict[str, Any]) -> bool:
                return (
                    _state_fingerprint(current) == initial_state_fingerprint
                    and current.get("project_id") == request.project_id
                    and current.get("status") == "REPORT_READY"
                    and current.get("active_run") is None
                    and current.get("generation") == generation
                    and current.get("latest_command") == latest_command
                    and current.get("latest_report") == latest_report
                )

            def already_applied(current: dict[str, Any]) -> bool:
                nonlocal already_applied_seen
                if not request_is_unchanged():
                    raise bridge_common.CASConflict("staged request changed")
                already_applied_seen = self._already_applied(
                    project_root, current, request
                )
                return already_applied_seen

            def expected(current: dict[str, Any]) -> bool:
                return request_is_unchanged() and state_is_eligible(current)

            def payload_builder(current: dict[str, Any]) -> dict[Path, str | bytes]:
                if not request_is_unchanged() or not state_is_eligible(current):
                    raise bridge_common.CASConflict("staged publication evidence changed")
                # Read the state one more time immediately before constructing
                # the CAS payload.  The complete request is revalidated here,
                # not merely the fields observed during directory scanning.
                fresh = _read_state(state_path)
                if not state_is_eligible(fresh):
                    raise bridge_common.CASConflict("canonical state changed before publish")
                projected_fresh = dict(fresh)
                projected_fresh["status"] = target_status
                projected_fresh["generation"] = request.expected_generation
                projected_fresh["latest_command"] = request.command_id
                projected_fresh["last_reviewed_report"] = request.based_on_report
                projected_fresh["active_run"] = None
                try:
                    validate_exact_command_for_publication(
                        request.command_bytes,
                        state=projected_fresh,
                        target_status=target_status,
                        command_id=request.command_id,
                    )
                except (protocol_core.ProtocolViolation, phased_task.PhasedTaskError) as exc:
                    raise bridge_common.CASConflict("exact command preflight changed") from exc
                if (
                    command_path.is_symlink()
                    or report_path.is_symlink()
                    or command_path.exists()
                    or report_path.exists()
                ):
                    raise bridge_common.CASConflict("command file appeared before publish")
                if not self._command_history_is_bounded_and_monotonic(
                    project_root, request.command_id
                ):
                    raise bridge_common.CASConflict("command history changed before publish")
                updated = dict(fresh)
                updated["status"] = target_status
                updated["generation"] = request.expected_generation
                updated["latest_command"] = request.command_id
                updated["last_reviewed_report"] = request.based_on_report
                updated["active_run"] = None
                updated["updated_at"] = _now_iso()
                return {
                    command_path: request.command_bytes,
                    state_path: _json_text(updated),
                }

            try:
                final_state = git_store.publish_cas(
                    bridge_root=self.bridge_root,
                    state_path=state_path,
                    expected=expected,
                    already_applied=already_applied,
                    payload_builder=payload_builder,
                    message=(
                        f"bridge: staged supervisor publication {request.project_id} "
                        f"command {request.command_id:03d}"
                    ),
                )
            except bridge_common.CASConflict:
                return self._result(request, "cas_race", "cas_race")
            except (OSError, ValueError, json.JSONDecodeError, git_store.WorkerError):
                return self._result(request, "error_safe", "error_safe")
            except Exception:
                return self._result(request, "error_safe", "error_safe")

            if not isinstance(final_state, Mapping):
                return self._result(request, "error_safe", "error_safe")
            try:
                canonical_bytes = git_store.read_blob(
                    self.bridge_root,
                    git_store.relative_path(command_path, self.bridge_root),
                    max_bytes=MAX_COMMAND_BYTES,
                )
                verified_state = _read_state(state_path)
            except (OSError, StagedPublicationError, git_store.WorkerError, ValueError):
                return self._result(request, "error_safe", "error_safe")
            if (
                canonical_bytes != request.command_bytes
                or sha256(canonical_bytes).hexdigest() != request.command_sha256
                or verified_state.get("project_id") != request.project_id
                or verified_state.get("latest_command") != request.command_id
                or verified_state.get("generation") != request.expected_generation
                or verified_state.get("last_reviewed_report") != request.based_on_report
                or verified_state.get("status") != target_status
                or verified_state.get("active_run") is not None
            ):
                return self._result(request, "error_safe", "error_safe")
            return self._result(
                request,
                "already_applied" if already_applied_seen else "published",
                "already_applied" if already_applied_seen else "published",
            )
        except StagedPublicationError as exc:
            return self._result(request, "invalid", exc.reason)
        except Exception:
            return self._result(request, "error_safe", "error_safe")

    @staticmethod
    def _command_history_is_bounded_and_monotonic(
        project_root: Path,
        new_command_id: int,
    ) -> bool:
        commands_dir = project_root / "commands"
        if commands_dir.is_symlink() or not commands_dir.is_dir():
            return False
        ids: list[int] = []
        try:
            with os.scandir(commands_dir) as entries:
                count = 0
                for entry in entries:
                    count += 1
                    if count > MAX_COMMAND_HISTORY_ENTRIES:
                        return False
                    match = re.fullmatch(r"command-(\d+)\.md", entry.name, re.IGNORECASE)
                    if match:
                        try:
                            ids.append(int(match.group(1)))
                        except ValueError:
                            return False
        except OSError:
            return False
        return new_command_id > max(ids, default=0)

    def _already_applied(
        self,
        project_root: Path,
        state: Mapping[str, Any],
        request: StagedPublicationRequest,
    ) -> bool:
        if (
            state.get("project_id") != request.project_id
            or state.get("status") != _target_status(request.kind)
            or state.get("generation") != request.expected_generation
            or state.get("latest_report") != request.based_on_report
            or state.get("last_reviewed_report") != request.based_on_report
            or state.get("active_run") is not None
            or state.get("latest_command") != request.command_id
        ):
            return False
        command_path = project_root / "commands" / f"command-{request.command_id:03d}.md"
        if not command_path.is_file():
            return False
        try:
            canonical_bytes = git_store.read_blob(
                self.bridge_root,
                git_store.relative_path(command_path, self.bridge_root),
                max_bytes=MAX_COMMAND_BYTES,
            )
        except (OSError, git_store.WorkerError, ValueError):
            return False
        return (
            canonical_bytes == request.command_bytes
            and sha256(canonical_bytes).hexdigest() == request.command_sha256
        )

    @staticmethod
    def _result(
        request: StagedPublicationRequest,
        outcome: str,
        reason: str,
    ) -> GatewayResult:
        safe_reason = reason if reason in _SAFE_REASONS else "error_safe"
        return GatewayResult(
            request_id=request.request_id,
            project_id=request.project_id,
            command_id=request.command_id,
            outcome=outcome,
            reason=safe_reason,
        )


__all__ = [
    "GatewayResult",
    "HISTORY_DIRECTORY_NAME",
    "MAX_COMMAND_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_REQUESTS_PER_POLL",
    "MAX_STATE_BYTES",
    "SCHEMA_VERSION",
    "STAGED_PUBLICATION_ROOT",
    "StagedPublicationError",
    "StagedPublicationRequest",
    "SupervisorPublicationGateway",
    "validate_exact_command_for_publication",
]
