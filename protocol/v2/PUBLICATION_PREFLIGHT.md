# Protocol v2 command publication preflight

This document defines the deterministic pre-publication validation boundary for a command that may become canonical `COMMAND_READY`/`FINALIZING` work.

It is a Protocol-v2 engineering/profile rule. It does not add a wire state, change generation arithmetic, or change the execution lease model.

## Core rule

A Supervisor/operator publisher MUST NOT make a command canonical until the **exact final command bytes that will be committed** pass the complete deterministic, side-effect-free validation set that the Worker would apply before claim.

Valid `bridge-command` metadata alone is not sufficient proof that a command is claimable.

The publication preflight is therefore an exact-bytes gate:

```text
construct final command bytes
-> run complete deterministic pre-claim validation on those exact bytes
-> re-read canonical state / re-check state-relative identity
-> publish append-only command + canonical pointer by CAS
```

Any byte change after a successful preflight invalidates that preflight and requires validation again before publication.

## Required validation set

Before creating the canonical command pointer, the publisher MUST validate at least:

1. **Protocol command metadata**
   - exactly one near-header `bridge-command` metadata record;
   - normalized metadata/schema validity;
   - supported source/kind/executor fields only;
   - command id and any supersession relation are structurally valid.

2. **Canonical-relative command identity**
   - `command_id` is the intended new canonical id;
   - `based_on_report` matches the canonical report boundary used for publication;
   - `expected_generation` matches the generation that the publication transition will establish;
   - the source state is an allowed publication boundary;
   - there is no competing active execution lease.

3. **Optional execution-layer body format**
   - if a phased declaration exists, or any phased marker-like syntax is present, run the strict whole-body phased parser on the exact final bytes;
   - require exactly one global section;
   - require every declared phase exactly once, in declared order, with non-empty content;
   - require exactly one final-acceptance section after the declared phases;
   - reject missing/undeclared/duplicate/nested/overlapping/out-of-order markers;
   - reject malformed marker-like literals or other unmarked content that the Worker parser would reject.

4. **Other deterministic Worker pre-claim contracts**
   - when the current Worker has additional pure, deterministic command-format validators, the publisher SHOULD reuse the canonical implementation rather than duplicate a weaker parser;
   - validation that requires host mutation, executor launch, network side effects, or a claim lease is not publication preflight and remains the Worker's responsibility.

Current implementation references include `worker/protocol_core.py` for Protocol-v2 metadata semantics and `worker/phased_task.py::parse_phased_task` for the optional phased body. These implementation names are not new wire-protocol fields; they identify the canonical parser boundaries that publication tooling should reuse where available.

## Exact-bytes discipline

Preflight MUST run after the command body is fully assembled, including headers, metadata, phased markers, final acceptance, examples, and completion instructions.

Do not validate a template and then append content before publication. Do not validate metadata separately and infer that the body is safe. Do not hand-check phased markers when the canonical strict parser is available.

A publisher that cannot mechanically run the required deterministic parser for a body format MUST fail conservatively rather than publish that format speculatively.

## Cloud-to-local staged publication

The cloud Scheduled Supervisor cannot run the Windows Worker's local phased
parser, host checks, or Git CAS. Its only normal bridge transport is one
immutable request committed under:

```text
worker/staged-publications/requests/request-<request_id>.json
```

The request MUST conform to `staged-publication.schema.json` and include the
complete final `command_content` plus `command_sha256` over its exact UTF-8
bytes. The envelope identity (`project_id`, `command_id`, `source`, `kind`,
`based_on_report`, and `expected_generation`) must equal the one metadata
record inside those bytes. Normal staged pairs are
`scheduled_chatgpt/EXECUTE` and `finalizer/FINALIZE`; ordinary manual/user
commands remain on their existing explicit local paths.

The local gateway reads the committed blob after the Worker's ordinary Git
sync, resolves the matching local project, and re-reads canonical state. A
normal command publication still requires `REPORT_READY`. The existing
Protocol-v2 `supervisor.resume_after_owner` boundary is the one narrow
exception: a `scheduled_chatgpt/EXECUTE` request may publish directly from
`HUMAN_REQUIRED` to `COMMAND_READY` only when the newest tracked owner-action
linked to the current canonical report is still `OWNER_REPORTED_DONE /
VERIFIED`. The gateway re-reads and binds that exact evidence again at the CAS
boundary; pending, failed, stale-report, malformed, or changed owner evidence
fails closed without a canonical write. No extra resume state, request type,
queue, or second execution path is introduced.

For both ordinary publication and verified owner resume, the gateway rejects
active projects and stale generation/report identities, runs this exact
preflight again immediately before the CAS payload is built, and commits the
command bytes and state pointer together through `git_store.publish_cas`; the
state update also sets `last_reviewed_report=based_on_report`. A verified owner
resume additionally clears `human_required` in that same atomic publication.
The next normal Worker pass then claims the canonical pending command. There is
no direct cloud canonical write, claim-before-validation path, mutable command
rewrite, semantic stale retry, or success report emitted by the gateway. A CAS
race is a bounded safe outcome for that request and must be reconsidered by a
later Supervisor pass from fresh canonical evidence.

After a bounded inspection outcome, the gateway moves the unchanged tracked
request blob out of the active inbox into the non-canonical history area:

```text
worker/staged-publications/history/<outcome>/request-<request_id>.json
```

This is a lifecycle rename, not a content update; Git history retains the
original staged bytes and the outcome path. `published`, `already_applied`,
stale, invalid, active-project, CAS-race, and safe-error entries therefore do
not accumulate in the bounded active inbox or consume a later project's
publication slot. If the lifecycle rename itself cannot be pushed safely, the
immutable request remains in `requests/` for a later non-canonical retry; the
canonical CAS result remains idempotent.

## Failure behavior

A deterministic preflight error means **do not publish**:

- do not create or advance the canonical `state.json` pointer;
- preferably do not create the command file at all;
- if a non-canonical orphan file was already created because of a lost race or staging flow, leave it immutable and non-canonical under the existing append-only history rules;
- do not rely on the Worker to reject a command that the publisher already knows is malformed.

Publication preflight is prevention. `operator.repair_unclaimed_command` remains the narrow recovery mechanism only for a deterministic-invalid command that actually escaped prevention and became the exact canonical unclaimed command. When a valid replacement was already written before that discovery, `operator.adopt_staged_repair` is the separate pointer-only path: it binds the exact existing replacement bytes and publishes only the state pointer through the shared CAS store.

## Repair after an escaped defect

If a malformed command nevertheless becomes canonical and remains unclaimed:

```text
COMMAND_READY / invalid command C
-> prove deterministic complete pre-claim defect
-> append new valid command N with supersedes_command_id=C
-> CAS canonical pointer to N
```

The old command stays immutable history. It is never edited in place, manually claimed, or given a fabricated report.

A complete pre-claim defect includes deterministic metadata/Protocol contract errors **or deterministic optional-body parser errors** such as a malformed phased-task envelope. Runtime environment failures, network guard failures, Worker crashes, dirty workdirs, timeouts, and uncertain execution side effects are not command-repair conditions.

The replacement command itself MUST pass this full publication preflight on its exact final bytes before it may supersede the old command.

## Supervisor acceptance checklist

For every future command publication, record the following mental/mechanical gate before canonical mutation:

```text
exact_final_bytes_ready = YES
protocol_metadata_preflight = PASS
canonical_relative_preflight = PASS
optional_body_preflight = PASS or NOT_APPLICABLE
bytes_changed_after_preflight = NO
canonical_snapshot_revalidated = YES
```

If any required value is not proven, publication stops before canonical mutation.
