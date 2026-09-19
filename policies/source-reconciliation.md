# Source and milestone reconciliation policy

This policy defines how the Scheduled Supervisor distinguishes historical Bridge evidence from the current real project baseline. It is operational policy, not Protocol-v2 state.

## Core rule

The newest canonical Bridge Report is authoritative evidence about that run, but it is not automatically the newest source, release, deployment, device, or Owner milestone. Human work, manual Codex/CLI sessions, accepted repairs/releases, CI, real-device tests and other approved external work may advance a project between Bridge cycles.

Never rewrite old commands/reports/acceptance records to make history look current.

## Required reconciliation at a semantic planning boundary

Before publishing new executable work, changing Goal lifecycle, or escalating a blocker to Owner:

1. read current canonical project state and newest relevant report;
2. resolve registered repository/workdir/branch identity when execution/source evidence matters;
3. identify the source HEAD and milestone facts that the report/active Goal actually proved;
4. compare those facts with current repository source/history and relevant CI/release/artifact evidence;
5. inspect newer owner feedback/actions and later accepted external/device/production evidence relevant to the same boundary;
6. classify newer evidence as compatible forward progress, benign drift, accepted repair/release/rollout, conflicting/ambiguous, or insufficient;
7. plan from the current proven source + latest proven milestone while keeping old Bridge evidence historical;
8. never fabricate a missing report, command, test, rollout, release, owner action, device result, notification or Goal completion.

Historical `NOT_RUN`, `NOT_STARTED`, helper-not-installed, old version strings, old pause statements, or old runtime capacity values are point-in-time facts only. Search for newer durable evidence before resurrecting them as blockers.

## HUMAN_REQUIRED and owner-action freshness

A canonical `HUMAN_REQUIRED` state is an execution container, not proof that the Owner still has an unperformed action today. On **every** review of `HUMAN_REQUIRED`:

1. identify the exact owner-action thread/blocker that originally justified the state;
2. read the newest event in that thread, not just the immutable root request;
3. inspect newer explicit Owner direction, portfolio pause/resume changes, current Goal state, and later accepted source/runtime/device evidence relevant to the same blocker;
4. distinguish these cases explicitly:
   - `AWAITING_OWNER` and still current -> genuine current Owner dependency;
   - `OWNER_IN_PROGRESS` -> Owner is working on it; do not repeat the request;
   - `OWNER_REPORTED_DONE + PENDING` -> Owner work is reported complete; verification/resume is pending, so do **not** describe the action as still waiting for the Owner;
   - `OWNER_REPORTED_DONE + VERIFIED` -> owner blocker is resolved; any remaining `HUMAN_REQUIRED` container is a resume/publication boundary, not an Owner ask;
   - newer Owner/current evidence makes the historical prerequisite irrelevant or superseded -> do not resurrect the old phase/action as current work; preserve history and record the newer owner/action evidence through the normal append-only mechanism.
5. never ask the Owner to repeat an old privileged/external action solely because an older root event still says `AWAITING_OWNER` after a newer linked event reports completion;
6. if execution/publication is temporarily unavailable, keep canonical state truthful and report `verification-pending` or `resume-pending` rather than falsely reporting `awaiting-owner`.

This rule does **not** authorize bypassing Protocol-v2 resume guards. It prevents stale historical owner evidence from being misclassified as a current human dependency while the legal resume/verification transition waits for its normal execution boundary.

## Planning-state freshness

`supervisor/portfolio.json` is the only current Owner selection/order/pause authority. Project `SUPERVISOR.md`, Goal history, reports, feedback, phase documents, and automation snapshots must not hard-code another project's current pause status.

For a non-paused owner-selected project that participates in current Goals, `CURRENT_GOAL.md` and the referenced ACTIVE goal must be reconciled at the same planning boundary. A queued goal that was waiting only on an Owner portfolio pause should be activated when the Owner explicitly resumes the project, subject to newer compatible Owner direction and current canonical safety constraints.

Operational project context files should describe durable constraints and route to dynamic authorities rather than embedding a point-in-time portfolio state or obsolete phase as the default next action.

## Evidence strength

Keep evidence categories distinct. Source inspection, unit tests, build/lint, host/emulator integration, production-service behavior, physical-device/Owner evidence and longitudinal observation are not interchangeable. A source commit alone does not prove an external/privileged/live action occurred.

Use the strongest evidence required by the Goal/acceptance boundary, following `policies/success-review.md` and project-specific requirements.

## Ambiguity

If current source/evidence advanced but intent, safety or provenance is ambiguous, do not reset/rebase/overwrite it and do not blindly continue a stale plan. Stop only that project's publication and perform the smallest safe reconciliation/diagnosis. Unrelated portfolio projects continue normally.

## Production baseline index

An explicitly configured deployment-baseline index in Private State may reference already accepted milestones. The concrete path and current deployment facts belong to the private instance, not to the Public Engine. Every entry must include an `as_of` time plus exact durable evidence references (commit/report/acceptance path as applicable).

The baseline index is not a live authority. It cannot replace current canonical state, current source HEAD/history, Worker local config, live capacity/admission, current CI/release/device evidence, or publication-time revalidation.

## Offline Worker

This policy applies even when Worker execution is unavailable. Source/report/Goal/Owner semantic review remains useful offline; only executable publication is gated by `policies/worker-availability.md`.
