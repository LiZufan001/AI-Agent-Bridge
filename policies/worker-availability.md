# Worker availability policy

This policy defines the Scheduled Supervisor's external execution-capacity input. It is operational policy, not Protocol-v2 state. Heartbeat never changes generation, command/report history, Owner actions, Goal state, leases, recovery identity, or Worker admission.

## Contract

Deployment locator and thresholds come from `supervisor/bootstrap.json`. The mutable current value is the configured fixed GitHub Issue Comment and must conform to `supervisor/schemas/worker-heartbeat.schema.json`.

Availability has exactly two Supervisor values:

- `worker_available = true`
- `worker_available = false`

Do not create ONLINE/STALE/UNKNOWN/DEGRADED sub-states.

A valid `true` requires all of:

1. payload schema valid;
2. exact configured `worker_id`;
3. exact configured heartbeat interval and TTL;
4. offset-aware non-null `last_seen_at`;
5. timestamp no further in the future than configured clock-skew tolerance;
6. age not greater than configured TTL.

Missing/null/malformed/unreadable/wrong-identity/wrong-contract/future-beyond-skew/expired evidence is `false`.

## Meaning of a fresh heartbeat

A heartbeat means the designated Bridge Worker control loop was recently in a condition to accept new Bridge work: recent GitHub control-plane sync succeeded, required runtime config/registry were loaded, publication gateway/coordinator intake was initialized, and no control-plane-wide drain/restart/failure barrier prevented new intake.

It does **not** promise an immediately free slot, that all project workdirs are valid, that no active run exists, or that a specific project will pass claim/admission. Worker runtime remains authoritative for capacity, project locks, exclusive paths, Git/path conflicts and recovery.

The heartbeat payload must remain minimal. Do not add project states, command/report ids, active-run details, free-slot counts, source HEADs, raw exceptions/logs, IPs, secrets/tokens/cookies/signed URLs, recovery detail, or monitoring dashboards.

## Gate semantics

Availability gates only Worker-dependent execution/publication. It does not decide which project deserves Supervisor attention.

When `worker_available=false`, the Scheduled Supervisor may still perform one focus project's safe repository-grounded read-only review: inspect relevant Report/Goal/source/CI/release/feedback evidence, reconcile current facts, classify blockers, and produce a concise advisory next intent.

While false, it must not:

- mutate product source;
- change Goal lifecycle;
- allocate or consume a command id;
- produce final executable command bytes;
- create a staged publication/transition request;
- mutate canonical state;
- create a Worker-dependent notification-only EXECUTE;
- treat host capacity as available or reserve work for later execution.

Offline review is best-effort and non-canonical. It may be lost or recomputed. There is no separate receipt, lease, checkpoint or recovery requirement for advisory reasoning. When the Worker later becomes available, use fresh current evidence and the ordinary publication preflight; an earlier advisory is only a hint.

## Publication-time revalidation

Availability can change during a pass. Immediately before staging executable work, re-read the same fixed heartbeat and recompute the same boolean. If it is no longer true, do not materialize the request.

This check is planning safety. The local publication gateway remains independently authoritative for transport expiry, exact bytes, schema, fresh state/generation/report/CAS and publication. The gateway does not need to read the heartbeat comment.

## Writer behavior

The real Windows Bridge Worker is the only producer of fresh timestamps. It updates the same fixed comment rather than creating heartbeat history. Refresh only after the control loop has successfully reached the new-work intake boundary. Stop refreshing while controlled drain/restart or a control-plane-wide intake failure is in force.

Heartbeat update failure is fail-closed for future availability but must not kill or invalidate already running work. Graceful shutdown may best-effort write `last_seen_at:null`, but correctness depends on TTL expiry, not shutdown success.
