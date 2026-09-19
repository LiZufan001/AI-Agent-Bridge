#!/usr/bin/env python3
"""Worker-owned delivery and evidence for owner-notification commands.

Codex is allowed to verify blocker semantics, but it does not own the email side
-effect.  A notification command carries immutable identity plus an explicit
plain-text payload.  After Codex returns SUCCESS, the Worker writes an atomic
pre-send journal record, sends through the existing Worker SMTP boundary, and
persists a minimal non-secret provider-accepted receipt.

A pre-existing pending/failed journal record is terminal for automatic sending:
it may represent a side effect that happened immediately before a crash, so a
later run must reconcile it instead of resending.
"""

from __future__ import annotations

import state_roots

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import bridge_alerts


MARKER_RE = re.compile(
    r"^\s*<!--\s*bridge-owner-notification:\s*(\{.*\})\s*-->\s*$",
    re.IGNORECASE,
)
BODY_START = "<!-- bridge-owner-notification-body:start -->"
BODY_END = "<!-- bridge-owner-notification-body:end -->"
MAX_BODY_CHARS = 16_384
SCHEMA_VERSION = 2


class OwnerNotificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerNotificationSpec:
    owner_action_id: str
    blocker_key: str
    body: str
    subject: str | None = None
    transport: str = "worker-email"


@dataclass(frozen=True, slots=True)
class ReceiptAssessment:
    receipt: dict[str, Any] | None
    error: str | None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _required_text(value: Any, field: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise OwnerNotificationError(f"{field} is invalid")
    if any(ord(char) < 32 for char in value):
        raise OwnerNotificationError(f"{field} contains control characters")
    return value.strip()


def _payload(command_text: str) -> str:
    starts = command_text.count(BODY_START)
    ends = command_text.count(BODY_END)
    if starts != 1 or ends != 1:
        raise OwnerNotificationError(
            "notification command must contain exactly one Worker-owned body block"
        )
    start = command_text.index(BODY_START) + len(BODY_START)
    end = command_text.index(BODY_END, start)
    body = command_text[start:end].strip()
    if not body:
        raise OwnerNotificationError("notification body is empty")
    if len(body) > MAX_BODY_CHARS:
        raise OwnerNotificationError("notification body exceeds the safe size limit")
    if "\x00" in body:
        raise OwnerNotificationError("notification body contains NUL")
    return body + "\n"


def parse_spec(command_text: str) -> OwnerNotificationSpec | None:
    """Parse one schema-v2 Worker-owned owner-notification declaration."""

    values: list[dict[str, Any]] = []
    for line in command_text.splitlines():
        match = MARKER_RE.match(line)
        if match is None:
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise OwnerNotificationError("notification marker contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise OwnerNotificationError("notification marker must be a JSON object")
        values.append(value)
    if not values:
        if BODY_START in command_text or BODY_END in command_text:
            raise OwnerNotificationError("notification body exists without identity marker")
        return None
    if len(values) != 1:
        raise OwnerNotificationError("exactly one notification marker is allowed")

    value = values[0]
    allowed = {
        "schema_version",
        "owner_action_id",
        "blocker_key",
        "transport",
        "subject",
    }
    if value.get("schema_version") != SCHEMA_VERSION or set(value) - allowed:
        raise OwnerNotificationError("notification marker schema is invalid")
    transport = value.get("transport", "worker-email")
    if transport != "worker-email":
        raise OwnerNotificationError("unsupported notification transport")
    subject_raw = value.get("subject")
    subject = (
        None
        if subject_raw is None
        else _required_text(subject_raw, "subject", limit=200)
    )
    return OwnerNotificationSpec(
        owner_action_id=_required_text(
            value.get("owner_action_id"), "owner_action_id", limit=128
        ),
        blocker_key=_required_text(value.get("blocker_key"), "blocker_key"),
        body=_payload(command_text),
        subject=subject,
        transport=transport,
    )


def notification_id(project_id: str, spec: OwnerNotificationSpec) -> str:
    logical = f"{project_id}\0{spec.owner_action_id}\0{spec.blocker_key}".encode("utf-8")
    return "owner-notify-" + hashlib.sha256(logical).hexdigest()[:24]


def _bridge_root() -> Path:
    return state_roots.resolve_state_root(for_write=True)


def runtime_dir(bridge_root: Path | None = None) -> Path:
    root = (bridge_root or _bridge_root()).resolve()
    return root / "worker" / "runtime" / "owner-notifications"


def record_path(
    project_id: str,
    spec: OwnerNotificationSpec,
    *,
    bridge_root: Path | None = None,
) -> Path:
    return runtime_dir(bridge_root) / f"{notification_id(project_id, spec)}.json"


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


def _write_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    rendered = json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_record(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OwnerNotificationError("notification journal record is not an object")
    return raw


def _default_subject(project_id: str) -> str:
    return f"Codex 任务通知 | {project_id}"


def _receipt(
    *,
    project_id: str,
    command_id: int,
    run_id: str,
    spec: OwnerNotificationSpec,
    sent_at: str,
    journal_path: Path,
    journal_persisted: bool,
    bridge_root: Path | None = None,
) -> dict[str, Any]:
    try:
        relative = journal_path.resolve().relative_to(bridge_root if bridge_root is not None else _bridge_root()).as_posix()
    except ValueError:
        relative = str(journal_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "notification_id": notification_id(project_id, spec),
        "project_id": project_id,
        "owner_action_id": spec.owner_action_id,
        "blocker_key": spec.blocker_key,
        "state": "SENT",
        "transport": spec.transport,
        "evidence_type": "provider_accepted",
        "provider_result": "accepted",
        "source_command_id": command_id,
        "source_run_id": run_id,
        "provider_event_at": sent_at,
        "evidence": {
            "source": "worker-owned-smtp-gateway",
            "binding": "immutable-command-marker+worker-pre-send-journal",
            "journal_path": relative,
            "journal_persisted_sent": journal_persisted,
        },
    }


def deliver(
    *,
    project_id: str,
    command_id: int,
    run_id: str,
    spec: OwnerNotificationSpec,
    bridge_root: Path | None = None,
) -> ReceiptAssessment:
    """Send at most once for the exact logical notification identity."""

    _required_text(project_id, "project_id", limit=128)
    _required_text(run_id, "run_id", limit=256)
    if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id < 1:
        raise OwnerNotificationError("command_id is invalid")

    path = record_path(project_id, spec, bridge_root=bridge_root)
    if path.exists():
        try:
            existing = _read_record(path)
        except Exception as exc:
            return ReceiptAssessment(
                None,
                f"notification journal exists but is unreadable ({type(exc).__name__}); reconciliation required",
            )
        state = str(existing.get("state", "unknown"))
        receipt = existing.get("receipt")
        if state == "sent" and isinstance(receipt, dict):
            return ReceiptAssessment(dict(receipt), None)
        return ReceiptAssessment(
            None,
            f"prior notification attempt exists with state {state}; reconciliation required and resend suppressed",
        )

    identity = notification_id(project_id, spec)
    pending = {
        "schema_version": SCHEMA_VERSION,
        "notification_id": identity,
        "project_id": project_id,
        "owner_action_id": spec.owner_action_id,
        "blocker_key": spec.blocker_key,
        "state": "pending",
        "source_command_id": command_id,
        "source_run_id": run_id,
        "attempted_at": now_iso(),
        "body_sha256": hashlib.sha256(spec.body.encode("utf-8")).hexdigest(),
    }
    try:
        _write_atomic(path, pending)
    except Exception as exc:
        return ReceiptAssessment(
            None,
            f"could not persist pre-send journal ({type(exc).__name__}); email not attempted",
        )

    try:
        bridge_alerts.send_test_email(
            {},
            subject=spec.subject or _default_subject(project_id),
            body=spec.body,
        )
    except Exception as exc:
        failed = dict(pending)
        failed["state"] = "failed"
        failed["updated_at"] = now_iso()
        failed["error_kind"] = type(exc).__name__
        try:
            _write_atomic(path, failed)
        except Exception:
            pass
        return ReceiptAssessment(
            None,
            f"Worker-owned email attempt failed or became ambiguous ({type(exc).__name__}); resend suppressed",
        )

    sent_at = now_iso()
    receipt = _receipt(
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        spec=spec,
        sent_at=sent_at,
        journal_path=path,
        journal_persisted=True,
        bridge_root=bridge_root,
    )
    sent = dict(pending)
    sent["state"] = "sent"
    sent["updated_at"] = sent_at
    sent["receipt"] = receipt
    try:
        _write_atomic(path, sent)
    except Exception:
        # The already-durable pending record still suppresses automatic resend.
        # The canonical Report can durably persist the in-memory SENT receipt.
        receipt = _receipt(
            project_id=project_id,
            command_id=command_id,
            run_id=run_id,
            spec=spec,
            sent_at=sent_at,
            journal_path=path,
            journal_persisted=False,
            bridge_root=bridge_root,
        )
    return ReceiptAssessment(receipt, None)


def receipt_marker(receipt: Mapping[str, Any]) -> str:
    return "BRIDGE_OWNER_NOTIFICATION_RECEIPT: " + json.dumps(
        dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = [
    "BODY_END",
    "BODY_START",
    "OwnerNotificationError",
    "OwnerNotificationSpec",
    "ReceiptAssessment",
    "deliver",
    "notification_id",
    "parse_spec",
    "receipt_marker",
    "record_path",
    "runtime_dir",
]
