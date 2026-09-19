# Supervisor SUCCESS review policy

This file defines the default Supervisor review policy after a Worker/Codex run reports `SUCCESS`. It is operational policy, not a Protocol-v2 wire-state change.

## Principle

A Worker/Codex `SUCCESS` report is execution evidence, not self-approval. The Supervisor must independently verify the evidence needed to accept the result and choose the next planning boundary.

The default review mode is **evidence verification, not redundant re-execution**.

The Supervisor should verify the strongest already-produced mechanical evidence first and should not repeat expensive work merely to reproduce evidence that is already exact, attributable, current, and independently inspectable.

## Default lightweight SUCCESS review

For an ordinary successful command, the Supervisor should normally verify only the smallest set needed to establish that the reported result is real and in scope:

1. canonical `state.json` is at the expected post-run boundary and names the expected `latest_command` / `latest_report`;
2. the canonical report identity matches the claimed command/run/generation and reports `SUCCESS`;
3. the reported delivery branch and exact HEAD exist and match the reported SHA;
4. any claimed GitHub Actions result is for that exact HEAD and is completed/GREEN;
5. the changed-file/diff scope is consistent with the command authority and contains no material unauthorized production/main/control-plane mutation;
6. any intentionally unperformed work or remaining limitation is still represented honestly in the report;
7. no stronger acceptance claim is made than the available evidence supports.

When these checks are sufficient, the Supervisor should accept the engineering result without rerunning the full test suite, rebuilding artifacts, replaying integration tests, or repeating the executor's implementation work.

## Escalation to deep review

Upgrade from lightweight evidence verification to deeper independent inspection only when justified by risk or evidence quality. Typical escalation triggers include:

- Protocol, canonical state, CAS, lease, recovery, no-rerun, or history semantics;
- security, credentials, permissions, sandbox/containment, WFP/AppContainer, or network-egress boundaries;
- concurrency, race conditions, process ownership, crash recovery, persistence, migration, or destructive behavior;
- Stable adoption, production activation, production rollback, privileged bootstrap, or other live-host mutation;
- exact-head CI is missing, stale, red, ambiguous, or does not cover the material change;
- report, branch, SHA, diff, runtime, or canonical evidence disagree;
- scope is materially broader than the published command;
- the executor explicitly reports uncertainty, partial acceptance, unsupported environment assumptions, flaky evidence, or unverified side effects;
- prior review/owner evidence contradicts the new `SUCCESS` claim.

A deep review should still prefer targeted independent verification over blindly rerunning everything. Re-execution is warranted only when it closes a concrete evidence gap or tests a materially independent acceptance boundary.

## Risk-proportional review depth

Review depth is proportional to consequence and uncertainty, not to how long the executor ran or how many tests it reported.

- **Low-risk routine work**: lightweight evidence verification is normally sufficient.
- **Medium-risk architecture/integration work**: verify exact diff and the most relevant mechanical tests/CI, adding targeted source inspection where the report alone is insufficient.
- **High-risk protocol/security/recovery/concurrency/production work**: perform independent source/evidence review of the affected invariants and live boundary; require stronger mechanical evidence before planning the next stage.

The Supervisor may downgrade back to lightweight review in later routine stages after the risky boundary itself has been independently accepted.

## No duplicate authority

This policy does not make CI, reports, or the Supervisor a second canonical state machine. `state.json` remains the canonical pointer, Protocol-v2 invariants remain authoritative, and the executor still cannot self-approve mission completion.

The review outcome is a planning decision: accept the report as sufficient evidence for the next bounded command, escalate review, request owner evidence where truly required, or stop conservatively when evidence is contradictory/insufficient.

## Practical rule

Prefer:

```text
report identity
-> exact delivered SHA
-> exact-head CI / mechanical evidence
-> bounded diff / authority check
-> stated limitations
-> next planning boundary
```

over:

```text
SUCCESS
-> rerun all tests again
-> rebuild everything again
-> repeat the executor's work
```

unless a concrete risk or evidence gap justifies the additional work.
