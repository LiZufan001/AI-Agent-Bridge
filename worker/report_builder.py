#!/usr/bin/env python3
"""Pure Markdown renderers for Bridge execution reports.

This module turns already-determined execution facts into the historical Git
report text.  It deliberately does not read clocks or host state, run Codex,
perform Git/network work, or decide protocol/recovery state transitions.
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Mapping

import owner_notification


DIAGNOSTIC_TAIL_CHARS = 4000


# This is credential-pattern suppression, NOT anonymisation. Operational reports
# (paths, hosts, identifiers and timing) stay in private State even after redaction.
_CREDENTIAL_KEYS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
    "token", "accesstoken", "refreshtoken", "authtoken", "idtoken",
    "password", "passwd", "pwd", "secret", "clientsecret", "credentials",
})
_LABEL = re.compile(
    r"(?<!\w)(?P<key>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[A-Za-z][A-Za-z0-9_-]*)"
    r"(?P<separator>[ \t]*[=:]\s*)"
)
_JSON_DECODER = json.JSONDecoder()
_BARE_VALUE = re.compile(r"[^\s,;}\]]+")
_HEADER_END = re.compile(r"[\r\n]")
_AUTH_SCHEME = re.compile(r"(?:bearer|basic)[ \t]+", re.IGNORECASE)


def _credential_value_end(text: str, start: int) -> int:
    """Consume one value, including spaces/nesting; malformed quoted data fails closed."""
    for placeholder in ("[REDACTED_TOKEN]", "[REDACTED]"):
        if text.startswith(placeholder, start):
            return start + len(placeholder)
    if start == len(text):
        return start
    first = text[start]
    if first in '\"{[':
        try:
            _, consumed = _JSON_DECODER.raw_decode(text, start)
            return consumed
        except (ValueError, RecursionError):
            # Do not expose the continuation of truncated/malformed credentials.
            return len(text)
    if first == "'":
        index = start + 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
            elif text[index] == "'":
                return index + 1
            else:
                index += 1
        return len(text)
    match = _BARE_VALUE.match(text, start)
    return match.end() if match else start


def redact_diagnostics(text: str) -> str:
    """Suppress labelled credentials, quoted JSON keys/values and known token forms.

    Labelled encoded values are removed as a whole, not decoded into the report.
    Arbitrary unlabelled encodings and human identity inference remain out of scope.
    """
    chunks: list[str] = []
    copied = search = 0
    while match := _LABEL.search(text, search):
        raw_key = match.group("key")
        if raw_key.startswith('\"'):
            try:
                key = _JSON_DECODER.decode(raw_key)
            except ValueError:
                search = match.end()
                continue
        elif raw_key.startswith("'"):
            key = raw_key[1:-1]
        else:
            key = raw_key
        key = re.sub(r"[-_\s]", "", key).casefold()
        if key not in _CREDENTIAL_KEYS:
            search = match.end()
            continue
        start = match.end()
        prefix = ""
        if raw_key[:1] not in "\"'" and key in {
            "authorization", "proxyauthorization", "cookie", "setcookie"
        }:
            # A complete header value, not just the authentication scheme.
            end = _HEADER_END.search(text, start)
            end = end.start() if end else len(text)
            if key in {"authorization", "proxyauthorization"}:
                scheme = _AUTH_SCHEME.match(text, start, end)
                prefix = scheme.group(0) if scheme else ""
            replacement = prefix + "[REDACTED]"
        else:
            end = _credential_value_end(text, start)
            quote = text[start:start + 1]
            replacement = quote + "[REDACTED]" + quote if quote in ("'", '\"') else "[REDACTED]"
        chunks.extend((text[copied:start], replacement))
        copied = search = end
    chunks.append(text[copied:])
    redacted = "".join(chunks)
    return re.sub(
        r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[-A-Za-z0-9_]{8,}\b",
        "[REDACTED_TOKEN]", redacted,
    )


def build_worker_report(
    *,
    project_id: str,
    command_id: int,
    outcome: str,
    exit_code: int,
    workdir: Path,
    head_before: str | None,
    head_after: str | None,
    final_message: str,
    stderr: str,
    meta: Mapping[str, Any],
    run_id: str,
    claim_generation: int,
    executor_profile: Mapping[str, Any],
    completed_at: str,
    worker_host: str,
    run_result: Any | None = None,
) -> str:
    """Render the historical Worker report from supplied execution facts."""

    model = executor_profile.get("model") or "not explicitly set in codex_args"
    effort = (
        executor_profile.get("reasoning_effort")
        or "not explicitly set in codex_args"
    )
    lines = [
        f"# Report {command_id:03d} — {project_id}",
        "",
        f"- command_id: {command_id}",
        f"- outcome: {outcome}",
        f"- source: {meta.get('source', 'unknown')}",
        f"- based_on_report: {meta.get('based_on_report', 'unknown')}",
        f"- run_id: `{run_id}`",
        f"- claim_generation: {claim_generation}",
        f"- executor_profile_source: {executor_profile.get('source', 'unknown')}",
        f"- requested_model: `{model}`",
        f"- requested_reasoning_effort: `{effort}`",
        f"- effective_cli_model: `{model}`",
        f"- effective_cli_reasoning_effort: `{effort}`",
        f"- codex_exit_code: {exit_code}",
        f"- completed_at: {completed_at}",
        f"- worker_host: {worker_host}",
        f"- workdir: `{workdir}`",
        f"- git_head_before: `{head_before or 'N/A'}`",
        f"- git_head_after: `{head_after or 'N/A'}`",
    ]
    # Keep old no-tier reports byte-compatible.  When a command/default argv
    # explicitly contains a service tier, record both the requested/effective
    # run-local setting and the fact that no server-served tier was observed.
    service_tier = executor_profile.get("service_tier")
    if isinstance(service_tier, str) and service_tier:
        requested_service_tier = executor_profile.get(
            "requested_service_tier", service_tier
        )
        effective_service_tier = executor_profile.get(
            "effective_cli_service_tier", service_tier
        )
        lines += [
            f"- requested_service_tier: `{requested_service_tier}`",
            f"- effective_cli_service_tier: `{effective_service_tier}`",
            "- service_tier_observation: request configuration only; "
            "server-served tier not observed",
        ]
    if run_result is not None:
        lines += [
            f"- process_scope: {run_result.process_scope}",
            f"- wrapper_pid: {run_result.wrapper_pid}",
            f"- codex_root_pid: {run_result.codex_root_pid or 'N/A'}",
            f"- process_launched_at: {run_result.launched_at}",
            (
                "- final_marker_detected_at: "
                f"{run_result.final_marker_detected_at or 'N/A'}"
            ),
            f"- marker_status: {(run_result.marker or {}).get('status', 'N/A')}",
            f"- marker_result: {run_result.marker_result}",
            f"- process_exited_at: {run_result.process_exited_at or 'N/A'}",
            f"- forced_cleanup: {str(run_result.forced_cleanup).lower()}",
            (
                "- forced_cleanup_after_final: "
                f"{str(run_result.forced_cleanup_after_final).lower()}"
            ),
            f"- execution_timed_out: {str(run_result.timed_out).lower()}",
            f"- termination_reason: {run_result.termination_reason or 'N/A'}",
            (
                "- log_threshold_exceeded: "
                f"{str(run_result.log_threshold_exceeded).lower()}"
            ),
            f"- runtime_cleanup_error: {run_result.cleanup_error or 'N/A'}",
            f"- stdout_log: `{run_result.stdout_log_path}`",
            f"- stderr_log: `{run_result.stderr_log_path}`",
        ]
    lines += [
        "",
        "## Codex final response",
        "",
        redact_diagnostics(final_message)
        or "(No final Codex message was captured.)",
    ]
    if run_result is not None:
        receipt = getattr(run_result, "owner_notification_receipt", None)
        receipt_error = getattr(run_result, "owner_notification_receipt_error", None)
        if isinstance(receipt, Mapping):
            lines += [
                "",
                "## Worker owner-notification receipt",
                "",
                owner_notification.receipt_marker(receipt),
            ]
        elif isinstance(receipt_error, str) and receipt_error:
            lines += [
                "",
                "## Worker owner-notification receipt",
                "",
                "- state: MISSING",
                f"- reason: {redact_diagnostics(receipt_error)}",
            ]
    if outcome != "SUCCESS" or exit_code != 0 or not final_message or (
        run_result is not None and run_result.forced_cleanup
    ):
        # Redact before taking the tail so a truncated label cannot expose its value.
        diag = redact_diagnostics(stderr)[-DIAGNOSTIC_TAIL_CHARS:].strip() or "(no stderr)"
        lines += ["", "## Worker diagnostics", "", "```text", diag, "```"]
    return redact_diagnostics("\n".join(lines).rstrip() + "\n")


def build_manual_report(
    *,
    project_id: str,
    command_id: int,
    outcome: str,
    final_message: str,
    active_run: Mapping[str, Any],
    completed_at: str,
    executor_host: str,
) -> str:
    """Render the Manual schema; apply the same credential filter as Worker reports."""

    return redact_diagnostics(
        f"# Report {command_id:03d} — {project_id}\n\n"
        f"- command_id: {command_id}\n"
        f"- outcome: {outcome}\n"
        f"- source: {active_run.get('source', 'manual_chatgpt')}\n"
        f"- kind: {active_run.get('kind', 'EXECUTE')}\n"
        f"- based_on_report: {active_run.get('based_on_report', 'unknown')}\n"
        f"- run_id: `{active_run.get('run_id', 'unknown')}`\n"
        f"- claim_generation: {active_run.get('claimed_generation', 'unknown')}\n"
        f"- completed_at: {completed_at}\n"
        f"- executor_host: {executor_host}\n\n"
        "## Codex final response\n\n"
        f"{final_message.rstrip() or '(No final Codex message was supplied.)'}\n"
    )
