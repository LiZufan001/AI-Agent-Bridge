#!/usr/bin/env python3
"""Small health evidence for the unattended Bridge Worker.

The local health files are intentionally ignored by Git. They contain bounded
runtime facts only; raw exceptions, command lines and credentials do not belong
in this evidence.

A successful Worker poll may also refresh the single external GitHub heartbeat
slot configured by ``supervisor/bootstrap.json``. That heartbeat is ephemeral
availability evidence only. It is never canonical Protocol state and failures
are always best-effort/fail-closed for future intake without affecting active
runs.
"""

from __future__ import annotations

import state_roots

import json
import os
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

WORKER_HEALTH_FILENAME = "worker-health.json"
LAUNCHER_HEALTH_FILENAME = "launcher-health.json"

HEALTH_FIELDS = {
    "owner_console_control_version",
    "schema_version",
    "updated_at",
    "host",
    "pid",
    "launcher_pid",
    "worker_pid",
    "launcher_started_at",
    "worker_started_at",
    "last_worker_start",
    "last_poll_at",
    "last_poll_attempt",
    "last_successful_poll_at",
    "last_successful_fetch_at",
    "last_successful_fetch",
    "last_command_seen",
    "last_claim_attempt",
    "last_claim_attempt_detail",
    "last_claim_at",
    "last_seen_project",
    "last_seen_state",
    "last_process_exit",
    "last_process_exit_at",
    "last_exit_code",
    "last_worker_exit_at",
    "restart_count",
    "consecutive_failures",
    "next_retry_at",
    "last_failure_kind",
    "last_failure_at",
    "last_failure_stage",
    "last_failure_project",
    "last_failure_command_id",
    "last_failure_state",
    "poll_count",
    "last_gateway_poll_at",
    "last_gateway_request_id",
    "last_gateway_project",
    "last_gateway_command_id",
    "last_gateway_outcome",
    "last_gateway_reason",
    "last_alert_at",
    "last_alert_kind",
    "last_alert_identity",
    "last_alert_send_status",
    "last_alert_error",
    "last_self_maintenance_preflight_at",
    "last_self_maintenance_preflight_project",
    "last_self_maintenance_preflight_command_id",
    "last_self_maintenance_preflight_status",
    "last_self_maintenance_preflight_reason",
    "last_self_maintenance_live_branch",
    "last_self_maintenance_live_head",
    "last_self_maintenance_candidate_branch",
    "last_self_maintenance_candidate_head",
    "last_self_maintenance_bootstrap_base",
    "last_self_maintenance_guard_at",
    "last_self_maintenance_guard_status",
    "last_self_maintenance_guard_reason",
    "last_self_maintenance_guard_violations",
    "max_parallel_runs",
    "active_run_count",
    "active_runs",
    "resource_wait_project",
    "resource_wait_reason",
    "coordinator_lifecycle",
}

_SECRET_PATTERN = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer\s+)?|cookie\s*:\s*|(?:token|secret|password|api[_-]?key)\s*[=:]\s*|ghp_|github_pat_|sk-(?:proj-)?)[^\s,;]+"
)
_HEALTH_LOCK = threading.RLock()
_EXTERNAL_HEARTBEAT_LOCK = threading.RLock()
_EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC = 0.0
_EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC = 0.0
_EXTERNAL_HEARTBEAT_RETRY_FLOOR_SECONDS = 30.0
_EXTERNAL_HEARTBEAT_HTTP_TIMEOUT_SECONDS = 4.0
_EXTERNAL_HEARTBEAT_CREDENTIAL_TIMEOUT_SECONDS = 3.0


def now_iso() -> str:
    # The outer health gate compares Worker start time with the durable
    # restart-request time. Microseconds prevent a same-second startup from
    # being indistinguishable from a pre-restart process.
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def worker_health_path(
    bridge_root: Path,
    *,
    runtime_root: Path | None = None,
) -> Path:
    root = runtime_root if runtime_root is not None else bridge_root / "worker" / "runtime"
    return root / WORKER_HEALTH_FILENAME


def launcher_health_path(log_file: Path) -> Path:
    """Resolve the health file next to the launcher's runtime directory.

    The production log lives in ``worker/logs`` and therefore maps to
    ``worker/runtime``. Tests commonly pass a temporary log directly, in which
    case the temporary directory itself is used so cleanup stays local.
    """

    log_parent = log_file.resolve().parent
    runtime_parent = (
        log_parent.parent if log_parent.name.casefold() == "logs" else log_parent
    )
    return runtime_parent / "runtime" / LAUNCHER_HEALTH_FILENAME


def read_health(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        bounded: list[Any] = []
        for item in value[:16]:
            if isinstance(item, Mapping):
                bounded.append(
                    {
                        str(key)[:64]: _safe_value(item_value)
                        for key, item_value in list(item.items())[:12]
                    }
                )
            else:
                bounded.append(_safe_value(item))
        return bounded
    if isinstance(value, Mapping):
        return {
            str(key)[:64]: _safe_value(item_value)
            for key, item_value in list(value.items())[:12]
        }
    text = str(value).strip()
    if _SECRET_PATTERN.search(text):
        return "[REDACTED]"
    return text[:256]


def _write_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def update_health(path: Path, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Merge allowlisted, bounded values into a local health JSON file."""
    with _HEALTH_LOCK:
        data = read_health(path)
        data["schema_version"] = 1
        for key, value in updates.items():
            if key in HEALTH_FIELDS:
                data[key] = _safe_value(value)
        data["updated_at"] = now_iso()
        _write_atomic(path, data)
        return data


def _read_external_heartbeat_contract(bridge_root: Path) -> dict[str, Any] | None:
    """Return a strictly bounded deployment contract or None on any ambiguity."""

    path = bridge_root / "supervisor" / "bootstrap.json"
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    repository = value.get("heartbeat_repository", value.get("repository"))
    heartbeat = value.get("heartbeat")
    if not isinstance(repository, str) or not state_roots._REPOSITORY.fullmatch(repository) or not isinstance(heartbeat, dict):
        return None
    required = {
        "issue_number",
        "comment_id",
        "worker_id",
        "heartbeat_interval_seconds",
        "ttl_seconds",
        "max_future_skew_seconds",
    }
    if set(heartbeat) != required:
        return None
    if (bridge_root / state_roots.MARKER).exists() and state_roots.marker(bridge_root)["kind"] == "private-state":
        try:
            binding = state_roots.deployment_binding(bridge_root).get("heartbeat")
            expected = {"repository": repository, **heartbeat}
            if binding != expected:
                return None
        except state_roots.StateRootError:
            return None
    worker_id = heartbeat.get("worker_id")
    interval = heartbeat.get("heartbeat_interval_seconds")
    ttl = heartbeat.get("ttl_seconds")
    issue_number = heartbeat.get("issue_number")
    comment_id = heartbeat.get("comment_id")
    future_skew = heartbeat.get("max_future_skew_seconds")
    if (
        not isinstance(worker_id, str)
        or not worker_id
        or isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 60 <= interval <= 3600
        or isinstance(ttl, bool)
        or not isinstance(ttl, int)
        or not 60 <= ttl <= 7200
        or ttl <= interval
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
        or isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
        or isinstance(future_skew, bool)
        or not isinstance(future_skew, int)
        or not 0 <= future_skew <= 300
    ):
        return None
    return {
        "repository": repository,
        "issue_number": issue_number,
        "comment_id": comment_id,
        "worker_id": worker_id,
        "heartbeat_interval_seconds": interval,
        "ttl_seconds": ttl,
    }


def _credential_token_from_git() -> str | None:
    """Best-effort Git Credential Manager lookup; the token never leaves memory."""

    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=_EXTERNAL_HEARTBEAT_CREDENTIAL_TIMEOUT_SECONDS,
            check=False,
            env=env,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            token = line[len("password=") :].strip()
            if token and "\n" not in token and "\r" not in token and len(token) <= 4096:
                return token
    return None


def _github_token() -> str | None:
    # Never silently borrow a general GitHub identity from the parent session.
    if "BRIDGE_GITHUB_TOKEN" in os.environ:
        token = os.environ["BRIDGE_GITHUB_TOKEN"].strip()
        return token if token and "\n" not in token and "\r" not in token and len(token) <= 4096 else None
    if os.environ.get("BRIDGE_ALLOW_GIT_CREDENTIAL_MANAGER") == "1":
        return _credential_token_from_git()
    return None


def _patch_external_heartbeat(contract: Mapping[str, Any]) -> bool:
    token = _github_token()
    if token is None:
        return False
    worker_id = str(contract["worker_id"])
    payload = {
        "schema_version": 1,
        "worker_id": worker_id,
        "last_seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "heartbeat_interval_seconds": int(contract["heartbeat_interval_seconds"]),
        "ttl_seconds": int(contract["ttl_seconds"]),
    }
    body = "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"
    request_body = json.dumps({"body": body}, ensure_ascii=False).encode("utf-8")
    repository = str(contract["repository"])
    comment_id = int(contract["comment_id"])
    url = f"https://api.github.com/repos/{repository}/issues/comments/{comment_id}"
    request = urllib.request.Request(
        url,
        data=request_body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "AI-Agent-Bridge-Worker-Heartbeat/1",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=_EXTERNAL_HEARTBEAT_HTTP_TIMEOUT_SECONDS,
        ) as response:
            # Drain only a bounded prefix; response content is not evidence and
            # is intentionally not logged or persisted.
            response.read(4096)
            return int(getattr(response, "status", 0)) == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _maybe_publish_external_heartbeat(
    bridge_root: Path,
    merged_health: Mapping[str, Any],
) -> None:
    """Refresh the single external slot only at a proven successful poll boundary."""

    global _EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC
    global _EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC

    if str(merged_health.get("coordinator_lifecycle", "")).upper() != "RUNNING":
        return
    contract = _read_external_heartbeat_contract(bridge_root)
    if contract is None:
        return
    host = platform.node() or ""
    bound_host = os.environ.get("BRIDGE_LOCAL_WORKER_HOST", "")
    external_id = str(contract["worker_id"])
    if (not host or not bound_host or host.casefold() != bound_host.casefold()
            or external_id.casefold() == host.casefold()
            or not external_id.isascii() or not 1 <= len(external_id) <= 64
            or any(not (ch.isalnum() or ch in "._-") for ch in external_id)):
        return

    interval = float(contract["heartbeat_interval_seconds"])
    now_mono = time.monotonic()
    with _EXTERNAL_HEARTBEAT_LOCK:
        if (
            _EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC > 0
            and now_mono - _EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC < interval
        ):
            return
        if (
            _EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC > 0
            and now_mono - _EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC
            < min(_EXTERNAL_HEARTBEAT_RETRY_FLOOR_SECONDS, interval)
        ):
            return
        _EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC = now_mono
        if _patch_external_heartbeat(contract):
            _EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC = time.monotonic()


def update_worker_health(
    bridge_root: Path,
    *,
    runtime_root: Path | None = None,
    **updates: Any,
) -> dict[str, Any]:
    data = update_health(
        worker_health_path(bridge_root, runtime_root=runtime_root),
        updates,
    )
    # The hardened Worker records this field only after a successful fetch,
    # recovery/report reconciliation, gateway poll, config validation and
    # project intake pass. Keep the external side effect outside the local file
    # lock and isolate every failure from Worker correctness.
    if "last_successful_poll_at" in updates:
        try:
            _maybe_publish_external_heartbeat(bridge_root, data)
        except Exception:
            pass
    return data


def update_launcher_health(log_file: Path, **updates: Any) -> dict[str, Any]:
    return update_health(launcher_health_path(log_file), updates)
