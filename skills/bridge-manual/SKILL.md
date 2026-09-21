---
name: bridge-manual
description: Explicit-only manual fast lane for an AI-Agent-Bridge goal. Claims the bridge execution lease before a human-driven Codex turn changes files, records the manual command, and publishes the final report afterward.
---

# bridge-manual

This skill is **opt-in only**. Never invoke it because a task merely looks related to AI-Agent-Bridge.

Use it only when the user explicitly invokes `$bridge-manual` or an applicable project instruction explicitly requires this skill.

## Purpose

This skill lets a human accelerate an existing Bridge goal with a manually obtained ChatGPT Supervisor command without creating a race with the continuous Worker or the Scheduled ChatGPT Supervisor.

The core invariant is:

> **Do not touch the target project until the manual execution lease has been successfully claimed in GitHub.**

## Invocation

Expected user form:

```text
$bridge-manual <project-id>

<the ChatGPT Supervisor command to execute>
```

The `<project-id>` is the directory name under `<State>/projects/`.

If the project id is missing or ambiguous, ask for it before making task changes. Do not guess a goal directory.

## Locate Engine and State independently

Locate the installed Public Engine containing `worker/bridge_manual.py` and `PROTOCOL.md`. Do not infer its location from the product working directory. Require an explicit absolute `AI_AGENT_BRIDGE_STATE_ROOT` or `--state-root` for the separate Private State repository. State must contain `bridge-state.json`, `projects/`, `supervisor/` and an independent `.git` directory. Never use a linked worktree, bundled fixture, running Engine or product repository as State.

The private local binding must pin the original Owner execution-control location. Missing or mismatched binding fails closed. If the helper or binding is missing, stop: do not emulate protocol writes manually. Only the original CAS/lease gateway may claim a command. Self-maintenance against Engine remains disabled pending independently accepted containment; this manual lane must not bypass that restriction or claim that an unrestricted same-user process is isolated from State.

## Step 1 — Preserve the manual Supervisor command

Before changing any target file, place the command portion of the user's request into a temporary UTF-8 text file.

Do not include unrelated chat text, your own analysis, or secrets.

Normally use `kind=EXECUTE`.

Use `kind=FINALIZE` only when the manual Supervisor command explicitly says the Bridge mission has met its acceptance criteria and this is the final delivery command. A FINALIZE command must obey the Bridge finalization contract and normally invokes the configured completion-notification mechanism.

## Step 2 — Claim the distributed execution lease

Run:

```text
python <Engine>\worker\bridge_manual.py --state-root <State> start --project <project-id> --command-file <temp-command-file> --kind EXECUTE
```

or, for an explicit final delivery:

```text
python <Engine>\worker\bridge_manual.py --state-root <State> start --project <project-id> --command-file <temp-command-file> --kind FINALIZE
```

The helper uses a temporary Git clone and a generation/CAS check. It can safely race with the continuously running Worker.

A successful claim prints exactly one machine-readable line beginning with:

```text
BRIDGE_MANUAL_CLAIM:
```

Record these returned fields for this turn:

- `command_id`
- `run_id`
- `claimed_generation`
- `based_on_report`
- `kind`
- `lease_expires_at`

Proceed only if the helper exits successfully and the returned state is `CODEX_RUNNING`.

If the claim fails because another Codex run is active, the state changed, finalization is in progress, human recovery is required, or another non-supersedable command is canonical: **do not execute the task**. Report the conflict to the user.

An unclaimed normal `scheduled_chatgpt` command may be superseded by this manual lane. The helper records that fact; the superseded command file remains immutable.

## Step 3 — Execute exactly the claimed manual command

After the lease is confirmed, execute the user's manual Supervisor command.

Rules:

- Treat the claimed command as the task scope.
- Do not broaden the task merely because additional improvements are possible.
- Do not modify AI-Agent-Bridge message-bus files by hand.
- The target project may be outside the current working directory when the current Codex session has the required permissions.
- Perform only the validation needed to support the result.
- Do not start a second Bridge/Codex execution for the same project while this lease is active.
- If this is a FINALIZE command, obey its completion-notification requirements and emit the strict `BRIDGE_FINAL_JSON` line only when final delivery and notification delivery were actually verified successful.

## Step 4 — Compose the execution report

Before the user-facing final response, compose a concise but complete report containing:

- what was actually changed or done;
- important files/areas touched;
- tests or validation and results;
- repository/branch/commit when verifiable and relevant;
- blockers or remaining limitations;
- whether human input is required.

Distinguish verified facts from suggestions.

Write this report to a temporary UTF-8 text file.

Choose one outcome:

- `SUCCESS`: the claimed command was completed and its required validation passed;
- `PARTIAL`: useful progress was made but the command is not fully complete;
- `FAILED`: the command could not be completed.

## Step 5 — Publish the report and release the lease

Using the exact identifiers returned by Step 2, run:

```text
python <Engine>\worker\bridge_manual.py --state-root <State> finish \
  --project <project-id> \
  --run-id <run-id> \
  --command-id <command-id> \
  --claimed-generation <claimed-generation> \
  --outcome <SUCCESS|PARTIAL|FAILED> \
  --report-file <temp-report-file>
```

On Windows/PowerShell, adapt line continuation syntax as needed; the arguments themselves must remain unchanged.

A successful publication prints a machine-readable line beginning with:

```text
BRIDGE_MANUAL_REPORT:
```

Normally the resulting project state is `REPORT_READY`. A successful explicit FINALIZE command with the strict verified final marker may produce `FINAL_REPORT_READY`.

## Publication failure rule

If the Codex work already finished but `finish` cannot safely publish because GitHub/state changed:

- do **not** rerun the task;
- do **not** overwrite the newer Bridge state;
- do **not** fake a successful handoff;
- preserve the helper's pending-report diagnostic under the Bridge runtime directory;
- tell the user that execution finished but Bridge report publication requires recovery.

## Cleanup

Temporary command/report files may be deleted after successful publication. Canonical `commands/` and `reports/` entries are append-only and are never cleanup targets for this skill.

## Default final response

After successful publication, return the same substantive execution report to the user and add a short note that the manual Bridge report was published, including the Bridge `command_id` when useful.

## Split repository binding

Engine source and State authority are separate. Require an absolute `AI_AGENT_BRIDGE_STATE_ROOT`. Invoke `python <Engine>/worker/bridge_manual.py --state-root <State> ...`. Never infer State from the product workdir or mutate bundled fixtures.
