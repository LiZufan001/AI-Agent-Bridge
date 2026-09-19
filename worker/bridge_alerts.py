#!/usr/bin/env python3
"""Minimal, durable and secret-safe Worker alerting.

The Worker already has the most useful context when an execution becomes unsafe.
This module supplies the small side-effect boundary needed to notify the owner
without becoming part of Protocol v2 state transitions.  SMTP is configured
through a provider-neutral Bridge namespace and an explicitly supplied env file.

Alert records are local runtime evidence under ``worker/runtime/alerts``.  A
record is written atomically before SMTP is attempted.  A ``pending`` record is
treated as already attempted after a Worker restart, which gives v1 an
at-most-once delivery attempt per incident and avoids duplicate mail during a
crash window.  SMTP failures are recorded as ``failed`` and are not retried by
the normal poll loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import uuid
from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_FROM_NAME = "AI-Agent-Bridge Worker"
DEFAULT_SMTP_TIMEOUT_SECONDS = 8.0
DEFAULT_TEST_SUBJECT = "[Bridge测试] Worker异常告警通道测试"
DEFAULT_TEST_BODY = (
    "这是测试邮件，不代表真实 Bridge 故障。\n"
    "本邮件仅用于验证 Worker 异常告警通道。\n"
)

_CONTEXT_FIELDS = (
    "timestamp",
    "project_id",
    "command_id",
    "run_id",
    "alert_kind",
    "current_bridge_status",
    "original_status",
    "generation",
    "generation_before",
    "claim_generation",
    "interruption_recovery_type",
    "recovery_reason_safe",
    "codex_terminated",
    "worker_is_alive",
    "journal_saved",
    "pending_report_saved",
    "remote_recovery_cas_success",
    "remote_publish_pending",
    "worktree_dirty",
    "local_commit_created",
    "unpushed_commits_present",
    "external_side_effects_unknown",
    "lease_expires_at",
    "claimed_at",
    "reconciliation_reason",
)
_BOOL_FIELDS = {
    "codex_terminated",
    "worker_is_alive",
    "journal_saved",
    "pending_report_saved",
    "remote_recovery_cas_success",
    "remote_publish_pending",
    "worktree_dirty",
    "local_commit_created",
    "unpushed_commits_present",
    "external_side_effects_unknown",
}
_INT_FIELDS = {
    "command_id",
    "generation",
    "generation_before",
    "claim_generation",
}
_TEXT_LIMITS = {
    "timestamp": 64,
    "project_id": 160,
    "run_id": 160,
    "alert_kind": 64,
    "current_bridge_status": 64,
    "original_status": 64,
    "interruption_recovery_type": 96,
    "recovery_reason_safe": 512,
    "lease_expires_at": 64,
    "claimed_at": 64,
    "reconciliation_reason": 512,
}

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|password|secret|auth(?:[_-]?code)?|"
        r"smtp[_-]?(?:password|auth[_-]?code)?)\s*[=:：]\s*)[^\s,;]+"
    ),
    re.compile(r"(?i)((?:授权码|密码|口令)\s*[=:：]\s*)[^\s,;]+"),
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[-A-Za-z0-9_]{8,}\b"),
)


class AlertConfigurationError(ValueError):
    """The local alert configuration is missing or invalid."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def alert_settings(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return explicitly enabled alert settings.

    Existing Worker configurations remain safe and testable when the new
    ``alerts`` block has not yet been adopted.  The production local config is
    updated separately, while tracked example config shows the opt-in block.
    """

    raw = config.get("alerts") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    return dict(raw)


def _safe_text(value: Any, *, limit: int = 256) -> str:
    text = str(value).replace("\x00", " ")
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED_TOKEN]", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return value if isinstance(value, bool) else None


def _normalize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in _CONTEXT_FIELDS:
        if key not in context:
            continue
        value = context[key]
        if key in _BOOL_FIELDS:
            normalized[key] = _safe_bool(value)
        elif key in _INT_FIELDS:
            normalized[key] = _safe_int(value)
        else:
            normalized[key] = _safe_text(value, limit=_TEXT_LIMITS.get(key, 256))

    normalized.setdefault("timestamp", now_iso())
    normalized["project_id"] = normalized.get("project_id") or "unknown-project"
    normalized["run_id"] = normalized.get("run_id") or "none"
    normalized["alert_kind"] = normalized.get("alert_kind") or "bridge-anomaly"
    normalized.setdefault("current_bridge_status", "unknown")
    normalized.setdefault("original_status", "unknown")
    normalized.setdefault("recovery_reason_safe", "manual inspection required")
    return normalized


def alert_identity(context: Mapping[str, Any]) -> str:
    """Build a stable identity from the required incident dimensions."""

    normalized = _normalize_context(context)
    identity_fields = {
        "project_id": normalized["project_id"],
        "command_id": normalized.get("command_id"),
        "run_id": normalized["run_id"],
        "alert_kind": normalized["alert_kind"],
    }
    rendered = json.dumps(
        identity_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def alert_runtime_dir(bridge_root: Path) -> Path:
    return bridge_root / "worker" / "runtime" / "alerts"


def alert_record_path(bridge_root: Path, context: Mapping[str, Any]) -> Path:
    return alert_runtime_dir(bridge_root) / f"{alert_identity(context)}.json"


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
        raise ValueError("alert record is not an object")
    return raw


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AlertConfigurationError(f"invalid env line {lineno}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise AlertConfigurationError(f"invalid env line {lineno}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_email_config(settings: Mapping[str, Any]) -> tuple[dict[str, str], Path | None]:
    """Resolve the provider-neutral Bridge SMTP contract in memory."""

    configured_path = str(settings.get("env_path", "")).strip()
    env_path_raw = os.environ.get("BRIDGE_SMTP_ENV_FILE", "").strip()
    env_path = Path(env_path_raw or configured_path).expanduser() if (env_path_raw or configured_path) else None
    file_values = _parse_env_file(env_path) if env_path is not None else {}

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    user = get("BRIDGE_SMTP_USER")
    password = get("BRIDGE_SMTP_PASSWORD")
    recipient = get("BRIDGE_SMTP_TO")
    host = get("BRIDGE_SMTP_HOST")
    port_raw = get("BRIDGE_SMTP_PORT")
    from_name = get(
        "BRIDGE_SMTP_FROM_NAME",
        str(settings.get("from_name", DEFAULT_FROM_NAME)) or DEFAULT_FROM_NAME,
    )
    if not host or not port_raw or not user or not password or not recipient:
        raise AlertConfigurationError("Bridge SMTP configuration is incomplete")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise AlertConfigurationError("SMTP port is invalid") from exc
    if not 1 <= port <= 65535:
        raise AlertConfigurationError("SMTP port is invalid")
    return (
        {
            "user": user,
            "password": password,
            "recipient": recipient,
            "host": host,
            "port": str(port),
            "from_name": from_name,
        },
        env_path,
    )


def _smtp_timeout(settings: Mapping[str, Any]) -> float:
    raw = settings.get("smtp_timeout_seconds", DEFAULT_SMTP_TIMEOUT_SECONDS)
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise AlertConfigurationError("SMTP timeout is invalid") from exc
    if timeout <= 0 or timeout > 60:
        raise AlertConfigurationError("SMTP timeout is outside the safe range")
    return timeout


def _subject(context: Mapping[str, Any]) -> str:
    normalized = _normalize_context(context)
    project = _safe_text(normalized.get("project_id", "unknown-project"), limit=120)
    command_id = normalized.get("command_id")
    command = f"Command {command_id:03d}" if isinstance(command_id, int) and command_id > 0 else "Command unknown"
    kind = str(normalized.get("alert_kind", ""))
    if kind == "lease_expired":
        suffix = "execution lease 已过期"
    elif kind == "network_guard_interruption":
        suffix = "需要恢复"
    elif kind == "deferred_recovery_conflict":
        suffix = "deferred recovery conflict"
    elif kind == "deferred_recovery_error":
        suffix = "deferred recovery reconciliation failed"
    else:
        suffix = "需要恢复"
    return f"[Bridge异常] {project} {command} {suffix}"


def _display(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return _safe_text(value, limit=512)


def build_alert_body(context: Mapping[str, Any]) -> str:
    """Render only the allowlisted, redacted diagnostic context."""

    normalized = _normalize_context(context)
    lines = [
        "AI-Agent-Bridge Worker 异常告警",
        "",
        f"时间: {normalized['timestamp']}",
        f"project_id: {normalized['project_id']}",
        f"command_id: {_display(normalized.get('command_id'))}",
        f"run_id: {normalized['run_id']}",
        f"alert_kind: {normalized['alert_kind']}",
        f"当前 Bridge status: {normalized.get('current_bridge_status', 'unknown')}",
        f"原状态: {normalized.get('original_status', 'unknown')}",
        f"generation: {_display(normalized.get('generation'))}",
        f"generation_before: {_display(normalized.get('generation_before'))}",
        f"claim_generation: {_display(normalized.get('claim_generation'))}",
        f"interruption/recovery 类型: {normalized.get('interruption_recovery_type', 'unknown')}",
        f"recovery_reason（安全版本）: {normalized.get('recovery_reason_safe', 'unknown')}",
        f"Codex 是否已终止: {_display(normalized.get('codex_terminated'))}",
        f"Worker 是否仍在运行: {_display(normalized.get('worker_is_alive'))}",
        f"journal 是否已保存: {_display(normalized.get('journal_saved'))}",
        f"pending report 是否已保存: {_display(normalized.get('pending_report_saved'))}",
        f"remote recovery CAS 是否成功: {_display(normalized.get('remote_recovery_cas_success'))}",
        f"remote_publish_pending: {_display(normalized.get('remote_publish_pending'))}",
        f"worktree_dirty: {_display(normalized.get('worktree_dirty'))}",
        f"local_commit_created: {_display(normalized.get('local_commit_created'))}",
        f"unpushed_commits_present: {_display(normalized.get('unpushed_commits_present'))}",
        f"external_side_effects_unknown: {_display(normalized.get('external_side_effects_unknown'))}",
        f"lease_expires_at: {_display(normalized.get('lease_expires_at'))}",
    ]
    if normalized.get("claimed_at"):
        lines.append(f"claimed_at: {normalized['claimed_at']}")
    if normalized.get("reconciliation_reason"):
        lines.append(
            f"reconciliation_reason（安全版本）: {normalized['reconciliation_reason']}"
        )

    if (
        normalized.get("alert_kind") == "network_guard_interruption"
        and normalized.get("remote_publish_pending") is True
    ):
        lines.extend(
            [
                "",
                "远端仍可能暂时显示 CODEX_RUNNING；",
                "本地 recovery journal 已保存；",
                "Worker 将在网络恢复后的成功 fetch 中执行 deferred reconciliation；",
                "原 Command 不会自动重跑。",
            ]
        )
    elif normalized.get("alert_kind") == "lease_expired":
        lines.extend(
            [
                "",
                "这是 orphaned-run safety fuse 被触发；",
                "Bridge 已进入 RECOVERY_REQUIRED；",
                "原 Command 禁止自动 rerun；",
                "需要 Supervisor / owner 检查。",
            ]
        )
    elif normalized.get("alert_kind") == "deferred_recovery_conflict":
        lines.extend(
            [
                "",
                "deferred recovery 未覆盖已变化的远端 canonical state；",
                "需要 Supervisor / owner 检查，原 Command 不会自动重跑。",
            ]
        )
    elif normalized.get("alert_kind") == "deferred_recovery_error":
        lines.extend(
            [
                "",
                "deferred recovery 无法安全补偿；",
                "需要 Supervisor / owner 检查，原 Command 不会自动重跑。",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _build_message(
    config: Mapping[str, str],
    context: Mapping[str, Any],
    *,
    subject_override: str | None = None,
    body_override: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    subject = subject_override or _subject(context)
    message["Subject"] = _safe_text(subject, limit=240).replace("\r", " ").replace("\n", " ")
    message["From"] = formataddr((config["from_name"], config["user"]))
    message["To"] = config["recipient"]
    message.set_content(body_override if body_override is not None else build_alert_body(context), charset="utf-8")
    return message


def send_message(config: Mapping[str, str], message: EmailMessage, *, timeout: float) -> None:
    """Send one message; callers isolate all exceptions from Worker state."""

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config["host"],
        int(config["port"]),
        timeout=timeout,
        context=context,
    ) as smtp:
        smtp.login(config["user"], config["password"])
        smtp.send_message(message)


def _result(
    *,
    status: str,
    identity: str | None = None,
    path: Path | None = None,
    attempted: bool = False,
    error_kind: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "identity": identity,
        "record_path": str(path) if path else None,
        "attempted": attempted,
        "error_kind": error_kind,
    }


def emit_alert(
    bridge_root: Path,
    config: Mapping[str, Any] | None,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist and best-effort send one deduplicated Worker alert.

    No exception escapes this function.  In particular, an SMTP failure cannot
    change canonical Bridge state, interrupt recovery, or cause a rerun.
    """

    settings = alert_settings(config)
    if settings is None:
        return _result(status="disabled")

    try:
        normalized = _normalize_context(context)
        identity = alert_identity(normalized)
        path = alert_runtime_dir(bridge_root) / f"{identity}.json"
    except Exception as exc:
        return _result(status="failed", error_kind=type(exc).__name__)

    if path.exists():
        # A pre-existing pending/failed record is intentionally also a dedupe
        # hit.  Retrying it automatically could duplicate a mail sent just
        # before a Worker crash, and v1 favors bounded at-most-once behavior.
        try:
            existing = _read_record(path)
            return _result(
                status="deduped",
                identity=identity,
                path=path,
                error_kind=str(existing.get("error_kind", "")) or None,
            )
        except Exception:
            # A malformed record is safer as a terminal evidence artifact than
            # as permission to send another potentially duplicate message.
            return _result(
                status="deduped",
                identity=identity,
                path=path,
                error_kind="malformed_record",
            )

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "identity": identity,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "pending",
        "attempt_count": 1,
        "attempted_at": now_iso(),
        "alert_kind": normalized["alert_kind"],
        "project_id": normalized["project_id"],
        "command_id": normalized.get("command_id"),
        "run_id": normalized["run_id"],
        "context": normalized,
    }
    try:
        _write_atomic(path, record)
    except Exception as exc:
        return _result(
            status="failed",
            identity=identity,
            path=path,
            attempted=False,
            error_kind=type(exc).__name__,
        )

    try:
        email_config, _env_path = resolve_email_config(settings)
        message = _build_message(email_config, normalized)
        send_message(email_config, message, timeout=_smtp_timeout(settings))
    except Exception as exc:
        record["status"] = "failed"
        record["updated_at"] = now_iso()
        record["error_kind"] = type(exc).__name__
        try:
            _write_atomic(path, record)
        except Exception:
            pass
        return _result(
            status="failed",
            identity=identity,
            path=path,
            attempted=True,
            error_kind=type(exc).__name__,
        )

    record["status"] = "sent"
    record["updated_at"] = now_iso()
    try:
        _write_atomic(path, record)
    except Exception:
        # The pending record still prevents a duplicate on the next Worker
        # start.  The SMTP attempt itself succeeded, so report sent.
        pass
    return _result(
        status="sent",
        identity=identity,
        path=path,
        attempted=True,
    )


def send_test_email(
    settings: Mapping[str, Any] | None = None,
    *,
    subject: str = DEFAULT_TEST_SUBJECT,
    body: str = DEFAULT_TEST_BODY,
) -> None:
    """Send a manually requested, non-incident channel test email."""

    email_config, _env_path = resolve_email_config(dict(settings or {"enabled": True}))
    message = _build_message(
        email_config,
        {"project_id": "bridge-test", "command_id": 0, "alert_kind": "test"},
        subject_override=subject,
        body_override=body,
    )
    send_message(email_config, message, timeout=_smtp_timeout(settings or {}))


__all__ = [
    "DEFAULT_TEST_BODY",
    "DEFAULT_TEST_SUBJECT",
    "alert_identity",
    "alert_record_path",
    "alert_runtime_dir",
    "alert_settings",
    "build_alert_body",
    "emit_alert",
    "resolve_email_config",
    "send_message",
    "send_test_email",
]
