# Supervisor context template — engine-maintenance

This file is project-specific planning context only. Generic workflow, state transitions, publication, Owner control, recovery, portfolio, Goal and reconciliation rules live in `PROTOCOL.md`, `protocol/v2/`, `SUPERVISOR_ENTRYPOINT.md` and `policies/`.

## Required policy

- start from the current `CURRENT_GOAL.md`, canonical Protocol-v2 State, current source/runtime facts, applicable Owner evidence and current Owner execution control;
- use compact portfolio accounting and exactly one deep-focus planning target per Supervisor pass;
- publication uses the staged gateway and preserves exact bytes/hash, generation/CAS, Owner checks, canonical State and conflict/effect preflight;
- Worker admission remains authoritative for host/resource/path/repository conflicts and exact run identity;
- read-only planning while execution is unavailable is non-canonical and must be revalidated before publication;
- acceptance claims use the evidence category required by the Goal.

## Self-maintenance

For a Goal that changes Bridge itself, use the registered independent maintenance checkout and the production Candidate-local execution contract. Derive exact branch/source identity, health gate, adoption boundary and rollback boundary from current policy and runtime evidence.

Every report distinguishes current source/runtime facts, canonical execution State, planning Goal State, mutable Owner execution control and the exact acceptance evidence used for the current boundary.
