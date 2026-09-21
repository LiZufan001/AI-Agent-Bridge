# Scheduled Supervisor entrypoint

> Repository boundary: code, schemas and generic policy are in Engine. All `projects/`, concrete `supervisor/*.json`, registry and staged publication paths in this document are relative to the explicitly bound **State** root. Read Engine policy from an independently accepted immutable Engine revision; never infer State from Engine working directory. Protocol v2 semantics below are unchanged.

This file is the durable bootstrap/index for the external Scheduled Portfolio Supervisor. It is operational policy routing, not Protocol-v2 wire state and not a second state machine.

## Authority by domain

`PROTOCOL.md` and `protocol/v2/` define canonical workflow transitions. Explicit Owner controls govern project selection, pause and execution permission. Canonical State and current source/runtime evidence determine current facts; Mission, Goals and the newest applicable Owner records determine intent in their own domains.

The Supervisor decides what should happen next. The Worker owns local admission and real execution. Codex executes one claimed command in the Worker's registered environment. Do not blur those responsibilities.

## Pass shape

Every Scheduled pass has one deep focus project and one coherent bounded planning intent.

First perform compact portfolio accounting, then choose the single focus project under `policies/portfolio-attention.md`. Ordinary focus selection is stateless round-robin over the currently eligible Owner-selected projects. Narrow safety/recovery/new-Owner-evidence conditions may preempt that rotation. Only the chosen focus project may receive Goal/Report/source/feedback/CI body reads during the pass.

The Supervisor should adapt its pacing to the actual task and tool/runtime window. Repository policy intentionally does not encode guessed minute-by-minute checkpoints, arbitrary body-file/byte quotas, or a persistent attention queue.

## Mandatory compact read

For every pass:

1. Read `supervisor/bootstrap.json`.
2. Read the fixed durable Owner execution control. Missing/invalid control forbids new execution.
3. Read the configured fixed Worker heartbeat and compute only `worker_available=true|false` under `policies/worker-availability.md` using current time.
4. Read `supervisor/portfolio.json`.
5. Read the current `projects/<project-id>/state.json` for every `owner_selected=true` project and derive compact attention facts.
6. For any project whose canonical state is `HUMAN_REQUIRED`, perform the bounded owner-action freshness check required by `policies/portfolio-attention.md`: identify the current linked blocker thread and read only the newest linked owner-action event/status needed to distinguish a genuine current Owner dependency from `OWNER_REPORTED_DONE/PENDING`, `OWNER_REPORTED_DONE/VERIFIED`, or a prerequisite that current Owner evidence has superseded. This is compact classification, not permission to deep-read every project's Goal/Report/source/CI/feedback evidence.
7. Build the ordinary eligible ring in the existing `priority_rank` order and choose exactly one focus using the current UTC hour bucket, unless a narrow preemption condition in `policies/portfolio-attention.md` applies.
8. For the focus only, read the current Goal, relevant canonical Report, current source/CI/release evidence, feedback/actions and project context needed for one bounded intent.
9. Apply any explicit Automation-level emergency Owner override. The default is none; never invent one.

Do not use conversational memory or a derived snapshot as canonical authority. Worker registry `enabled` and Owner portfolio selection/pause are separate authorities; `worker/remote-projects.json` does not define which projects the Scheduled Supervisor manages.

## Focus selection and non-focus projects

Follow `policies/portfolio-attention.md`.

`priority_rank` defines only the stable ring order for ordinary scheduling. It is not a project importance score and does not give a project extra ordinary focus turns. An unfinished Goal, a fresh ordinary SUCCESS Report, or repeated availability of another slice does not override the round-robin slot.

A non-focus project may legitimately contain an unreviewed report, unfinished Goal, or resolved-owner `HUMAN_REQUIRED` resume/verification boundary until a later rotation slot. It must still receive a concise disposition based on compact current facts so intentional deferral is distinguishable from omission.

Typical compact classifications include pending/running work, genuine owner pause/block, resolved-owner verification/resume pending, recovery/failed state, terminal state, or eligible work deferred to a later rotation slot. Canonical `HUMAN_REQUIRED` by itself is not enough to label a project `blocked-owner`; the newest linked owner-action status controls that semantic classification.

## Worker availability and offline behavior

Worker availability gates execution/publication, not read-only reasoning.

When `worker_available=false`, the Supervisor still uses the same focus rotation/preemption rules and may perform repository-grounded read-only review for that one focus project: inspect the relevant Report/Goal/source/CI/feedback, reconcile current facts, classify blockers and produce a concise advisory next intent.

Offline review is best-effort and non-canonical. It may be lost or recomputed. While the Worker is unavailable, do not:

- mutate product source;
- change Goal lifecycle;
- allocate or consume a command id;
- create final executable command bytes;
- create a staged publication/transition request;
- mutate canonical state;
- perform Worker-dependent notification execution.

When the Worker later becomes available, plan from fresh current evidence and perform the normal publication preflight. An earlier advisory is only a hint.

## Goal and evidence reconciliation

For the focus project, use `policies/current-goals.md` to determine the current product outcome and lifecycle. Use `policies/source-reconciliation.md` before publishing work, changing Goal lifecycle or escalating to the Owner. A Report proves the run and evidence it binds; current source, release, device and production facts must be established from their current authorities.

For a focus project still canonical `HUMAN_REQUIRED`, first apply the latest owner-action reconciliation rules from `policies/source-reconciliation.md` and `policies/owner-escalation.md`. If the Owner action is reported done or verified, perform only the smallest legal verification/resume work and never ask the Owner to repeat the resolved action merely to clear the canonical container.

After a Worker/Codex `SUCCESS`, follow `policies/success-review.md`: verify the strongest existing exact evidence needed for the boundary rather than reflexively re-running the executor's entire test suite. Escalate review depth only when consequence or evidence uncertainty justifies it.

If the ACTIVE Goal remains incomplete and current evidence identifies a bounded Executor-doable slice, that is valid work for the project's next ordinary rotation slot unless a narrow preemption condition applies. Avoid the deadlock `ACTIVE Goal -> wait for new Report -> no command -> no new Report` without letting one long Goal monopolize Scheduled attention.

When a focus is blocked by a missing prerequisite or required capability, apply `policies/supervisor.md` capability-resolution and blocker-progression rules before deciding that work cannot continue. A later focus should advance an autonomously satisfiable blocker through the smallest safe unblock slice rather than repeat the same unchanged precondition check.

## Publication boundary

Only publish for the focus project when current evidence establishes useful executable work, canonical state permits it, `worker_available=true`, and durable Owner execution control is valid `auto`.

Immediately before materializing a staged request:

1. re-read heartbeat and Owner execution control;
2. re-read the focus project's canonical state and latest relevant Report/feedback/action;
3. re-read the current source/CI/release evidence required by the actual command;
4. ensure there is no active execution conflict or unreconciled external side effect;
5. derive command identity and immutable bytes from that fresh decision;
6. use only the existing staged publication gateway.

For a normal `REPORT_READY` publication from generation `G`, the staged command's `expected_generation` is the post-publication generation `G+1`; `command_id=latest_command+1` and `based_on_report=latest_report`. Envelope and command metadata must agree exactly.

For a legal `HUMAN_REQUIRED -> COMMAND_READY` resume, follow the existing Protocol-v2 owner-action recovery transition and its current guards rather than treating the project as an ordinary `REPORT_READY` publication. Do not directly edit canonical state merely because the Owner blocker is resolved.

The external Supervisor must not directly mutate canonical command/state. Exact bytes/hash, schema, generation/CAS, `based_on_report`, staged expiry, claim/lease, same-project exclusion, host concurrency, repository/path conflicts and recovery enforcement remain deterministic gateway/Worker boundaries.

A lost race or changed evidence stops that publication. Never force/reset/overwrite, lower generation, clear an active run, or retry a stale semantic plan merely by adjusting ids/generation.

## CI evidence

Collect CI only when it is relevant to the focus intent or acceptance boundary. Preserve exact run identity, attempt, workflow path, head SHA and conclusion as needed. Equivalent supported GitHub Actions collections may be used when one endpoint shape is unavailable; missing/truncated evidence remains UNKNOWN rather than an invented empty set.

## Owner escalation

Follow `policies/owner-escalation.md`. Escalate only a genuine current Owner-only dependency. The notification-before-`HUMAN_REQUIRED` safety rule remains binding until a separately accepted deterministic replacement exists.

## Output contract

Every pass must report:

- heartbeat evidence, `worker_available`, Owner execution mode and publication permission;
- the actual Owner-selected portfolio ring order/pause state;
- whether focus came from ordinary rotation or narrow preemption;
- for ordinary rotation, the eligible ring and selected `rotation_index`;
- the chosen focus project and the bounded intent/review completed there;
- one concise disposition for every Owner-selected project, including explicit `deferred-this-pass` when a non-focus eligible project was intentionally left for later;
- Goal/feedback/action conclusion when relevant;
- publication outcome or exact no-publication reason;
- if offline, the concise non-canonical advisory intent;
- any genuine remaining Owner/recovery/fresh-evidence dependency.

The Supervisor is a periodic planner. End the pass after one coherent focus boundary; never wait for Worker/Codex completion inside the Scheduled task and never start a second deep project simply because time remains.

Use `NO_NOVEL_WORK_AVAILABLE` only when compact portfolio accounting plus the completed focus review supports that conclusion without unknown relevant evidence. Otherwise report the precise current disposition.
