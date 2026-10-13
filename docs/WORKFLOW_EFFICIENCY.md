# Workflow efficiency and deterministic Supervisor helpers

This change keeps public Engine, private State and product workspaces separate. Protocol v2, immutable command/report history, CAS/generation, same-project exclusion, Owner controls, model cost policy and controlled adoption remain binding. A helper result is never a new execution authority.

## What changed

- `worker/supervisor_tools.py`: pure fixed-owner-ring selection, exact task preparation, optional create-only local staged input, bounded immutable-commit context, conservative exact-run recovery advisory and code-revision capabilities.
- Gateway scanning: unrelated files do not consume the valid-request page budget; unsupported JSON names, incomplete scans and access/I/O errors are visible. Candidate pages rotate in disposable process memory. The maximum directory scan remains 4096 examined entries; beyond it, coverage is explicitly incomplete, not claimed exhaustive.
- One shared consume flow now handles core publication. The production facade supplies only current freshness, Owner execution-control and verified-resume hooks. The core class remains a lower-level synthetic adapter, not a replacement for the production facade.
- Transient/ambiguous publication errors retain immutable request input. A later pass reconciles exact State and bytes before publication. This does not replay an executor.
- Network Git is bounded by an exact owned subprocess-tree lifetime and bounded output. Local Git behavior and model execution-duration policies are unchanged.
- Phased prompts expose a bounded global objective/dependency/final-acceptance overview. Later-phase context cannot authorize early actions; the original current-phase/checkpoint mechanism remains the execution boundary.
- Network diagnostics distinguish transport timeout, DNS, TLS, connection, HTTP status, unknown country and confirmed disallowed country without exposing response bodies or addresses. Nonfinite timeouts and non-ASCII country codes are rejected; fail-closed behavior is retained.
- Request privacy screening distinguishes finite non-secret literals/references from credential-like values. It remains a conservative heuristic, not proof that arbitrary text is safe. Do not place real credentials into task transport.

## Stable rotation

Sort all Owner-selected members by `(priority_rank, project_id)`, including paused/running/blocked members. Derive the starting index from the UTC hour modulo that fixed ring length, then walk cyclically to the first currently eligible member. Paused members keep positions but cannot run. Owner selection/rank changes rebuild the anchor; transient eligibility does not.

`choose_focus(projects, eligible_ids, hour_bucket=...)` performs this calculation. Eligibility and a fresh-recovery/control-plane/new-Owner preemption still require current evidence from the caller. The helper cannot decide that a recovery is safe or that a paused project should execute. Empty/all-ineligible rings return no ordinary focus. There is no persistent fairness cursor or extra queue.

## Task preparation

Use the actual accepted Engine interpreter and an explicitly bound State checkout:

```text
python -B worker/supervisor_tools.py capabilities
python -B worker/supervisor_tools.py context --state-root <state-checkout> --focus <project>
python -B worker/supervisor_tools.py prepare --state-root <state-checkout> --project <project> --body-file <utf8-body> --producer scheduled_chatgpt
```

Preparation is read-only by default. It computes the next command id, based-on report, post-publication generation, exact UTF-8 command/header/hash and `request-<id>.json` envelope. The body must not provide another `bridge-command` header. Ordinary work inherits the Worker default unless an explicit validated override is provided through the Python API.

`--emit-request` includes the exact validated envelope as UTF-8 text for an already-authorized transport. It is not submission or a permission grant. `--write-staged` creates only a new local staged file after a second State identity and expiry check; it does not commit, push, publish canonical files, claim a task or start a model. An authorized producer must separately publish those exact staged bytes through the existing transport and verify the resulting immutable Git identity. A local staged receipt is not `STAGED_INPUT_SUBMITTED` to the remote and not canonical acceptance.

The helper's normal preparation path accepts REPORT_READY only. Verified HUMAN_REQUIRED resume continues to use the existing specialized protocol path; do not synthesize REPORT_READY input to bypass it. The schema-v1 envelope is unchanged. Supported preparation lifetime is at most 600 seconds and allowed future skew at most 60 seconds, aligned with the current production facade contract.

Producer selection is an explicit caller attestation, not authentication. The caller must establish its actual authority and re-read Owner control/heartbeat before preparing executable work. The gateway independently validates freshness, exact bytes, State and CAS. Do not label a manual session as Scheduled merely to satisfy the field.

## Compact context and evidence

`context` reads all compact project facts from one immutable local State commit, then returns references and bounded focus-only Goal/Report excerpts. Missing/unreadable optional excerpts are explicit and do not assert that no goal/report exists. Diagnostics, preserved interrupted transcripts and sensitive assignments are not copied into the excerpt. Read the original source when the excerpt is insufficient.

The view is disposable, carries hashes and source identity, does not perform semantic review, does not prove that local Git equals the latest remote, and does not replace current heartbeat/control verification. There is no persisted view ledger or mandatory cache. If the helper is absent, use existing authoritative source reads. Never infer its installation from a new remote document.

`needs_report_review` is a derived comparison of existing report counters, not a new state, command, automatic trigger or permission. No event-driven paid model loop or higher polling cadence is introduced.

## Recovery advisory

```text
python -B worker/supervisor_tools.py recovery --state-root <state-checkout> --project <project> --run-id <exact-run-id>
```

This command reads bounded exact-run evidence and returns hashes/classification only. Active runs, identity mismatch and unresolved external effects remain blocking. A completed process with explicitly known effects and pending report publication is merely a candidate for the existing recovery inspector. Every advisory explicitly denies automatic retry and makes no canonical change.

The change does not automatically resolve a historical recovery or change its original unknown-effects flag. Notifications and existing notification-before-transition guards are unchanged; no speculative automatic uninstall, registry cleanup or delivery replay is performed.

## Git transport

`BRIDGE_GIT_NETWORK_TIMEOUT_SECONDS` defaults to 60, with finite validated values 1 through 300 seconds. It applies to fetch/push/pull/ls-remote/clone through the store. The existing owned-process primitive terminates only that invocation's child tree on timeout and releases the cooperating mutation lock. It never kills unrelated user processes.

An uncertain push retains the local publication intent and raises a safe error. Do not treat it as proof that the remote rejected the write. A subsequent read-back may establish already-applied publication; it must not run the model twice. Bounded diagnostics expose operation class, outcome, timestamp and whether an upstream request id was returned, not raw credentials or URLs. This is not an additional timeout on Codex work.

## Deployment and acceptance

New runtime code is not live until the outer authority adopts an independently accepted candidate. Keep production runtime revision, trusted State validator revision and Supervisor policy revision distinct and verify supported capabilities. Do not update the trusted validator or bootstrap baseline merely to make a candidate pass.

Before production adoption: verify exact source/diff, complete regression and synthetic State/privacy gates; exercise real temporary Git and a synthetic child executor; ensure no active production run, establish the existing controlled handoff, retain exact rollback identities and recheck the original Worker/Tunnel. The developer checkout must not silently become the running Engine.

Synthetic integration validates preparation→staged input→canonical gateway publication→one actual child-process invocation→Report, plus duplicate input, stale/forged bytes and lost push acknowledgement. This is not a real-model or production end-to-end claim. Report unit tests, real fixture integration, remote CI, deployment and actual model acceptance separately.
