# New Bridge project template

Use this directory as a checklist when creating a new executable Bridge project. Do not copy a template directory under `projects/` until its identifiers/workdir are real; `projects/` is canonical coordination data.

## Minimum project layout

```text
projects/<project-id>/
├── MISSION.md
├── CURRENT_GOAL.md       # recommended for long-lived product projects
├── goals/                # bounded planning goals; optional for finite/smoke projects
├── SUPERVISOR.md         # optional but recommended for project-specific context
├── WORKDIR.md            # required before local execution
├── state.json
├── commands/
├── reports/
└── owner-actions/
```

`CURRENT_GOAL.md` / `goals/` are a non-canonical planning layer. They never replace `state.json` and never change Protocol-v2 generation/CAS/lease semantics. Long-lived product projects should follow `policies/current-goals.md`; acceptance-only/smoke projects may opt out.

## Initial state

A newly bootstrapped Protocol-v2 project normally starts with a deliberate Supervisor-created baseline, for example:

```json
{
  "protocol_version": 2,
  "project_id": "example-project",
  "status": "REPORT_READY",
  "generation": 0,
  "latest_command": 0,
  "latest_report": 0,
  "last_reviewed_report": 0,
  "active_run": null,
  "finalized": false,
  "human_required": false,
  "updated_at": "<ISO-8601>"
}
```

Before making the project executable, validate the intended baseline against `protocol/v2/state.schema.json` and `protocol/v2/check_conformance.py`.

## MISSION.md

Record stable mission-level facts only:

- goal and non-goals;
- acceptance criteria;
- important safety/security boundaries;
- evidence categories required before mission completion.

Do not use MISSION as an execution log.

## CURRENT_GOAL.md and goals/

For a long-lived product mission, use the goal layer to keep one bounded near-term outcome active at a time:

- `CURRENT_GOAL.md` points to exactly one ACTIVE `goals/goal-NNN.md` record;
- queued owner goals may be prepared ahead of time;
- Supervisor reviews evidence and marks goals COMPLETED;
- explicit owner direction may SUPERSEDE a goal;
- when no ACTIVE goal exists, Supervisor should auto-chain the next compatible queued owner goal or synthesize a bounded goal from current evidence + MISSION rather than waiting for the owner to return;
- Worker/Codex executors do not update goal status.

Use `templates/project/CURRENT_GOAL.md`, `templates/project/GOAL.md`, and `policies/current-goals.md` as the contract. A current goal is normally a one-to-three-command product outcome, not a second Mission and not a micro-task.

## SUPERVISOR.md

Use for mutable project-specific decision context that Scheduled Supervisor needs but generic protocol/policy should not duplicate, such as:

- owner product decisions;
- device/environment-specific operating conditions;
- current working hypotheses;
- evidence already collected and evidence still pending;
- project-specific priority order;
- project-specific goal-chain context when it cannot be inferred from the generic goal policy.

Do not duplicate generic state/CAS/alert/model policy here.

## WORKDIR.md and registry

Record the source repository and confirmed local workdir, then add the project to `worker/remote-projects.json` for the correct Worker host. The workdir must satisfy the tracked host roots and any stricter local allowlist.

## Before first command

Check:

1. project id is unique;
2. MISSION is specific enough to know the durable direction/stop boundary;
3. for a long-lived product project, CURRENT_GOAL is bounded and points to one ACTIVE goal (or the project explicitly opts out);
4. WORKDIR and repository identity are confirmed;
5. remote registry mapping is enabled and valid;
6. no credentials are stored in tracked project files;
7. canonical state passes conformance;
8. first command uses a fresh monotonic id and Protocol-v2 metadata.
