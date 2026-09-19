#!/usr/bin/env python3
"""Owner-controlled execution gate for the local Bridge Worker.

The durable switch lives outside the canonical Bridge repository at a fixed
owner-controlled path. It governs only whether NEW Worker/Codex work may be
admitted. It never changes Protocol-v2 state and never terminates an already
active run.
"""

from __future__ import annotations

import state_roots

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from bridge_common import WorkerError

CONTROL_REPOSITORY = "example-owner/owner-control"
CONTROL_BRANCH = "main"
CONTROL_PATH = "bridge-control/worker-execution.json"
CONTROL_MAX_BYTES = 16 * 1024
CONTROL_HTTP_TIMEOUT_SECONDS = 4.0
CONTROL_CREDENTIAL_TIMEOUT_SECONDS = 3.0

AUTO = "auto"
PAUSED = "paused"
OWNER_REASONS = {
    "owner_resume",
    "owner_quota_saving",
    "owner_manual_pause",
}
_REQUIRED_CONTROL_FIELDS = {
    "schema_version",
    "execution_mode",
    "reason",
    "set_by",
    "set_at",
}
_REQUIRED_CONTRACT_FIELDS = {
    "repository",
    "branch",
    "path",
    "fail_closed_mode",
}


@dataclass(frozen=True, slots=True)
class ExecutionControlDecision:
    execution_mode: str
    reason: str
    set_by: str
    set_at: str | None
    supported: bool
    source_status: str

    @property
    def execution_allowed(self) -> bool:
        return self.execution_mode == AUTO

    def health_projection(self) -> dict[str, object]:
        return {
            "execution_mode": self.execution_mode,
            "execution_reason": self.reason,
            "execution_control_status": self.source_status,
            "execution_control_set_at": self.set_at,
        }


class ExecutionControlError(RuntimeError):
    pass


UrlOpener = Callable[..., Any]


def _fail_closed(status: str) -> ExecutionControlDecision:
    return ExecutionControlDecision(
        execution_mode=PAUSED,
        reason="control_unavailable",
        set_by="system",
        set_at=None,
        supported=True,
        source_status=status,
    )


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ExecutionControlError("control_duplicate_key")
        value[key] = item
    return value


def _bootstrap_contract(bridge_root: Path) -> Mapping[str, object] | None:
    path = bridge_root / "supervisor" / "bootstrap.json"
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ExecutionControlError("bootstrap_too_large")
        bootstrap = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionControlError("bootstrap_invalid") from exc
    if not isinstance(bootstrap, dict):
        raise ExecutionControlError("bootstrap_invalid")
    raw_contract = bootstrap.get("execution_control")
    if raw_contract is None:
        return None
    if not isinstance(raw_contract, dict) or set(raw_contract) != _REQUIRED_CONTRACT_FIELDS:
        raise ExecutionControlError("contract_invalid")
    try:
        expected = state_roots.expected_control(bridge_root, {
            "repository": CONTROL_REPOSITORY, "branch": CONTROL_BRANCH,
            "path": CONTROL_PATH, "fail_closed_mode": PAUSED,
        })
        if state_roots.validate_control_contract(raw_contract) != expected:
            raise ExecutionControlError("contract_invalid")
    except state_roots.StateRootError as exc:
        raise ExecutionControlError("deployment_binding_invalid") from exc
    return raw_contract


def _credential_token_from_git() -> str | None:
    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    options: dict[str, int] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=CONTROL_CREDENTIAL_TIMEOUT_SECONDS,
            check=False,
            env=env,
            **options,
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


def _github_token(explicit: str | None = None) -> str | None:
    if explicit is not None:
        token = explicit.strip()
        return token if token and len(token) <= 4096 and "\n" not in token and "\r" not in token else None
    # Never silently borrow a general GitHub identity from the parent session.
    if "BRIDGE_GITHUB_TOKEN" in os.environ:
        token = os.environ["BRIDGE_GITHUB_TOKEN"].strip()
        return token if token and "\n" not in token and "\r" not in token and len(token) <= 4096 else None
    if os.environ.get("BRIDGE_ALLOW_GIT_CREDENTIAL_MANAGER") == "1":
        return _credential_token_from_git()
    return None


def _parse_timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ExecutionControlError("control_timestamp_invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ExecutionControlError("control_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ExecutionControlError("control_timestamp_invalid")
    if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=60):
        raise ExecutionControlError("control_timestamp_future")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ExecutionControlError("control_redirect_refused")


def parse_control(raw: bytes) -> ExecutionControlDecision:
    if len(raw) > CONTROL_MAX_BYTES:
        raise ExecutionControlError("control_too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionControlError("control_invalid_json") from exc
    if not isinstance(value, dict) or set(value) != _REQUIRED_CONTROL_FIELDS:
        raise ExecutionControlError("control_invalid_shape")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise ExecutionControlError("control_schema_version")
    mode = value.get("execution_mode")
    reason = value.get("reason")
    if (
        not isinstance(mode, str)
        or not isinstance(reason, str)
        or mode not in {AUTO, PAUSED}
        or reason not in OWNER_REASONS
    ):
        raise ExecutionControlError("control_value_invalid")
    if value.get("set_by") != "owner":
        raise ExecutionControlError("control_authority_invalid")
    if mode == AUTO and reason != "owner_resume":
        raise ExecutionControlError("control_reason_mode_mismatch")
    if mode == PAUSED and reason not in {"owner_quota_saving", "owner_manual_pause"}:
        raise ExecutionControlError("control_reason_mode_mismatch")
    set_at = _parse_timestamp(value.get("set_at"))
    return ExecutionControlDecision(
        execution_mode=mode,
        reason=reason,
        set_by="owner",
        set_at=set_at,
        supported=True,
        source_status="valid",
    )


def _control_url(contract: Mapping[str, object] | None = None) -> str:
    bound = contract or {"repository": CONTROL_REPOSITORY, "path": CONTROL_PATH, "branch": CONTROL_BRANCH}
    owner, repo = str(bound["repository"]).split("/", 1)
    path = urllib.parse.quote(str(bound["path"]), safe="/")
    branch = urllib.parse.quote(str(bound["branch"]), safe="")
    return (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/"
        f"{urllib.parse.quote(repo)}/contents/{path}?ref={branch}"
    )


def read_execution_control(
    bridge_root: Path,
    *,
    token: str | None = None,
    opener: UrlOpener | None = None,
) -> ExecutionControlDecision:
    """Fail closed on absent, invalid or unreadable control; never infer AUTO."""

    try:
        contract = _bootstrap_contract(bridge_root)
    except ExecutionControlError as exc:
        return _fail_closed(str(exc))
    if contract is None:
        return _fail_closed("contract_absent")

    credential = _github_token(token)
    if credential is None:
        return _fail_closed("credential_unavailable")
    request = urllib.request.Request(
        _control_url(contract),
        method="GET",
        headers={
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Cache-Control": "no-cache",
            "User-Agent": "AI-Agent-Bridge-Worker-Execution-Control/1",
        },
    )
    try:
        open_url = opener or urllib.request.build_opener(_NoRedirect()).open
        with open_url(request, timeout=CONTROL_HTTP_TIMEOUT_SECONDS) as response:
            encoded = response.read(CONTROL_MAX_BYTES * 4)
            status = int(getattr(response, "status", 0))
        if status != 200:
            return _fail_closed(f"github_http_{status}")
        envelope = json.loads(encoded.decode("utf-8"))
        if not isinstance(envelope, dict):
            return _fail_closed("github_response_invalid")
        if envelope.get("encoding") != "base64" or not isinstance(envelope.get("content"), str):
            return _fail_closed("github_response_invalid")
        raw = base64.b64decode(str(envelope["content"]).replace("\n", ""), validate=True)
        return parse_control(raw)
    except urllib.error.HTTPError as exc:
        return _fail_closed(f"github_http_{exc.code}")
    except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _fail_closed("github_read_failed")
    except ExecutionControlError as exc:
        return _fail_closed(str(exc))


def new_execution_allowed(bridge_root: Path) -> ExecutionControlDecision:
    """Re-read immediately before every new claim/publication attempt."""
    return read_execution_control(bridge_root)


def require_new_execution(bridge_root: Path) -> ExecutionControlDecision:
    decision = new_execution_allowed(bridge_root)
    if not decision.execution_allowed:
        raise WorkerError("Owner execution control blocks new work: " + decision.source_status)
    return decision
