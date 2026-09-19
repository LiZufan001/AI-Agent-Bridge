# Protocol v2 owner feedback

This document defines Bridge-wide owner-feedback intake semantics for human product, UX, bug, priority, and acceptance feedback that should influence future autonomous planning without creating a canonical pause.

It is an additive Protocol v2 maintenance rule. It does not change `protocol_version`, canonical `state.json`, generation/CAS, execution lease, Worker claim/report behavior, or the no-auto-rerun contract.

## Purpose and separation from owner actions

Owner feedback is ordinary human direction such as UI/UX complaints, observed bugs or performance problems, feature requests, product-priority changes, acceptance feedback after trying a build, or requests to revise wording, navigation, visual language, and interaction behavior.

It is not the same thing as `owner-actions/`. Use `owner-actions/` plus `HUMAN_REQUIRED` only when safe autonomous progress is genuinely blocked on a human-only action, decision, physical-device step, or other owner-only evidence.

Ordinary owner feedback does not by itself change canonical state and must not set `HUMAN_REQUIRED` merely because a human supplied it.

## Project location

Projects that receive owner feedback use:

```text
projects/<project-id>/owner-feedback/
├── README.md
├── feedback-001.md
└── ...
```

Feedback history is non-canonical. `state.json` remains the only canonical project pointer.

Feedback records are append-only. Do not rewrite an older feedback record to change what the owner originally said. Clarifications, corrections, rejection, acceptance, or resolution updates use a new feedback event.

Feedback ids are monotonically increasing within a project; gaps are allowed.

## Event/thread model

A feedback event should identify itself and, when it updates an earlier event, link the thread explicitly. Recommended fields are:

```yaml
feedback_id: feedback-002
root_feedback_id: feedback-001
relates_to: feedback-001
recorded_at: 2024-01-02T00:00:00Z
source: owner
applies_after_command: 7
status: OPEN
summary: Owner reports a product/UI defect after trying the current build.
```

For a first event, `root_feedback_id` and `relates_to` may be omitted.

A feedback record may contain several clearly numbered item ids such as `F001-01`, `F001-02`, and so on. Later feedback events, commands, and reports should cite those item ids when practical so planning and acceptance remain traceable.

Recommended item lifecycle values are:

```text
OPEN -> PLANNED -> IMPLEMENTED -> OWNER_ACCEPTED
```

Terminal alternatives are `REJECTED` and `SUPERSEDED`. These are planning/evidence labels only; they are not canonical Bridge states.

The root feedback event's status is not a shortcut for every numbered item's status. A root/thread may remain `OPEN` because one child item remains unresolved while sibling items are already `IMPLEMENTED`, `OWNER_ACCEPTED`, `SUPERSEDED`, or deliberately deferred with explicit Owner evidence. Later planning must preserve those item-level outcomes unless newer evidence proves regression or the Owner explicitly reopens the item.

Never infer `OWNER_ACCEPTED` merely because Codex or a report says the implementation succeeded.

## In-flight command immutability

Owner feedback may be recorded while canonical state is `COMMAND_READY`, `CODEX_RUNNING`, or `FINALIZING`, but it must not mutate, replace, append to, or otherwise inject new work into the already-published or already-claimed command.

When a command has been claimed, its canonical command bytes and runtime task snapshot remain frozen. No late feedback is added to its phase files or final-acceptance body, no competing command is published, and no execution lease, generation, or state field changes merely because feedback arrived.

Feedback that arrives after a command is published applies at the next safe planning boundary unless the current command independently happens to address the same issue. The next Supervisor pass must still reconcile the owner's feedback against the completed report rather than assuming it was satisfied.

`applies_after_command` may be used to make this boundary explicit.

## Supervisor consumption rule

At a normal `REPORT_READY` planning boundary, the Supervisor must read relevant unresolved owner-feedback threads before choosing autonomous roadmap or polish work.

Owner feedback has priority over discretionary autonomous polish when it is compatible with mission constraints and current evidence. It does not override security, destructive-operation safeguards, canonical state/CAS/lease rules, physical-device or production-evidence boundaries, correctness constraints, or explicit mission invariants.

For a multi-item feedback thread, the Supervisor must first build an item-level evidence map from later feedback, verified owner-actions, canonical Reports, current source and the strongest relevant runtime/device evidence. Only `OPEN`, regressed, or genuinely uncertain items are candidates for new work. Root feedback `OPEN` must never be used to re-open already accepted siblings. A later source change may justify a narrow regression check on a previously accepted item when that source change materially touches the accepted boundary, but the command/report must describe that as regression verification rather than new implementation work.

The Supervisor should group compatible unresolved feedback items into the next largest coherent safe command under the normal sizing policy. It must not blindly create one tiny command per feedback bullet when several unresolved items share the same subsystem, verification path, and rollback boundary.

If feedback exposes an unresolved root cause, safety ambiguity, or owner-only dependency, the Supervisor may first publish diagnosis or verification work or enter the existing owner-action path when justified.

When a new command intentionally addresses feedback, its human-readable body should name the relevant feedback file/item ids. This maintenance design does not add a new required field to `bridge-command` metadata or change `command.schema.json`.

## Acceptance and closure

Executor success is implementation evidence, not product acceptance.

If the owner later tries the result and accepts or rejects it, record a new feedback event rather than rewriting the original intake. A rejected implementation returns the relevant item to actionable planning with the newer owner evidence.

Supervisor finalization must not silently ignore unresolved owner feedback that materially conflicts with mission acceptance. Minor discretionary suggestions may remain open only when the final report explicitly identifies them as remaining limitations or the owner has accepted deferral.

## Safety and privacy

Owner-feedback files must contain only non-sensitive project direction and evidence summaries. Do not use this channel for private authentication material or other secrets.
