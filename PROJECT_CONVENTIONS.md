# AI Agent Bridge project conventions

> Repository boundary: code, schemas and generic policy are in Engine. All `projects/`, concrete `supervisor/*.json`, registry and staged publication paths in this document are relative to the explicitly bound **State** root. Read Engine policy from an independently accepted immutable Engine revision; never infer State from Engine working directory. Protocol v2 semantics below are unchanged.

This file records repository-wide project setup conventions that sit beside `PROTOCOL.md`. Keep protocol semantics in `PROTOCOL.md` / `protocol/v2/`; keep Supervisor operating heuristics in `policies/supervisor.md`; keep shared security policy in `policies/security.md`.

## Required local execution path

Every Bridge project intended for the local Worker must record its expected source repository and local workdir before its first executable command is published.

Each executable project must contain:

```text
projects/<project-id>/WORKDIR.md
```

`WORKDIR.md` records at least:

- Bridge `project_id`;
- source GitHub repository;
- expected local Worker `workdir` on the primary execution machine;
- whether that path has been confirmed by the owner/local environment.

A GitHub repository identity and a Windows clone path are separate facts. The Supervisor must never infer one from the other.

## Executable project registry

The tracked host-scoped registry is:

```text
worker/remote-projects.json
```

For each enabled remotely managed project, the registry maps:

```text
project_id -> repository + workdir
```

The hardened Worker reloads the registry after Bridge synchronization, so adding, disabling, or moving a registered project normally does not require editing `worker/config.local.json` or restarting the Worker.

Before claim, the Worker must verify:

1. the workdir is inside the host registry's allowed roots;
2. if local `allowed_workdir_roots` is configured, the workdir is also inside that stricter machine-local allowlist;
3. the directory exists and is a Git working tree;
4. `remote.origin.url` matches the repository declared by the registry.

Validation failure leaves the command unclaimed. Never weaken these checks merely to make a task start.

`worker/config.local.json` remains authoritative for machine/runtime-only settings such as Codex arguments, network guard, timeouts, log retention, and optional stricter local roots. Secrets remain local and must not be moved into the tracked registry.

## New-project procedure

Before publishing the first executable command for a new project:

1. obtain and verify the local workdir;
2. create `projects/<project-id>/WORKDIR.md`;
3. add the host/project mapping to `worker/remote-projects.json`;
4. verify that the workdir satisfies host allowed roots and any stricter local allowlist;
5. only then publish executable work.

If an already-published initial command is still canonical and unclaimed because an older Worker predates remote-registry support, do not publish a duplicate command. Synchronize/adopt the upgraded Worker and allow the existing command to be claimed normally.

## Owner feedback inbox

Projects that receive direct owner product, UX, bug, priority, or acceptance feedback should use:

```text
projects/<project-id>/owner-feedback/
```

The directory is distinct from `owner-actions/`.

- `owner-feedback/` stores non-canonical human planning/acceptance input that should influence future Supervisor commands without pausing the state machine;
- `owner-actions/` stores human-only actions/evidence associated with the existing `HUMAN_REQUIRED` recovery semantics.

When `owner-feedback/` exists, include a local `README.md` that points to `protocol/v2/OWNER_FEEDBACK.md` and explains any project-specific naming or item conventions. Feedback history is append-only and must never be used as a second canonical state pointer.

Feedback may be recorded while work is pending or running, but a published/claimed command remains immutable. Late feedback is consumed at the next safe Supervisor planning boundary according to `policies/supervisor.md`.

## Path changes

If a source repository moves locally, update both:

- `projects/<project-id>/WORKDIR.md`;
- the matching host entry in `worker/remote-projects.json`.

A local `allowed_workdir_roots` setting is an independent narrowing rule and must not be remotely widened.

## Worker self-update

The hardened Worker detects tracked Worker implementation changes after synchronization and exits with the dedicated restart code before claiming new work. The Windows launcher treats that as a controlled restart and remains a persistent watchdog with bounded exponential backoff.

This mechanism only works after the hardened launcher/Worker generation has been adopted once. Migration from an older already-running Worker may therefore require one explicit adoption restart after the new tracked files have synchronized.

See `worker/README.md` and `worker/REMOTE_PROJECTS.md` for runtime details.

## Scheduled Supervisor workload sizing

Command-sizing heuristics are operational policy, not project-registration semantics. Follow `policies/supervisor.md` instead of duplicating timing guidance here.

## Hosted CI / Actions budget discipline

Hosted CI minutes are a finite shared engineering resource across Bridge-managed projects. Saving CI must never weaken correctness or final acceptance, but routine development must avoid spending hosted runners on evidence that can be obtained more cheaply and deterministically.

These conventions apply to every Bridge-managed project that uses hosted CI:

1. **Diagnose before rerun.** Read the existing failed job logs, step summaries, artifacts, and source before starting another hosted run. Do not rerun an unchanged deterministic failure merely to see whether it fails again.
2. **Use focused validation while debugging.** During reproduction and corrective iteration, prefer the smallest local or targeted test that exercises the changed subsystem. Do not push speculative edits only to use hosted CI as a diagnostic probe when source inspection, local reproduction, or a narrower test can answer the question.
3. **Target failed work instead of replaying known-green work.** When the CI provider permits it and the evidence boundary is preserved, rerun only the failed job or a deliberately targeted workflow rather than the entire suite.
4. **Reserve full hosted acceptance for real acceptance boundaries.** A full exact-head regression/integration/build/installer/isolation suite should normally run after the focused fix is credible, at a stage boundary, before release/finalization, or when a cross-cutting change genuinely requires full coverage. Repeated full-suite runs are not a substitute for diagnosis.
5. **Use expensive environments only when their semantics matter.** Windows, installer, production-service, isolation, physical-device, soak, and other high-cost jobs should run when the behavior under test requires those environments. Tests that are semantically portable should prefer the cheaper compatible runner or local execution where practical.
6. **Superseded runs should not keep burning quota.** Workflows SHOULD use branch/workflow concurrency with cancellation of in-progress runs when a newer commit makes the older run irrelevant, unless preserving both runs is required for a specific evidence or release reason.
7. **Coalesce pushes.** Prefer one coherent implementation plus focused local validation before pushing, rather than a sequence of tiny speculative commits that each trigger the same hosted matrix.
8. **Quota pressure may defer non-urgent hosted work, not evidence requirements.** When included CI quota is low or an owner has requested conservation, non-urgent heavy validation may wait for a later safe window. The report must state what remains unverified; it must not promote partial/local evidence to full hosted acceptance.
9. **Final evidence still wins over cost.** If mission, release, security, platform, or production acceptance requires a full hosted run, run it before claiming the corresponding acceptance. Cost optimization changes when and how often evidence is collected, not the required strength of the final evidence.

Workflow design SHOULD separate cheap routine checks from expensive platform/integration acceptance where practical, using path filters, manual/targeted dispatch, reusable workflows, or equivalent mechanisms without silently reducing coverage. CI-budget optimization is an engineering optimization, not permission to weaken regression, security, release, or production gates.

## Security

Follow `policies/security.md`.

In particular, `WORKDIR.md`, `worker/remote-projects.json`, and owner-feedback records may contain ordinary non-sensitive repository, local-path, product, and evidence metadata chosen by the owner, but must not contain private authentication material or equivalent secrets.

The remote registry is operational configuration, not a substitute for Protocol v2 authorization. Every command still requires the normal canonical-state, generation/CAS, stale-command, and execution-lease checks.
