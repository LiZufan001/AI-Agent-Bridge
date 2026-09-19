# Supervisor context template — engine-maintenance

This file is project-specific planning context only. Generic canonical workflow, state transitions, publication, Owner control, recovery, portfolio, Goal, and source-reconciliation rules live in `PROTOCOL.md`, `protocol/v2/`, `SUPERVISOR_ENTRYPOINT.md`, and `policies/` and take precedence.

## Required policy

- start from the current `CURRENT_GOAL.md`, canonical Protocol-v2 state, current source/runtime facts, unresolved relevant Owner evidence, and current Owner execution control;
- use compact portfolio accounting and at most one deep-focus planning target per Supervisor pass;
- bounded cold planning must remain possible without a saved review receipt, review lease, Shadow backlog, or optional analysis service;
- optional offline/review analysis is read-only, non-canonical, disposable advice and must be freshly revalidated before it can influence executable work;
- publication must use the normal staged gateway and preserve exact bytes/hash, generation/CAS, Owner checks, canonical state, and conflict/effect preflight;
- Worker admission remains authoritative for host/resource/path/repository conflicts and exact run identity;
- do not synthesize Protocol state, commands, reports, recovery evidence, or acceptance results;
- do not reset, force-push, rewrite history, or revive a retired Candidate-B/Shadow/review execution authority.

## Self-maintenance

If the current Goal requires changing Bridge itself, derive any isolated checkout, branch, source identity, health gate, adoption boundary, and rollback boundary from current policy and runtime evidence. Do not reuse historical Candidate-B branch names, Bootstrap SHAs, phase numbering, or Stable-A assumptions as standing requirements.

Every report should distinguish current source/runtime facts, canonical execution state, planning Goal state, mutable Owner execution control, and historical evidence. Implementation or CI evidence must not be reported as live runtime acceptance unless the Goal's actual acceptance boundary has been exercised.
