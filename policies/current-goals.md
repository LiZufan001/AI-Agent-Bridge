# Scheduled Supervisor current-goal policy

This file defines the **non-canonical planning-goal layer** used by the Scheduled ChatGPT Supervisor. It does not add Protocol-v2 states and does not change `state.json`, generation/CAS, leases, Worker admission, recovery, finalization, or owner-action semantics.

`state.json` remains the only canonical execution-state pointer. A current goal answers a different question: **what bounded product outcome should the Supervisor keep pursuing across one or more commands right now?**

## Project layout

Long-lived Bridge product projects SHOULD use:

```text
projects/<project-id>/
├── MISSION.md
├── CURRENT_GOAL.md
├── goals/
│   ├── goal-001.md
│   ├── goal-002.md
│   └── ...
├── SUPERVISOR.md
├── WORKDIR.md
├── state.json
└── ...
```

`MISSION.md` is the durable long-term direction. `CURRENT_GOAL.md` is a small mutable pointer to exactly one ACTIVE goal. `goals/goal-NNN.md` contains the bounded goal definition and lifecycle evidence.

Acceptance-only/smoke projects, terminal projects, or projects whose `SUPERVISOR.md` explicitly opts out do not need this layer.

## Goal states

Goal records use exactly these planning states:

- `QUEUED` — owner/Supervisor has defined the goal for future activation, but it is not the current goal.
- `ACTIVE` — the one current goal the Supervisor is pursuing.
- `COMPLETED` — acceptance criteria are proven strongly enough for the Supervisor to close the goal.
- `SUPERSEDED` — the goal is intentionally replaced by a newer direction; it is not the same as completion or failure.

A participating project MUST have at most one `ACTIVE` goal. `CURRENT_GOAL.md` MUST point only to that ACTIVE goal.

Goal ids are monotonically increasing within a project; gaps are allowed. Goal files are planning artifacts, not Protocol-v2 command/report ids.

## Authority

- **Owner** — may define, reorder, replace, pause, or supersede goals at any time. Explicit newer owner direction wins over Supervisor-generated planning.
- **Supervisor** — may create a goal when no valid current goal exists, activate queued goals, mark a goal COMPLETED when evidence satisfies its acceptance criteria, and auto-chain the next goal.
- **Worker / Codex Executor** — MUST NOT change `CURRENT_GOAL.md` or goal lifecycle status. Executors may report evidence relevant to a goal, but they do not decide that the product goal is complete.

A goal change is planning metadata only. It MUST NOT mutate canonical generation, active lease, published command bytes, or a running executor task snapshot.

If owner direction arrives while `COMMAND_READY` or `CODEX_RUNNING`, record it durably if appropriate but do not inject it into the already-published/claimed task. Apply it at the next safe Supervisor planning boundary.

## Goal record contract

Each `goals/goal-NNN.md` SHOULD contain a single machine-readable metadata comment near the top plus human-readable sections. The metadata should include at least:

```json
{
  "schema_version": 1,
  "goal_id": 1,
  "status": "ACTIVE",
  "source": "OWNER",
  "created_at": "<ISO-8601>"
}
```

`source` is `OWNER` or `SUPERVISOR`.

Each goal MUST state:

1. a concise title;
2. `why_now` / why this goal is the next useful boundary;
3. one coherent objective;
4. explicit acceptance criteria;
5. important non-goals / scope boundaries;
6. dependencies or owner/external evidence that may be required;
7. when terminal, concise completion or supersession evidence.

A good current goal is normally small enough to complete in roughly **one to three coherent Bridge commands**, while still representing a user/product outcome rather than a micro-task. Do not create a second Mission disguised as a goal.

## Goal auto-chaining

At every safe planning boundary for a participating, non-terminal product project, the Supervisor applies this order:

1. Reconcile current canonical state, latest report, real source head/history, CI/E2E evidence, owner feedback/actions, `MISSION.md`, `SUPERVISOR.md`, `CURRENT_GOAL.md`, and relevant goal files.
2. If one ACTIVE goal exists and its acceptance criteria are not proven, keep pursuing that goal. Do not invent another goal merely to keep Codex busy.
3. If the ACTIVE goal's criteria are proven, mark it `COMPLETED` with evidence. Executor self-assertion alone is insufficient; the Supervisor must review the evidence category required by the goal.
4. After completion, first activate the highest-priority compatible `QUEUED` goal that came from explicit owner direction.
5. If no queued owner goal exists and the mission is not ready for finalization, synthesize one new bounded goal from the current real baseline.
6. Update `CURRENT_GOAL.md` to point to the newly ACTIVE goal before or together with publishing work for it.
7. If the mission is genuinely complete, prefer the normal finalization path instead of manufacturing another goal.

When synthesizing a goal, planning priority is:

1. latest explicit owner direction;
2. unresolved material owner feedback;
3. natural continuation of the just-completed goal;
4. correctness/reliability gaps proven by current source/report/CI/E2E evidence;
5. `MISSION.md`;
6. project roadmap / discretionary polish.

The Supervisor must not create work solely to occupy a concurrency slot.

## ACTIVE-goal continuation invariant

An ACTIVE goal is itself a durable planning instruction; it does **not** require a fresh Executor report, source commit, CI run, or other new drift before the next command can be planned.

At every safe planning boundary, after current-state/source reconciliation:

- if canonical Protocol state permits a new command, there is no active/pending run for the project, `CURRENT_GOAL.md` points to an ACTIVE goal whose acceptance criteria are not yet proven, and current evidence identifies at least one bounded Executor-doable slice that is not already satisfied, the Supervisor MUST continue that goal by planning the next safe command (and publish it when execution/publication gates permit);
- the absence of newer source/report evidence is **not** a valid `reviewed/no-op` reason. Source reconciliation is a guard against conflicting or already-completed work, not a requirement that some external change occur before an ACTIVE goal can progress;
- this rule applies immediately after goal auto-chaining. If Report N closes Goal A and the Supervisor activates Goal B, the same planning boundary may produce the first command for Goal B when safe; it must not wait for a hypothetical Report N+1 that cannot exist until such a command is executed;
- when Worker execution/publication is unavailable, record the bounded deferred execution intent instead of manufacturing a command id or immutable bytes.

`reviewed/no-op` is appropriate for an ACTIVE goal only when current evidence proves one of the following: all remaining acceptance criteria are genuinely Owner-only/external/time/device dependent; the candidate implementation slice is already satisfied; current source/evidence is ambiguous or unsafe to modify; an explicit dependency prevents safe execution; or the goal should instead be completed/superseded under the normal lifecycle rules.

This invariant prevents a self-locking cycle of `ACTIVE goal -> no new report -> no command -> no possible new report`.

## Blocked goals

A current goal does not expire merely because progress is temporarily blocked.

If safe continuation requires a genuine owner-only action/decision, keep the goal ACTIVE and use the existing `HUMAN_REQUIRED` / owner-action path. Do not replace the goal just to avoid the blocker.

`SUPERSEDED` is appropriate when a newer owner direction intentionally replaces the outcome, or when later accepted evidence proves the old outcome is no longer the desired/valid target. It must not be used to hide implementation failure.

## History and mutation rules

`CURRENT_GOAL.md` is intentionally mutable because it is only a pointer to current planning intent.

A goal record may be updated while `QUEUED` or `ACTIVE` to reflect owner-approved clarifications and the final lifecycle transition. Once a goal becomes `COMPLETED` or `SUPERSEDED`, treat that goal file as frozen historical evidence; later corrections should be recorded in a new goal or explicit linked note rather than silently rewriting terminal meaning.

Git history remains the audit trail for goal transitions. Commands, reports, owner-actions and canonical state keep their existing Protocol-v2 immutability/CAS rules unchanged.
