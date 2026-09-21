# Current baseline reconciliation policy

This policy defines how the Scheduled Supervisor establishes the current real project baseline before planning or publication. It is operational policy, not Protocol-v2 state.

## Core rule

The newest canonical Bridge Report is authoritative evidence for the run it binds. The current project baseline is established from canonical State, registered repository/workdir identity, current source HEAD, the newest applicable Owner evidence, and relevant current CI/release/artifact/device/production evidence.

Commands, Reports, Owner records and acceptance records are append-only evidence and remain immutable.

## Required reconciliation at a semantic planning boundary

Before publishing executable work, changing Goal lifecycle, or escalating an Owner dependency:

1. read current canonical project State and the newest relevant Report;
2. resolve the registered repository/workdir/branch identity when source or execution evidence matters;
3. identify the source HEAD and milestone facts established by the Report and ACTIVE Goal;
4. read the current repository source/history and relevant CI/release/artifact evidence;
5. read the newest applicable Owner feedback/action and accepted external/device/production evidence for the same boundary;
6. classify the combined evidence as aligned current state, compatible forward progress, benign drift, accepted repair/release/rollout, conflicting/ambiguous, or insufficient;
7. plan from the current proven source and strongest proven milestone;
8. never fabricate a report, command, test, rollout, release, owner action, device result, notification or Goal completion.

A status string or capability claim is current only when its binding evidence is current for the decision being made.

## HUMAN_REQUIRED and owner-action freshness

A canonical `HUMAN_REQUIRED` state is an execution container. On every review:

1. identify the exact owner-action thread/blocker bound to the state;
2. read the newest linked event in that thread;
3. read current Owner direction, portfolio pause/resume state, current Goal state, and accepted source/runtime/device evidence relevant to that blocker;
4. classify the current dependency:
   - `AWAITING_OWNER` and still current -> genuine Owner dependency;
   - `OWNER_IN_PROGRESS` -> Owner is working on it;
   - `OWNER_REPORTED_DONE + PENDING` -> verification/resume is pending;
   - `OWNER_REPORTED_DONE + VERIFIED` -> the Owner dependency is resolved and any remaining container is a legal resume/publication boundary;
   - current durable Owner evidence that resolves or supersedes the prerequisite -> reconcile from that evidence through the append-only owner-action mechanism;
5. ask the Owner only when the newest exact evidence proves a current Owner-only action is still required;
6. when execution/publication is unavailable, keep canonical State unchanged and report the exact verification/resume dependency.

Protocol-v2 resume guards remain authoritative.

## Planning-state freshness

`supervisor/portfolio.json` is the Owner selection/order/pause authority. For a participating non-paused project, `CURRENT_GOAL.md` and its referenced ACTIVE Goal are reconciled at the same planning boundary.

Operational context files describe durable constraints and route mutable decisions to their current authorities.

## Evidence strength

Keep evidence categories distinct. Source inspection, unit tests, build/lint, host/emulator integration, production-service behavior, physical-device/Owner evidence and longitudinal observation are not interchangeable. Use the strongest evidence required by the Goal/acceptance boundary under `policies/success-review.md` and project-specific requirements.

## Ambiguity

If current evidence conflicts or cannot establish a safe next action, stop only that project's publication and perform the smallest safe reconciliation/diagnosis. Unrelated portfolio projects continue normally.

## Production baseline index

An explicitly configured deployment-baseline index in Private State may reference accepted milestones. The concrete path and deployment facts belong to the private instance. Each entry includes an `as_of` time plus exact durable evidence references.

The baseline index is a read-only aid. Publication decisions still re-read canonical State, source HEAD/history, Worker local config, live admission/capacity and relevant CI/release/device evidence.

## Offline Worker

This policy also applies when Worker execution is unavailable. Source/report/Goal/Owner semantic review remains useful offline; executable publication is gated by `policies/worker-availability.md`.
