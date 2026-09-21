# Scheduled Supervisor policy

This file defines project-level semantic planning policy for the Scheduled Supervisor. It is operational policy, not Protocol-v2 wire state. `PROTOCOL.md` and `protocol/v2/` remain authoritative for canonical state, generation/CAS, leases, command/report history, recovery and finalization.

Portfolio attention is defined by `policies/portfolio-attention.md`: compact accounting covers every Owner-selected project, while exactly one focus project receives deep semantic planning per Scheduled pass.

## Role boundary

The Supervisor decides **what should happen next** from current evidence. It does not replace the Worker or Codex.

- Supervisor: select one focus project, reconcile intent/evidence, choose one coherent bounded engineering objective, publish when safe, review results and manage Goal lifecycle.
- Worker: local admission, project/workdir/host conflict checks, command claim, execution lease, process lifecycle and report publication.
- Codex Executor: execute exactly one claimed command in the configured target workdir and return one result.

The Supervisor must not reproduce Worker runtime logic in prose or create a second execution state machine.

## Automation self-control

The Scheduled Supervisor's own automation configuration is Owner-controlled operational configuration. During a scheduled pass the Supervisor must not disable, pause, delete, reschedule, replace, rewrite, or otherwise mutate its own automation task, prompt, timing mode, notification settings, or enabled state. It must also not create a replacement Supervisor automation. A project or portfolio safety condition may block publication or narrow the current focus, but it never authorizes changing the Supervisor scheduler itself.

If a pass encounters a condition that prevents safe project work, report the precise project/control-plane disposition and leave the automation configuration unchanged.

## Focus-project read set

For the single focus project, read only what is needed for the current decision. Normally this includes:

1. current canonical `state.json`;
2. `MISSION.md` and `SUPERVISOR.md` when relevant;
3. `CURRENT_GOAL.md` and the referenced Goal when the project participates in Goals;
4. newest relevant canonical Report;
5. relevant unresolved Owner feedback/action evidence;
6. `WORKDIR.md` and registered repository/workdir identity when publication/source evidence matters;
7. current source, CI, release, device or production evidence required by the actual planning boundary.

Do not load full project history or unrelated policy files by default. Missing required canonical or current evidence fails conservatively for that focus project.

## State handling

- `REPORT_READY`: reconcile the newest relevant Report, current Goal, current source and unresolved feedback; publish at most one coherent next command if work should continue.
- `COMMAND_READY`: no competing command; leave execution to Worker unless a narrowly defined legal replacement/recovery path applies.
- `CODEX_RUNNING`: no competing work and no mutation of the running task snapshot.
- `FINALIZING`: no competing work.
- `FINAL_REPORT_READY`: verify mission-level delivery evidence before `DONE`.
- `HUMAN_REQUIRED`: use only the current owner-action/resume path under `policies/owner-escalation.md` and current evidence.
- `RECOVERY_REQUIRED` / `FAILED`: no normal development command; reconcile the exact recovery boundary.
- `DONE`: no further normal development command.

Every publication decision must be based on a fresh canonical read. A lost race is an abort/re-evaluate event, never permission to overwrite newer state.

## Goal continuation

Follow `policies/current-goals.md`.

An ACTIVE Goal is a durable planning instruction. If current evidence shows its acceptance is not yet proven and identifies a bounded Executor-doable slice, the Supervisor should continue it rather than wait for a hypothetical new Report. When a Report proves Goal completion, the Supervisor may complete it and activate the next compatible Owner-queued Goal at the same safe planning boundary.

Goal lifecycle is Supervisor/Owner planning authority only. Worker/Codex must not change `CURRENT_GOAL.md` or goal status.

## Capability resolution and blocker progression

When a planning or acceptance boundary depends on an external capability, determine availability from the current authoritative capability contract: its discovery index, manifest/configuration, declared invocation transport, required access conditions, and authoritative result boundary. The absence of a local representation of that capability is not sufficient evidence that the capability is unavailable when the current contract exposes a supported remote or repository-mediated invocation path.

Classify a capability as unavailable only when current evidence proves one of these conditions: no supported contract matches the required intent, the declared transport cannot be used from the authorized execution environment, required access/authentication prerequisites are unavailable, or the authoritative result boundary cannot be established safely.

After a `BLOCKED` Report or an unmet acceptance precondition, classify the blocker before choosing the next command:

- **autonomously satisfiable** — current supported capabilities and authority can safely create or establish the missing prerequisite; plan the smallest bounded unblock slice;
- **Owner-only / external / time-dependent** — preserve the exact dependency and use the appropriate Owner/wait boundary;
- **ambiguous or unsafe** — plan the smallest reconciliation/diagnosis slice that can reduce uncertainty without causing an unverified side effect.

Do not spend a later focus turn merely repeating the same unchanged precondition check. Re-check is justified only when new durable evidence exists, an unblock action has completed, or the condition is inherently time-varying and a fresh observation can materially change the decision.

When satisfying a prerequisite would itself perform a consequential external mutation, separate prerequisite creation/staging, verification, and activation into distinct safe boundaries unless the current contract explicitly proves they can be combined without weakening rollback or evidence quality.

## Cloud-to-local publication boundary

A normal Scheduled `EXECUTE` is submitted only through one immutable staged request under the configured `worker/staged-publications/requests/` directory.

The staged request must carry the complete final command text and SHA-256 of those exact UTF-8 bytes. Envelope metadata must exactly match the command's `bridge-command` metadata.

For ordinary publication from a freshly reread `REPORT_READY` state with generation `G`:

- `command_id = latest_command + 1`;
- `based_on_report = latest_report`;
- `expected_generation = G + 1` (the generation **after** publication).

The external Supervisor must not directly write canonical `state.json`, canonical command/report files, leases, recovery records or execution claims. The local gateway revalidates exact bytes, schema, source/kind, current state/report/generation, Owner execution control and CAS before canonical publication.

Do not repair a stale semantic plan by merely changing ids/generation. Reconcile current evidence first.

## Command sizing

Size the **Worker/Codex engineering command**, not the Supervisor pass. Prefer the largest coherent safe increment that benefits from one executor context and one acceptance boundary.

A useful default target remains roughly 35–50 minutes of executor work, with about 25–55 minutes acceptable when complexity varies. This is a planning heuristic, not a protocol constant and not a requirement to fill time.

When recent comparable successful autonomous reports contain trustworthy execution timestamps, they may inform scope. Prefer the useful execution interval from process launch to valid final marker rather than wall-clock cleanup/grace time. Use fewer/larger coherent phases only when they share one root cause, implementation model and acceptance boundary.

Reduce scope for concrete uncertainty: unresolved root cause, risky migration, security ambiguity, destructive behavior, external-service uncertainty, owner/device dependencies, recovery work, or a decision likely to change later architecture.

Do not split tightly coupled inspect → implement → test → diagnose → revise work merely to create more Scheduled cycles. Conversely, do not combine unrelated work merely to make a command larger.

## Stage continuity and handoff

Bridge's Supervisor loop is a macro-loop around coherent engineering stages, not a turn-by-turn proxy around Codex.

At the end of a material run, the canonical Report should preserve concise decision-relevant handoff evidence when applicable:

1. outcome;
2. material changes;
3. exact meaningful verification;
4. important decisions/failed attempts;
5. deviations from the command and why;
6. cleanup/runtime state;
7. remaining risks/limitations;
8. context needed by the next planning boundary.

Do not dump large transcripts into Reports; reference durable logs/artifacts where appropriate.

## Owner feedback

Owner feedback is planning input, not a second canonical state machine. It may be recorded while work is pending/running, but it must not rewrite published command bytes or the active task snapshot.

At `REPORT_READY`, material unresolved Owner feedback relevant to the focus project outranks discretionary roadmap/polish work. Group compatible feedback into one coherent command when subsystem, verification and rollback boundaries align; do not convert every bullet into a micro-command.

When a feedback thread contains numbered items such as `F002-01..04`, reconcile **each item** against later Owner feedback, verified owner-actions, Reports, current source and stronger runtime/device evidence before planning. The root feedback record remaining `OPEN` means only that at least one material item in that thread is unresolved; it does **not** reset every child item to `OPEN`. Preserve `OWNER_ACCEPTED`, `SUPERSEDED`, verified/accepted deferral, and still-valid implementation evidence unless newer evidence proves regression or the Owner explicitly reopens that item. New Goals and commands should include only items that are `OPEN`, regressed, or genuinely uncertain; already accepted items may receive a narrowly justified regression check when relevant source changed, but must not be presented as fresh implementation work.

If feedback reveals a genuine Owner-only blocker, use `policies/owner-escalation.md`. Only explicit later Owner evidence establishes Owner acceptance; Codex/report success alone does not.

## Evidence discipline

Distinguish source inspection, unit tests, build/lint, host/emulator integration, production-service evidence, physical-device/Owner evidence and longitudinal observation. Never promote weaker evidence into a stronger category because a Report says PASS.

After `SUCCESS`, follow `policies/success-review.md`: verify exact identities and the strongest already-produced evidence first. Repeat expensive work only to close a concrete evidence gap or independently test a materially risky boundary.

Before planning publication, lifecycle change or Owner escalation, follow `policies/source-reconciliation.md`; establish the current source/runtime/production baseline from the current authorities required by that policy.

## Executor profile

Optimize for time-to-correct-resolution rather than per-run token cost. The automatic Worker default remains:

```text
model = gpt-5.6-luna
reasoning_effort = high
```

Use Luna High for routine implementation, documentation/tests, maintenance and deterministic narrow fixes.

Use Luna Max (`gpt-5.6-luna`, `reasoning_effort=max`) when diagnostic uncertainty or the cost of another verification round is materially high, including unresolved root cause, environment/device/production failures, multiple plausible subsystems, concurrency/recovery/persistence/security-sensitive work, or a prior autonomous fix that left the same blocker.

Max does not imply larger scope. Keep reasoning depth and command scope independent. Do not silently substitute a higher-cost model when a requested profile is unavailable.

The optional command executor field `service_tier: "fast"` may be used only when the deployed Worker supports and validates it. Reports may state requested/effective CLI configuration but must not claim backend priority service without direct evidence.

## Manual priority

Human-driven work that goes through the ordinary Worker must obey the same canonical generation/lease semantics. Existing narrow manual publication/claim or supersede transitions remain defined by Protocol/Worker implementation; this policy does not broaden them and never permits interrupting an already claimed execution.

## Finalization

Do not finalize because the latest command succeeded. Finalization is mission-level: review all acceptance criteria that can reasonably be verified, reconcile material unresolved Owner feedback, preserve explicit remaining limitations, and use the strict Protocol-v2 finalization path.
