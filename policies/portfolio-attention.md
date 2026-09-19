# Portfolio attention policy

This file defines durable attention rules for the Scheduled Portfolio Supervisor. It is operational policy, not Protocol-v2 wire state. Canonical `state.json`, generation/CAS, command/report history, Worker admission and runtime concurrency remain authoritative in their existing layers.

Owner planning selection/rotation/pause comes from `supervisor/portfolio.json`. Worker registration/execution `enabled` in `worker/remote-projects.json` is a separate host-runtime concern and must not be treated as Owner portfolio selection.

## One focus project per pass

Every Scheduled pass performs two different levels of work:

1. **compact portfolio accounting** for every `owner_selected=true` project;
2. **deep semantic planning** for exactly one focus project.

Compact accounting reads the current canonical state and only the minimal control facts needed to classify attention. For a project whose canonical state is `HUMAN_REQUIRED`, compact accounting has one bounded exception: read only the newest linked owner-action event/status needed to determine whether the Owner dependency is still current. Do not expand that exception into Goal/Report/source/CI/feedback deep reads for every project.

After accounting, choose one focus project under the round-robin rules below. Deep-read only that project's evidence and pursue one coherent bounded intent. Non-focus eligible projects remain valid work for later passes and must not be deep-read merely to make the report look complete.

## Ordinary round-robin focus

Ordinary development is **round-robin first**, not priority first.

The existing `priority_rank` field in `supervisor/portfolio.json` is retained for compatibility and now defines only the stable ring order. A smaller `priority_rank` does **not** mean that project deserves more Scheduled focus time and does not outrank another ordinary eligible project.

Build the ordinary eligible set from compact current facts:

- include only `owner_selected=true` and `owner_paused=false` projects;
- `REPORT_READY` / `FINAL_REPORT_READY` with potentially reviewable or unfinished work are ordinary eligible;
- `COMMAND_READY` / `CODEX_RUNNING` / `FINALIZING` are already pending/running and are not ordinary focus candidates;
- for `HUMAN_REQUIRED`, read the newest linked owner-action event before classifying eligibility:
  - current `AWAITING_OWNER` or `OWNER_IN_PROGRESS` remains outside ordinary rotation;
  - `OWNER_REPORTED_DONE + PENDING` is no longer blocked on the Owner and is ordinary eligible for bounded verification/reconciliation;
  - `OWNER_REPORTED_DONE + VERIFIED` is no longer blocked on the Owner and is ordinary eligible for Protocol-legal resume/reconciliation;
  - newer durable Owner evidence that makes the historical prerequisite irrelevant or superseded is likewise eligible for bounded reconciliation;
- `DONE` is terminal and is not an ordinary focus candidate;
- `RECOVERY_REQUIRED` / `FAILED` and uncertain side-effect states are handled by the preemption rules below rather than ordinary rotation.

A newly arrived explicit Owner direction/control event may still qualify for narrow preemption under the rules below. Already-recorded resolved owner evidence does not remain a permanent preemption: once compact reconciliation shows the Owner dependency is no longer outstanding, the project participates in ordinary rotation until selected for its bounded verification/resume work.

Sort the ordinary eligible set by `priority_rank`. Let `N` be its size. The Scheduled task is hourly, so use the authoritative UTC hour bucket as the stateless rotation cursor:

```text
rotation_bucket = floor(unix_timestamp_utc / 3600)
rotation_index  = rotation_bucket mod N
focus           = ordinary_eligible[rotation_index]
```

This cursor is derived from current time and therefore creates no new persistent control state, cursor file, Git commit, lease, queue or acknowledgement protocol. With a stable eligible set, each project receives exactly one ordinary focus during each `N`-pass rotation cycle. If a Scheduled pass is missed, the next pass uses the current bucket rather than replaying old attention.

An unfinished ACTIVE Goal, a newly completed ordinary Report, repeated availability of another bounded slice, or project identity does not let one project consume extra ordinary focus slots outside this rotation.

If the eligible set changes because a project becomes paused, terminal, running/pending, genuinely blocked, or newly eligible after owner-action reconciliation, recompute the ring from fresh compact facts for that pass. Do not preserve a stale ring or reserve a future slot.

## Narrow preemption rules

Round-robin may be preempted only by a current condition whose delay would make safe operation or Owner intent materially worse. Valid preemption classes are:

- `RECOVERY_REQUIRED`, ambiguous execution, or an unknown external side effect that must be reconciled before safe continuation;
- an active safety/control-plane integrity problem that can make further execution unsafe;
- explicit **new** Owner direction/control evidence that changes what is permitted or resolves/creates an Owner-only blocker.

Ordinary correctness work, normal security hardening inside an ACTIVE Goal, former priority semantics, project name, conversational habit, an unfinished Goal, a fresh ordinary SUCCESS Report, or already-recorded owner-action completion awaiting routine verification/resume are **not** preemption reasons.

If multiple current preemption candidates exist, choose the one with the strongest immediate safety/Owner consequence; use `priority_rank` only as a deterministic tie-breaker among otherwise equivalent preemption candidates. After the preempting condition is resolved, return to the time-derived ordinary rotation. Do not create compensation passes or a second queue.

## Compact attention classification

At minimum:

- `REPORT_READY` / `FINAL_REPORT_READY` with work requiring semantic review -> ordinary eligible candidate;
- `COMMAND_READY` / `CODEX_RUNNING` / `FINALIZING` -> `running/pending-worker` unless a narrow preemption condition exists;
- `HUMAN_REQUIRED` + newest linked event still `AWAITING_OWNER` / `OWNER_IN_PROGRESS` -> genuine owner dependency, no ordinary deep review;
- `HUMAN_REQUIRED` + `OWNER_REPORTED_DONE/PENDING` -> ordinary eligible verification/reconciliation candidate, not `blocked-owner`;
- `HUMAN_REQUIRED` + `OWNER_REPORTED_DONE/VERIFIED` -> ordinary eligible resume/reconciliation candidate, not `blocked-owner`;
- `RECOVERY_REQUIRED` / `FAILED` -> recovery/ambiguous preemption candidate;
- `DONE` -> accounted/terminal unless contradictory current evidence exists;
- owner-paused -> `paused-owner`, no ordinary development.

A non-focus project with `latest_report > last_reviewed_report`, or a reconciled `HUMAN_REQUIRED` project awaiting its later rotation slot, may remain semantically unreviewed until that slot. That is intentional deferral, not a protocol defect. Never fake a review result from compact state alone.

## Focus project behavior

For the focus project, read the current Goal, relevant Report, source/CI/release evidence, feedback/actions and project context necessary to make the current decision. Follow `policies/current-goals.md`, `policies/source-reconciliation.md`, `policies/success-review.md` and `policies/owner-escalation.md` only as the boundary requires.

If the focus project is canonical `HUMAN_REQUIRED` but compact owner-action reconciliation showed that the Owner dependency is no longer outstanding, perform the smallest legal verification/resume reconciliation. Do not invent a new state, bypass Protocol-v2 resume guards, or ask the Owner to repeat a resolved action.

A focus review should reach one concrete outcome:

1. useful executable work is justified and may proceed to publication when gates permit;
2. semantic review is complete but execution is unavailable, so record a concise advisory intent;
3. current evidence proves an intentional no-op/completion/lifecycle transition;
4. a genuine Owner/recovery/external dependency is identified;
5. evidence remains ambiguous/unknown and safe continuation cannot be derived.

After reviewing a successful Report, reconcile the ACTIVE Goal. If the Report completes the Goal, the Supervisor may complete it and activate the next compatible Owner-queued Goal at the same safe planning boundary. If the ACTIVE Goal remains incomplete and has a bounded Executor-doable slice, that slice remains valid work for the project's **next rotation slot** unless a narrow preemption condition applies.

## Worker availability

Follow `policies/worker-availability.md`.

`worker_available=false` closes Worker-dependent publication/execution only. The Supervisor still applies the same focus rotation and may perform useful read-only semantic review for that one focus project. The offline result is advisory and non-canonical; it does not reserve future execution.

When the Worker is unavailable, do not allocate a command id, generate final executable bytes, create a staged request, mutate Goal lifecycle, mutate product source or mutate canonical state.

## Required per-pass coverage

Before ending a Scheduled pass, give one concise disposition for every Owner-selected project. Recommended vocabulary:

- `planned / staged-input-submitted` — the focus produced a staged input but canonical publication has not yet been observed;
- `reviewed/deferred-execution` — focus review completed but Worker/publication is unavailable;
- `reviewed/no-op` — focus evidence was reviewed and current work is not warranted, with a concrete reason;
- `running/pending-worker` — immutable work is already pending/running/finalizing;
- `blocked-owner` — a genuine current Owner-only dependency exists;
- `verification-pending` — Owner work is reported done but technical verification remains;
- `resume-pending` — Owner work is verified/resolved but Protocol-legal resume/publication has not yet completed;
- `paused-owner` — explicit Owner pause remains in force;
- `recovery/ambiguous` — safe continuation requires recovery or unresolved evidence;
- `deferred-this-pass` — eligible or potentially reviewable non-focus project intentionally left for its later rotation slot;
- `accounted/terminal` — compact state is terminal and needs no current deep work.

Do not use a non-focus disposition to imply semantic review that did not occur. In particular, do not label `OWNER_REPORTED_DONE/PENDING` or `OWNER_REPORTED_DONE/VERIFIED` as `blocked-owner` solely because canonical state still says `HUMAN_REQUIRED`.

## Output discipline

The human-readable result must make the one deep focus obvious, state whether focus came from ordinary rotation or narrow preemption, and distinguish intentional deferral from accidental omission. Give detail for the focus project; keep non-focus dispositions compact and grounded only in the facts actually read.

For ordinary rotation, report the eligible ring in `priority_rank` order and the selected `rotation_index`. Do not describe `priority_rank` as a project priority; despite the legacy field name, its ordinary scheduling meaning is only stable ring order.

A project-local blocker, race or no-op does not justify silently omitting the other portfolio entries, but it also does not authorize switching to a second deep project in the same pass. The next Scheduled pass is the normal boundary for another rotation slot.
