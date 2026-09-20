#!/usr/bin/env python3
"""Codex invocation, prompt, and lifecycle-adapter boundary for the Worker.

This module decides how an already-authorized Bridge command invokes Codex.  It
does not own Protocol metadata validation, lease/CAS state, network policy,
recovery, or report publication.  The process-tree and marker lifecycle remain
owned by :mod:`codex_lifecycle`; this module only adapts the prepared argv and
runtime options to that boundary.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

import bridge_common
import codex_lifecycle
import phased_task


WorkerError = bridge_common.WorkerError
CodexRunResult = codex_lifecycle.CodexRunResult
MarkerParser = codex_lifecycle.MarkerParser

FULL_ACCESS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
WORKSPACE_WRITE_MODE = "workspace_write"
WORKSPACE_WRITE_SANDBOX_ARGS = (
    "--sandbox",
    "workspace-write",
    "--ask-for-approval",
    "never",
    "-c",
    "sandbox_workspace_write.network_access=true",
)
CONFLICTING_CODEX_FLAGS = {
    "-a",
    "-s",
    "--approval-policy",
    "--ask-for-approval",
    "--full-auto",
    "--sandbox",
}
EXECUTOR_CONFIG_KEYS = {"model", "model_reasoning_effort", "service_tier"}


def run_codex_process(**kwargs: Any) -> CodexRunResult:
    """Small patchable seam that forwards to the lifecycle implementation."""

    return codex_lifecycle.run_codex_process(**kwargs)


def command_argv(command: str) -> list[str]:
    """Return an executable argv prefix for the configured command."""

    resolved = shutil.which(command)
    if os.name == "nt" and resolved:
        resolved_path = Path(resolved)
        suffix = resolved_path.suffix.lower()
        if suffix in {".cmd", ".bat", ".exe", ""}:
            return [str(resolved_path)]
        if suffix != ".ps1":
            return [command]
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise WorkerError(
                "Codex resolves to a PowerShell launcher, but no PowerShell executable was found."
            )
        return [powershell, "-NoLogo", "-NoProfile", "-File", resolved]
    return [command]


def validate_codex_command(command: str) -> None:
    """Reuse the Worker's fail-closed executable availability check."""

    if shutil.which(command) is None and not Path(command).exists():
        raise WorkerError(f"Required Codex command not found: {command}")


def codex_args_from_config(config: dict[str, Any]) -> list[str]:
    """Validate and return the unattended full-access Codex argv."""

    mode = str(config.get("codex_execution_mode", ""))
    if mode != "full_access":
        raise WorkerError(
            "Bridge Worker requires codex_execution_mode=full_access for unattended execution."
        )

    args = list(config.get("codex_args", []))
    if FULL_ACCESS_FLAG not in args:
        raise WorkerError(
            f"Bridge Worker full-access mode requires {FULL_ACCESS_FLAG}."
        )
    conflicts = [
        arg
        for arg in args
        if arg in CONFLICTING_CODEX_FLAGS
        or arg.startswith("--approval-policy=")
        or arg.startswith("--ask-for-approval=")
        or arg.startswith("--sandbox=")
    ]
    if conflicts:
        raise WorkerError(
            "Full-access Codex args must not include sandbox/approval options: "
            + ", ".join(conflicts)
        )
    return args


def _config_assignment(argument: str) -> tuple[str, str] | None:
    key, separator, value = argument.partition("=")
    if not separator:
        return None
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _security_config_key(key: str) -> bool:
    normalized = key.strip()
    return (
        normalized in {
            "sandbox_mode",
            "approval_policy",
            "approvals_reviewer",
            "default_permissions",
            "sandbox_workspace_write.network_access",
        }
        or normalized.startswith("permissions.")
    )


def validate_self_maintenance_codex_args(codex_args: list[str]) -> list[str]:
    """Validate the run-local Candidate-only Codex sandbox contract."""

    if FULL_ACCESS_FLAG in codex_args:
        raise WorkerError("Self-maintenance Codex must not run with full access.")

    sandbox_values: list[str] = []
    approval_values: list[str] = []
    network_values: list[str] = []
    index = 0
    while index < len(codex_args):
        argument = codex_args[index]
        if argument in {"-C", "--cd", "--add-dir"} or argument.startswith(
            ("-C=", "--cd=", "--add-dir=")
        ):
            raise WorkerError(
                "Self-maintenance Codex must not override or extend its Candidate workspace."
            )
        if argument in {"-s", "--sandbox"}:
            if index + 1 >= len(codex_args):
                raise WorkerError("Self-maintenance sandbox flag is missing its value.")
            sandbox_values.append(codex_args[index + 1])
            index += 2
            continue
        if argument.startswith("--sandbox="):
            sandbox_values.append(argument.split("=", 1)[1])
            index += 1
            continue
        if argument in {"-a", "--approval-policy", "--ask-for-approval"}:
            if index + 1 >= len(codex_args):
                raise WorkerError("Self-maintenance approval flag is missing its value.")
            approval_values.append(codex_args[index + 1])
            index += 2
            continue
        if argument.startswith("--approval-policy=") or argument.startswith(
            "--ask-for-approval="
        ):
            approval_values.append(argument.split("=", 1)[1])
            index += 1
            continue

        assignment: tuple[str, str] | None = None
        if argument in {"-c", "--config"} and index + 1 < len(codex_args):
            assignment = _config_assignment(codex_args[index + 1])
            index += 2
        elif argument.startswith("--config=") or argument.startswith("-c="):
            assignment = _config_assignment(argument.split("=", 1)[1])
            index += 1
        else:
            index += 1
        if assignment is not None and assignment[0] == "sandbox_workspace_write.network_access":
            network_values.append(assignment[1])

    if sandbox_values != ["workspace-write"]:
        raise WorkerError(
            "Self-maintenance Codex requires exactly one workspace-write sandbox."
        )
    if approval_values != ["never"]:
        raise WorkerError("Self-maintenance Codex requires approval policy never.")
    if network_values != ["true"]:
        raise WorkerError(
            "Self-maintenance Codex requires explicit workspace sandbox network access."
        )
    return list(codex_args)


def self_maintenance_codex_args(codex_args: list[str]) -> list[str]:
    """Rewrite validated full-access args into a Candidate-only write sandbox."""

    rewritten: list[str] = []
    saw_full_access = False
    index = 0
    while index < len(codex_args):
        argument = codex_args[index]
        if argument == FULL_ACCESS_FLAG:
            saw_full_access = True
            index += 1
            continue
        if argument in {"-C", "--cd", "--add-dir"} or argument.startswith(
            ("-C=", "--cd=", "--add-dir=")
        ):
            raise WorkerError(
                "Self-maintenance Codex must use only the validated Candidate workdir."
            )
        if (
            argument in CONFLICTING_CODEX_FLAGS
            or argument.startswith("--approval-policy=")
            or argument.startswith("--ask-for-approval=")
            or argument.startswith("--sandbox=")
        ):
            raise WorkerError(
                "Self-maintenance Codex received conflicting sandbox/approval arguments."
            )

        if argument in {"-c", "--config"}:
            if index + 1 >= len(codex_args):
                raise WorkerError(f"Codex argument {argument} is missing its value.")
            assignment = _config_assignment(codex_args[index + 1])
            if assignment is not None and _security_config_key(assignment[0]):
                index += 2
                continue
            rewritten.extend((argument, codex_args[index + 1]))
            index += 2
            continue
        if argument.startswith("--config=") or argument.startswith("-c="):
            assignment = _config_assignment(argument.split("=", 1)[1])
            if assignment is not None and _security_config_key(assignment[0]):
                index += 1
                continue

        rewritten.append(argument)
        index += 1

    if not saw_full_access:
        raise WorkerError(
            "Self-maintenance rewrite requires the validated full-access Worker baseline."
        )
    rewritten.extend(WORKSPACE_WRITE_SANDBOX_ARGS)
    return validate_self_maintenance_codex_args(rewritten)


def executor_profile_from_args(
    codex_args: list[str],
    *,
    source: str,
) -> dict[str, str | None]:
    """Describe the executor values explicitly present in Codex argv.

    ``service_tier`` is intentionally reported only when it is present in the
    prepared argv.  This preserves the byte-compatible legacy profile for
    commands that do not request a service-tier override.
    """

    configured_model: str | None = None
    direct_model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    index = 0
    while index < len(codex_args):
        argument = codex_args[index]
        if argument in {"-m", "--model"} and index + 1 < len(codex_args):
            direct_model = codex_args[index + 1]
            index += 2
            continue
        if argument.startswith("--model=") or argument.startswith("-m="):
            direct_model = argument.split("=", 1)[1]
            index += 1
            continue

        assignment: tuple[str, str] | None = None
        if argument in {"-c", "--config"} and index + 1 < len(codex_args):
            assignment = _config_assignment(codex_args[index + 1])
            index += 2
        elif argument.startswith("--config=") or argument.startswith("-c="):
            assignment = _config_assignment(argument.split("=", 1)[1])
            index += 1
        else:
            index += 1

        if assignment is not None:
            key, value = assignment
            if key == "model":
                configured_model = value
            elif key == "model_reasoning_effort":
                reasoning_effort = value
            elif key == "service_tier":
                service_tier = value

    profile: dict[str, str | None] = {
        "model": direct_model if direct_model is not None else configured_model,
        "reasoning_effort": reasoning_effort,
        "source": source,
    }
    if service_tier is not None:
        profile["service_tier"] = service_tier
    return profile


def effective_codex_args(
    default_args: list[str],
    override: Mapping[str, str] | None,
) -> tuple[list[str], dict[str, str | None]]:
    """Build run-local args from a validated command-scoped override.

    Protocol validation belongs to ``protocol_core.command_executor_override``
    and is intentionally performed by the bridge compatibility wrapper before
    this function is called.
    """

    if override is None:
        effective = list(default_args)
        return effective, executor_profile_from_args(
            effective,
            source="worker_default",
        )

    effective: list[str] = []
    override_config_keys = {"model", "model_reasoning_effort"}
    if "service_tier" in override:
        override_config_keys.add("service_tier")
    index = 0
    while index < len(default_args):
        argument = default_args[index]
        if argument in {"-m", "--model"}:
            if index + 1 >= len(default_args):
                raise WorkerError(f"Codex argument {argument} is missing its value.")
            index += 2
            continue
        if argument.startswith("--model=") or argument.startswith("-m="):
            index += 1
            continue

        if argument in {"-c", "--config"}:
            if index + 1 >= len(default_args):
                raise WorkerError(f"Codex argument {argument} is missing its value.")
            assignment = _config_assignment(default_args[index + 1])
            if assignment is not None and assignment[0] in override_config_keys:
                index += 2
                continue
            effective.extend((argument, default_args[index + 1]))
            index += 2
            continue

        if argument.startswith("--config=") or argument.startswith("-c="):
            assignment = _config_assignment(argument.split("=", 1)[1])
            if assignment is not None and assignment[0] in override_config_keys:
                index += 1
                continue

        effective.append(argument)
        index += 1

    effective.extend(
        [
            "-m",
            override["model"],
            "-c",
            f'model_reasoning_effort="{override["reasoning_effort"]}"',
        ]
    )
    if "service_tier" in override:
        effective.extend(
            ["-c", f'service_tier="{override["service_tier"]}"']
        )
    return effective, executor_profile_from_args(
        effective,
        source="command_override",
    )


def add_self_maintenance_prompt_guard(prompt: str) -> str:
    """Append the Candidate-only mutation contract without restricting reads."""

    return prompt + r"""

===== SELF-MAINTENANCE CANDIDATE BOUNDARY =====
This exact run is generating a maintenance Candidate, not deploying it.

- You may READ host files needed to understand and verify the maintenance task.
- WRITE only inside the current Candidate workspace.
- Do not request approval or attempt to expand the writable workspace.
- Do not use -C/--cd/--add-dir to change Codex workspace authority.
- Git status/diff/log and other read-only inspection are allowed.
- Do not commit, push, switch/reset branches, edit .git, deploy, restart, or
  adopt the running Bridge.
- Modify and test the Candidate working tree, then leave it for the trusted
  outer authority to review, commit, and adopt.
"""


def build_prompt(project_id: str, mission: str, command: str) -> str:
    """Build the ordinary one-command Codex prompt."""

    return f"""You are the Codex executor in an unattended AI-Agent-Bridge workflow.

Project id: {project_id}

Rules:
- Execute exactly one bridge command below.
- Treat the mission and command as authoritative task context.
- Do not edit the AI-Agent-Bridge message-bus files themselves.
- Do not broaden the task merely because extra improvements are possible.
- Perform only the validation needed by the command.
- If the command explicitly requires the configured completion-notification mechanism, invoke that mechanism exactly as requested.
- Your final response will be captured verbatim by the local worker and returned to the ChatGPT supervisor.
- In the final response, clearly distinguish verified results, failures, and remaining blockers.
- Do not claim tests, commits, pushes, email delivery, or other actions succeeded unless you verified them.
- For a FINALIZE command, include exactly one machine-readable line:
  BRIDGE_FINAL_JSON: {{"final_delivery":"SUCCESS","completion_email":"SENT"}}
  only if both final delivery and the completion email were actually verified successful.

===== MISSION =====
{mission}

===== COMMAND =====
{command}
"""


def build_phased_prompt(
    project_id: str,
    mission: str,
    runtime: phased_task.RuntimeInspection,
    *,
    bridge_root: Path | None = None,
    base_prompt_builder: Callable[[str, str, str], str] | None = None,
    helper_path: Path | None = None,
) -> str:
    """Build a short bootstrap that points Codex at one current phase only."""

    paths = runtime.paths
    helper = Path(helper_path or __file__).resolve()
    bridge_root_arg = (
        f' --bridge-root "{Path(bridge_root).resolve()}"'
        if bridge_root is not None
        else ""
    )
    bootstrap = f"""This is a phased AI-Agent-Bridge command.

The Worker has already claimed ONE Bridge command. This run is exactly ONE
Codex execution and will produce exactly ONE final Bridge report. Phases are
sequential slices of that one coherent command, not separate commands or
separate Codex sessions. Do not launch another executor.

Authoritative execution-memory files (use these absolute paths):
- global instructions: {paths.global_projection}
- current phase only: {paths.current_phase}
- phase progress: {paths.progress}
- checkpoint helper: {helper}

At the start of this execution, and immediately after any conversation
summary/context compaction or uncertainty about task details:
1. Read global.md.
2. Read current-phase.md.
3. Read phase-progress.json.
4. Treat the current target project's disk state as authoritative.

Do not read task-snapshot.md during normal execution. It is an immutable
recovery/integrity source for the Worker, not a prompt containing all future
work. Do not read or materialize future phase sections. Do not create
phase-A.md, phase-B.md, phase-C.md, or phase-D.md files.

This declaration contains {len(runtime.task.phases)} sequential phase(s).
Complete only the current phase and its focused validation, then run the
checkpoint helper with the current phase id, for example:
python "{helper}" advance --run-dir "{paths.run_dir}"{bridge_root_arg} --phase A

After a successful advance, re-read global.md, current-phase.md, and
phase-progress.json before starting the next phase. Never skip, repeat, or
manually edit phase-progress.json. When current-phase.md contains Final
Acceptance, perform the overall integration/regression/build/artifact checks
required by the command, then run:
python "{helper}" complete-final --run-dir "{paths.run_dir}"{bridge_root_arg}

Only after complete-final succeeds may you output the normal Bridge execution
marker. Do not edit task-snapshot.md or task-manifest.json. Do not invoke the
manual Bridge lane. Preserve the existing project rules and execution-marker
contract supplied by the Worker.
"""
    # The caller can supply the current Worker prompt builder so an installed
    # hardening hook remains visible to phased executions as well as ordinary
    # commands.
    prompt_builder = base_prompt_builder or build_prompt
    return prompt_builder(project_id, mission, bootstrap)


def run_codex(
    *,
    codex_argv: list[str],
    codex_args: list[str],
    workdir: Path,
    output_file: Path,
    prompt: str,
    timeout_seconds: int,
    stdout_log_path: Path | None = None,
    stderr_log_path: Path | None = None,
    final_grace_timeout_seconds: float = 20,
    cleanup_timeout_seconds: float = 10,
    marker_stable_seconds: float = 0.5,
    poll_interval_seconds: float = 0.1,
    max_log_bytes: int = 10 * 1024 * 1024,
    max_final_message_bytes: int = 2 * 1024 * 1024,
    marker_parser: MarkerParser | None = None,
    stop_check: Callable[[], bool] | None = None,
    guard_check: Callable[[], tuple[bool, str]] | None = None,
    guard_interval_seconds: float = 10.0,
    event_logger: Callable[[str, dict[str, Any]], None] | None = None,
    process_launcher: object | None = None,
) -> CodexRunResult:
    """Adapt a prepared Codex invocation to the bounded lifecycle module."""

    stdout_log_path = stdout_log_path or output_file.with_name("stdout.log")
    stderr_log_path = stderr_log_path or output_file.with_name("stderr.log")

    args = [
        *codex_argv,
        *codex_args,
        "-C",
        str(workdir),
        "--output-last-message",
        str(output_file),
        "-",
    ]
    lifecycle_kwargs: dict[str, object] = {
        "args": args,
        "workdir": workdir,
        "output_file": output_file,
        "stdout_log_path": stdout_log_path,
        "stderr_log_path": stderr_log_path,
        "prompt": prompt,
        "execution_timeout_seconds": timeout_seconds,
        "final_grace_timeout_seconds": final_grace_timeout_seconds,
        "cleanup_timeout_seconds": cleanup_timeout_seconds,
        "marker_stable_seconds": marker_stable_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "max_log_bytes": max_log_bytes,
        "max_final_message_bytes": max_final_message_bytes,
        "marker_parser": marker_parser,
        "guard_check": guard_check,
        "guard_interval_seconds": guard_interval_seconds,
        "event_logger": event_logger,
    }
    if stop_check is not None:
        lifecycle_kwargs["stop_check"] = stop_check
    if process_launcher is not None:
        lifecycle_kwargs["process_launcher"] = process_launcher
    return run_codex_process(**lifecycle_kwargs)  # type: ignore[arg-type]
