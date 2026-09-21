# Bridge roadmap

This file tracks **future** engineering direction. Current behavior is defined by `PROTOCOL.md`, `protocol/v2/`, `SUPERVISOR_ENTRYPOINT.md`, `policies/`, `docs/ARCHITECTURE.md`, and the actual Worker/runtime source.

## Current baseline

Bridge now has one supported execution spine:

```text
Scheduled Supervisor
  -> compact accounting for every Owner-selected project
  -> exactly one deep focus project
  -> staged publication gateway
  -> canonical Protocol-v2 generation/CAS
  -> Worker admission + active_run claim
  -> Codex
  -> exact-run Report
```

Execution-unavailable planning is read-only, non-canonical, and disposable. It does not reserve command ids, generate executable bytes, mutate product source/state, or create a second queue.

The Worker remains authoritative for actual host capacity, same-project serialization, repository/path conflicts, process ownership, claim/lease, and recovery. Supervisor fairness affects attention only; it does not model free execution slots.

## Near-term priorities

### 1. Prove the simplified Supervisor naturally

Use ordinary Scheduled passes and exact canonical evidence to verify that:

- one eligible focus can progress through the supported Supervisor → gateway → Worker path;
- non-focus projects are honestly reported as deferred for that pass rather than deeply scanned;
- malformed or unavailable optional evidence cannot veto unrelated legal Online work;
- Owner pause/unreadable control, active execution, source uncertainty, and unknown external side effects still fail closed at the correct project boundary;
- a published command proceeds through the existing gateway/Worker/Codex/Report chain without bypassing Protocol-v2 safety.

Natural production evidence is the acceptance authority for these claims.

### 2. Keep one fact in one authority

Continue removing duplicated mutable facts from project prose, dashboards and helper caches. Mutable facts such as execution permission, canonical state, current Goal, heartbeat freshness, runtime slots, and source HEAD must be read from their current authority when a decision depends on them.

Read-only projections should remain rebuildable and incapable of authorizing workflow transitions.

### 3. Finish operator visibility

The active `engine-maintenance` Goal is the real-time local Bridge Dashboard. Its useful direction is a local, read-only operational view over existing authorities: Worker freshness, active runs/slots, project state/severity, current model profile, durations, and human-readable blocked/waiting reasons. It must not become another workflow database or write surface.

### 4. Keep operator-managed environment cleanup evidence-driven

Environment cleanup may remove external services, directories or credentials only when the operator can independently verify service state, paths, permissions and credential ownership. Repository cleanup alone is not evidence that an external resource has been removed.

### 5. Preserve recovery and self-maintenance safety

Keep exact Git identities, non-force forward integration/rollback, admission drain, health gating, preserved canonical history, and no blind replay after uncertain side effects.

### 6. Wire-protocol changes only for real semantic need

Protocol v2 remains the wire authority. Do not introduce v3 states or fields merely to represent runtime-local lifecycle, host scheduling, observability, planning metadata, or an auxiliary analysis mechanism. A wire change should require a demonstrated semantic gap that cannot be represented safely by the current protocol plus local/runtime policy.

## Maintenance rule

Prefer deletion or demotion over another control plane. An auxiliary mechanism belongs on the Online critical path only when it uniquely protects an execution-safety invariant. Otherwise it should be advisory, derived, or absent.
