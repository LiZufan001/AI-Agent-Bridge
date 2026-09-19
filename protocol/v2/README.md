# Protocol v2 engineering profile

This directory is the machine-readable companion to `PROTOCOL.md`.

The on-disk wire protocol remains `protocol_version: 2`. The repository may refer to this maintenance layer as **v2.6**, but v2.6 is an engineering/profile revision, not a new wire version and does not require project-state migration.

## Authority and compatibility

- `PROTOCOL.md` defines the human-readable Protocol v2 semantics.
- Files in this directory encode the stable parts of those semantics in machine-readable form for validation and tooling.
- If prose and machine-readable files disagree, automation must fail conservatively; do not guess which side is newer. Reconcile the repository before changing canonical project state.
- v2.6 must not change generation/CAS, execution lease, stale-command rejection, no-auto-rerun, recovery, finalization, or owner-action semantics.

## Files

- `state.schema.json` — canonical `state.json` core contract.
- `command.schema.json` — normalized `bridge-command` metadata contract.
- `report.schema.json` — normalized report-header contract.
- `owner-action.schema.json` — normalized owner-action event contract.
- `staged-publication.schema.json` — non-canonical cloud-to-local Supervisor publication envelope.
- `transitions.json` — declarative map of supported canonical transitions.
- `INVARIANTS.md` — small set of protocol invariants that all implementations share.
- `check_conformance.py` — standard-library-only repository conformance check used by CI.
- `worker/protocol_core.py` — shared standard-library-only runtime implementation of stable v2 semantics (metadata, generation/CAS decisions, lease identity, manual eligibility, unclaimed-command repair eligibility, and final markers).
- `worker/command_repair.py` — explicit operator-only, evidence-bound repair publication for one deterministic-invalid unclaimed command; it includes both normal replacement publication and staged adoption of an already-written immutable replacement, and never invokes execution or recovery.
- `worker/supervisor_publication_gateway.py` — local-only staged publication gateway; it reads committed request blobs, runs exact-byte preflight, and uses the shared Git CAS store for the command/state pair.

`protocol/v2/*` is the machine-readable normative specification. The Python
runtime core is intentionally explicit rather than a dynamic workflow engine;
unit tests and `check_conformance.py` form the drift boundary between the
specification and Worker/Manual behavior.

The schemas intentionally constrain core fields while allowing documented compatibility fields where old project history already contains them. Historical command/report/owner-action files remain append-only and are never rewritten merely to satisfy a newer profile.

The staged publication schema is transport input, not canonical state. A cloud
Scheduled Supervisor may commit one request under
`worker/staged-publications/requests/request-<request_id>.json`, but it must not
write `projects/<project-id>/state.json` or `projects/<project-id>/commands/`
directly. The Windows Worker reads the exact request blob from Git HEAD after
normal sync, validates the envelope and the complete command bytes, and then
publishes the canonical command plus state pointer in one ordinary non-force
  CAS commit; that state update records
  `last_reviewed_report=based_on_report`. Staged requests do not authorize a
  claim, execution, report, recovery, or semantic stale retry.

After inspection, the local gateway moves the unchanged request bytes to
`worker/staged-publications/history/<outcome>/` in a separate non-canonical
Git lifecycle commit. This keeps the active inbox bounded without editing the
old request; its Git rename history remains the audit trail. Terminal history
entries, including `already_applied` and stale requests, do not consume a
future project's publication slot. A lifecycle push failure leaves the exact
request in the inbox for safe retry and cannot repeat a successful canonical
publication.

The one v2 repaired-history exception is relation-bound: a later fully valid
command with `supersedes_command_id` may cover only the exact earlier invalid
command it names, while cycles, self/future references, duplicate
superseders, and unrelated invalid history remain failures.

`operator.adopt_staged_repair` is the narrow queue-boundary variant for a
replacement that Supervisor has already written at its canonical
`commands/command-N.md` path. It requires the exact pre-CAS state snapshot,
target bytes, and staged replacement bytes to remain unchanged, then publishes
only the `state.json` pointer through `git_store.publish_cas`. It consumes
`COMMAND_READY/G/C/R -> COMMAND_READY/G+1/N/R`, preserves both command files
byte-for-byte, and creates no run, lease, report, recovery record, or executor
side effect.
