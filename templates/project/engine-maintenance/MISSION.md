# Mission template — AI-Agent-Bridge evolution

Evolve AI-Agent-Bridge safely through normal Protocol-v2 command/report cycles while preserving one canonical execution spine, exact history, recovery safety, and independent Owner control.

## Goals

- keep Protocol v2, canonical `state.json`, generation/CAS, exact `active_run`, append-only command/report/Owner evidence, and no-blind-rerun semantics authoritative;
- improve the Supervisor/Worker/Codex system in small reviewable units without creating a second workflow authority;
- keep planning and portfolio attention in the Supervisor while leaving real host/resource/path admission to the Worker;
- preserve rollback-safe self-maintenance and exact source/runtime identity when Bridge itself is changed;
- prefer derived read-only status views over duplicated mutable control state;
- require real runtime/acceptance evidence whenever a Goal depends on production or Windows behavior.

## Hard boundaries

- do not reset, force-push, rewrite canonical history, or manufacture State to make progress appear cleaner;
- do not bypass staged publication, Owner execution control, Worker admission, project/path/repository conflict checks, exact run identity, or uncertain-side-effect reconciliation;
- do not copy local credentials or secrets into repository evidence;
- Protocol wire changes require an explicit semantic need and reviewed compatibility boundary;
- live acceptance requires evidence from the live boundary named by the Goal.

## Planning authority

Read the instantiated project's `CURRENT_GOAL.md` and referenced Goal for its current objective. Generic architecture and operating rules come from `docs/ARCHITECTURE.md`, `SUPERVISOR_ENTRYPOINT.md`, `policies/`, `PROTOCOL.md`, and `protocol/v2/`.

When self-maintenance requires an isolated checkout, derive and verify that boundary from current source, policy, registry and runtime evidence.
