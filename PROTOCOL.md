# AI Agent Bridge Protocol v2

> Repository boundary: code, schemas and generic policy are in Engine. All `projects/`, concrete `supervisor/*.json`, registry and staged publication paths in this document are relative to the explicitly bound **State** root. Read Engine policy from an independently accepted immutable Engine revision; never infer State from Engine working directory. Protocol v2 semantics below are unchanged.

## Scope and authority

AI-Agent-Bridge is a Git-backed durable control plane between a ChatGPT Supervisor and one or more Codex executors.

The wire protocol remains `protocol_version: 2`. The repository's v2.6 maintenance work is an engineering/profile revision only; it does not change the on-disk wire version.

Protocol authority is layered:

- this file defines the human-readable Protocol v2 semantics;
- `protocol/v2/` contains machine-readable schemas, transitions, invariants, and conformance checks;
- `policies/` contains operational policy such as Supervisor sizing, alert thresholds, and shared security guidance;
- `PROJECT_CONVENTIONS.md` contains repository/project registration rules;
- `worker/` documentation contains Worker/Windows/process implementation details;
- `projects/<project-id>/SUPERVISOR.md`, when present, contains project-specific mutable Supervisor context.

If normative prose and machine-readable protocol files disagree, fail conservatively and reconcile the repository before mutating canonical state.

## Roles

- **ChatGPT Supervisor** — reviews canonical state/evidence and decides what may happen next.
- **Bridge Worker** — local non-AI poller that claims canonical commands, launches Codex, and publishes matching reports.
- **Codex Executor** — executes exactly one claimed command in the configured target workdir and returns one result.
- **GitHub** — persistent control-plane store and append-only history transport.
- **Human fast lane** — optional manual ChatGPT/Codex path that must obey the same distributed state/lease rules.
- **Owner** — may provide decisions, credentials through approved local channels, physical-device actions, or other evidence that automation cannot safely produce itself.

## Project layout and history model

```text
projects/<project-id>/
├── MISSION.md
├── SUPERVISOR.md        # optional project-specific context
├── WORKDIR.md           # required before local execution
├── state.json
├── commands/
│   ├── command-001.md
│   └── ...
├── reports/
│   ├── report-001.md
│   └── ...
└── owner-actions/
    ├── README.md
    ├── owner-action-001.md
    └── ...
```

`state.json` is the only canonical pointer to the current project state.

Published command, report, and owner-action files are append-only history. Never overwrite an old id to revise history. Corrections/progress use new ids. Stale/orphan historical files are harmless when canonical state does not point to them.

Command and owner-action ids are monotonically increasing within a project; gaps are allowed.

## Canonical state

A Protocol-v2 state contains at least:

```json
{
  "protocol_version": 2,
  "project_id": "example",
  "status": "REPORT_READY",
  "generation": 1,
  "latest_command": 3,
  "latest_report": 3,
  "last_reviewed_report": 3,
  "active_run": null
}
```

Core allowed states are exactly:

- `COMMAND_READY` — one canonical command is ready to be claimed.
- `CODEX_RUNNING` — one command is claimed and one distributed execution lease is active.
- `REPORT_READY` — the newest report is waiting for Supervisor review.
- `FINALIZING` — the Supervisor has published the final delivery command and it is waiting to be claimed.
- `FINAL_REPORT_READY` — final delivery completed and awaits Supervisor acceptance.
- `HUMAN_REQUIRED` — safe autonomous progress is blocked on a genuine owner action/decision/input.
- `RECOVERY_REQUIRED` — an interrupted/expired/uncertain run cannot be safely reconciled automatically.
- `DONE` — the Supervisor accepted mission-level final delivery; no further commands may be issued.
- `FAILED` — unrecoverable Bridge infrastructure failure requiring inspection.

`OWNER_IN_PROGRESS` is **not** a top-level state.

Compatibility/convenience fields such as `human_required` or `finalized`, when present, are subordinate to canonical `status` and must never contradict it.

Machine-readable state constraints live in `protocol/v2/state.schema.json`.

## State cycles

Normal cycle:

```text
REPORT_READY -> COMMAND_READY -> CODEX_RUNNING -> REPORT_READY
```

Final cycle:

```text
REPORT_READY -> FINALIZING -> CODEX_RUNNING -> FINAL_REPORT_READY -> DONE
```

Owner-action recovery cycle:

```text
HUMAN_REQUIRED -> COMMAND_READY -> CODEX_RUNNING -> REPORT_READY
```

Manual fast lane may atomically combine publication and claim:

```text
REPORT_READY -> CODEX_RUNNING -> REPORT_READY
```

and for explicit manual final delivery:

```text
REPORT_READY -> CODEX_RUNNING -> FINAL_REPORT_READY -> DONE
```

Declarative transition metadata is in `protocol/v2/transitions.json`.

## Generation and compare-and-swap

`generation` is the distributed compare-and-swap token.

Work prepared against generation `G` must never overwrite canonical state after it has advanced beyond `G`.

Command publication, Worker claim, report publication, and recovery transitions advance generation according to the existing Protocol-v2 rules. Normal publication/claim/report transitions consume one generation each.

An ordinary Supervisor manual-priority supersede is one normal command
publication at the unclaimed queue boundary:

```text
COMMAND_READY/G/C/R/active_run=null
  -> COMMAND_READY/G+1/N/R/active_run=null
```

The new command has `source=manual_chatgpt`, `kind=EXECUTE`, and
`supersedes_command_id=C`. The old scheduled command file is immutable and
remains inspectable. This event never interrupts or claims the old command;
the ordinary Worker re-reads the canonical pointer and can claim only `N`.
The CAS snapshot must still prove the old command is a fully valid canonical
`scheduled_chatgpt`/`EXECUTE` command with matching generation/report identity,
no active lease, and no prior replacement relation. A stale scheduled
Supervisor cannot resurrect it because its publication CAS no longer matches
the canonical `COMMAND_READY/G+1/N/R` snapshot.

The manual fast lane performs command publication + claim atomically in one Git commit but intentionally consumes two **logical** generations: publication `G+1`, claim `G+2`.

Every component must re-read current state immediately before a canonical write. SHA/CAS mismatch or changed generation is a lost race: abort/re-evaluate; never force stale state over newer state.

## Command contract

Every Protocol-v2 command contains exactly one machine-readable metadata line near the top:

```text
<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt","based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->
```

Required metadata:

- `command_id`;
- `source`: `scheduled_chatgpt`, `manual_chatgpt`, `user_direct`, or `finalizer`;
- `based_on_report`;
- `expected_generation`;
- `kind`: `EXECUTE` or `FINALIZE`.

Optional automatic-Worker executor override:

```json
{
  "executor": {
    "model": "gpt-5.6-luna",
    "reasoning_effort": "high",
    "service_tier": "fast"
  }
}
```

The exact normalized command schema is `protocol/v2/command.schema.json`.

An executor override may select model/reasoning effort and the narrowly
validated command-scoped Fast request tier. `service_tier=fast` maps to the
run-local Codex argument `-c service_tier="fast"`; it does not lower
`reasoning_effort`. Missing service-tier metadata preserves existing command
behavior. The override must not alter executable, workdir,
full-access/sandbox/approval mode, timeout, network guard, secret handling, or
arbitrary runtime flags. Unknown/invalid metadata is rejected rather than
guessed. The Worker report records requested/effective CLI configuration only;
it does not claim that the remote service actually served Fast unless that is
observable from Codex evidence.

The Worker refuses a command when any of these are stale/inconsistent:

- `command_id != state.latest_command`;
- `based_on_report != state.latest_report`;
- `expected_generation != state.generation` at the pending-command boundary;
- canonical state is no longer executable;
- an execution lease already exists.

This is the stale-command guard.

The optional `withdraws_command_id` metadata field is used by the atomic
withdraw-and-manual-start replacement to explicitly name the earlier command
the Owner withdrew before execution. It is a historical withdrawal reference,
not a `supersedes_command_id` relation, and the two fields must not appear
together.

### Unclaimed command repair

Protocol v2 has one narrow operator-only repair transition for a command that
was published but is deterministically impossible for the Worker to claim:

```text
COMMAND_READY/G/C/R/active_run=null
        -- operator.repair_unclaimed_command -->
COMMAND_READY/(G+1)/N/R/active_run=null
```

The operator must identify the exact canonical command `C`, prove that the
command itself has a deterministic metadata/contract error, and provide a
complete new command `N` explicitly.  Network-guard decisions, dirty
worktrees, missing workdirs, owner feedback, timeouts, Worker crashes,
execution uncertainty, `CODEX_RUNNING`, and `RECOVERY_REQUIRED` are not
repair conditions.

Repair requires `status=COMMAND_READY`, `active_run=null`, no report for the
target, an unchanged canonical target snapshot, and a replacement satisfying
all of the following:

- `command_id=N` is a new monotonic command id;
- `based_on_report` equals the current `latest_report=R`;
- `expected_generation` equals `G+1`;
- `supersedes_command_id` equals the exact current canonical command id `C`;
- the complete command contract, source, kind, executor override, and body
  pass normal Protocol-v2 validation.

`supersedes_command_id` is an immutable historical replacement relation.  It
does not mean that the old command was claimed, executed, successful, or
failed.  Ordinary commands omit it; an operator repair replacement must use
it.  A relation must point to an earlier existing command, must not
self-reference, and must not form a cycle.  The repair command is published
with the new command file and the canonical state in one existing Git CAS
commit.  The repair consumes exactly one generation and never creates a
`run_id`, claims a lease, starts Codex, publishes a report, or enters
`RECOVERY_REQUIRED`.

#### Staged replacement adoption

When Supervisor has already written the replacement command at its canonical
`commands/command-N.md` path, the operator uses the separate
`operator.adopt_staged_repair` transition rather than normal repair. It is
still a queue-boundary repair, not a claim or a report shortcut:

```text
COMMAND_READY/G/C/R/active_run=null
        -- operator.adopt_staged_repair -->
COMMAND_READY/G+1/N/R/active_run=null
```

The operator must re-read and bind the exact canonical state snapshot and its
SHA, the target command SHA, and the complete staged replacement bytes and
SHA. The target must be the current unclaimed command with no report and a
deterministic Protocol-v2 contract error; the staged replacement must have no
report, be a complete normal command, use a new monotonic id, match
`based_on_report=R` and `expected_generation=G+1`, and use
`supersedes_command_id=C`. The relation must be historical, non-self,
non-future, acyclic, and free of conflicting superseders. Any changed state,
generation, hash, or byte causes a fail-closed CAS refusal.

This transition never overwrites an existing command. Its `publish_cas`
payload contains only the advanced `state.json`; command `C` and staged
command `N` remain immutable and byte-for-byte unchanged. It preserves
`latest_report=R`, `active_run=null`, and the `COMMAND_READY` status, and
creates no run id, lease, report, recovery record, or executor side effect.

### Atomic owner withdrawal and manual start

When an Owner changes the requirements before an unclaimed manual command has
been executed, Protocol v2 exposes one narrow operator-only transition. It is
not a cancel operation and it does not create a synthetic report:

```text
COMMAND_READY/G/C/R/active_run=null
        -- operator.withdraw_and_manual_start -->
CODEX_RUNNING/(G+2)/N/R/active_run=new-run
```

The single Git CAS transaction contains both logical steps. First, publication
advances the logical generation from `G` to `G+1`, preserves `command-C.md`
byte-for-byte, records the Owner withdrawal, and publishes a complete new
manual `EXECUTE` command `N`. Then the human fast-lane claim advances the
logical generation from `G+1` to `G+2` and creates the ordinary execution lease
and `run_id` through the same `bridge_manual.py`/`git_store.py` path used by
the existing manual fast lane.

The operation is fail-closed unless the canonical state is exactly
`COMMAND_READY` with `active_run=null`, `C` is the current `latest_command`,
`C` has no report, `C` is a valid `manual_chatgpt`/`EXECUTE` command, the
canonical generation/state SHA/command SHA still match, and `N` passes the
complete command contract. `N` must use `based_on_report=latest_report`, the
publication generation as `expected_generation`, a new monotonic id, and
`withdraws_command_id=C`; it must not use `supersedes_command_id`.

There is no `REPORT_READY` intermediate state, no `report-C`, and no second
lease implementation. The immutable state record in `withdrawn_commands`
contains the target id and SHA, reason, timestamp, replacement id, and the
resolution `owner_withdrew_unclaimed_command_before_execution`; it is not a
report. A withdrawal relation must point to an earlier existing command, must
not self-reference, point to the future, or create duplicate/cyclic history.
If any state, command, SHA, or CAS value changes, the operation aborts without
publishing a replacement or claiming a run. A project already in
`CODEX_RUNNING` is never cancelled by this transition.

### Optional phased task body

Protocol v2 commands may optionally carry a strict execution-layer declaration
without changing the `bridge-command` metadata, canonical state machine, lease,
generation, CAS, report, or no-auto-rerun contract:

```text
<!-- bridge-phased-task: {"schema_version":1,"phases":["A","B"]} -->
<!-- bridge-global:start -->
shared rules and acceptance context
<!-- bridge-global:end -->
<!-- bridge-phase:A:start -->
one coherent vertical slice
<!-- bridge-phase:A:end -->
<!-- bridge-phase:B:start -->
the next dependent vertical slice
<!-- bridge-phase:B:end -->
<!-- bridge-final-acceptance:start -->
full integration and final acceptance
<!-- bridge-final-acceptance:end -->
```

The phased header envelope may also preserve at most one human-readable
Markdown H1 before the first section marker, in the strict form
`# Command <three-or-more decimal digits>...` (for example,
`# Command 123 — Synthetic phased example`). The H1 is header-only: it is
retained in the exact task snapshot but is not part of `global.md`, the current
phase projection, or final acceptance. Arbitrary unmarked prose and other
headings remain invalid outside phased section markers; the existing
`bridge-command` metadata remains authoritative for command identity.

Only the ordered prefixes `A`, `A,B`, `A,B,C`, and `A,B,C,D` are supported.
The declaration and sections are parsed strictly: every declared section must
appear once, in order, with non-empty content; malformed JSON, unknown fields,
missing or undeclared sections, duplicate/overlapping markers, nested markers,
and five or more phases are rejected before Codex starts. A command without the
phased declaration remains an ordinary command and receives its existing
prompt unchanged.

The Worker stores the exact claimed command bytes, identity, and hashes in the
current execution's ignored `worker/runtime/<project>/runs/<run-id>/` directory.
It materializes only `global.md` and `current-phase.md`; the checkpoint helper
atomically advances the one current projection and `phase-progress.json` in the
same Codex execution. `task-snapshot.md` is an integrity/recovery source, not
normal prompt context. A phased `SUCCESS` is accepted only when the snapshot,
manifest, ordered progress, and final-acceptance contract verify. Publication
failure still uses the existing pending-report/recovery evidence and never
starts another Codex execution. Successful evidence compaction occurs only
after canonical report publication; existing bounded run retention remains the
long-term cleanup mechanism.

## Supervisor publication protocol

When canonical state is `REPORT_READY`, the Supervisor:

1. reads `MISSION.md`, `state.json`, newest relevant unreviewed report(s), and required project/policy context;
2. records current `generation = G` and `latest_report = R`;
3. reviews only the source/environment evidence needed to choose the next bounded command;
4. prepares a fresh command with `based_on_report=R` and normally `expected_generation=G+1`;
5. creates a new append-only command file;
6. re-fetches canonical `state.json`;
7. only if the same `REPORT_READY/G/R` snapshot remains canonical, CAS-updates state to point to the command;
8. successful publication sets canonical generation to the command's expected generation.

A lost race may leave an orphan command file. Do not delete/overwrite it merely to make history look clean; the Worker ignores it because `state.json` does not point to it.

If a later command explicitly repairs an earlier deterministic-invalid v2
command, repository conformance may retain the old command as immutable
history only when the old command is no longer canonical, there is exactly
one later fully valid superseder, the relation names the exact old command,
and the relation is acyclic and within the canonical history.  This is an
exception for that explicit relation only.  An unrelated invalid historical
command remains a conformance failure.

Scheduled Supervisor must not publish a normal competing command while canonical state is `COMMAND_READY`, `CODEX_RUNNING`, `FINALIZING`, `FINAL_REPORT_READY`, `RECOVERY_REQUIRED`, `FAILED`, or `DONE`.

`HUMAN_REQUIRED` uses only the owner-action recovery rules below.

When the Supervisor runs outside the Windows checkout, its normal publication
transport is the non-canonical staged request described in
`protocol/v2/PUBLICATION_PREFLIGHT.md`. It may commit only
`worker/staged-publications/requests/request-<request_id>.json` with the exact
final command content and byte digest. The local Worker gateway then performs
  the complete deterministic pre-claim validation, revalidates the fresh
  canonical project snapshot, and publishes the command plus state pointer using
  the same non-force Git CAS boundary. The staged file is transport evidence; it
  is not a second state machine and cannot claim, execute, report, recover, or
  retry stale work. A successful scheduled publication also advances
  `last_reviewed_report` to the exact `based_on_report` it reviewed. After
  inspection, the local gateway may move the unchanged staged blob to
  `worker/staged-publications/history/<outcome>/` as a non-canonical lifecycle
  record; this prevents historical requests from starving the bounded inbox
  while preserving their Git audit history. A cloud caller must never write
  canonical project state or command files directly.

Operational command sizing/model/evidence policy is in `policies/supervisor.md`.

## Execution lease and report publication

Before Codex touches a target project, the Worker atomically claims the canonical pending command.

Claim changes state to `CODEX_RUNNING`, advances generation, and records an `active_run` containing at least:

```json
{
  "run_id": "run-004-...",
  "command_id": 4,
  "source": "scheduled_chatgpt",
  "based_on_report": 3,
  "base_generation": 7,
  "claimed_generation": 8,
  "claimed_at": "...",
  "lease_expires_at": "..."
}
```

Automatic runs may additionally record Worker host/pid and the effective executor profile passed to Codex CLI. Those fields are execution evidence, not proof of what a remote service ultimately ran.

Only the holder of the exact matching `run_id`, `command_id`, and `claimed_generation` may publish that run's report.

While `CODEX_RUNNING`, every Supervisor and normal manual lane must no-op for competing work.

A successful normal report publication moves to `REPORT_READY`; a successful strict final-delivery report moves to `FINAL_REPORT_READY`.

Reports must distinguish verified facts from suggestions and should record normalized core fields described by `protocol/v2/report.schema.json`, plus the actual changes, tests/evidence, commit/push status, limitations, and human-input requirement when relevant.

Worker process-finalization details and the strict execution-result marker contract live in `worker/CODEX_LIFECYCLE.md` / `worker/README.md`.

## No automatic rerun

A Codex command may already have produced local, Git, remote-service, email, or other side effects even when the control plane becomes uncertain.

Therefore the original command must **never** be automatically rerun merely because of:

- Git push/rebase race;
- Worker/network interruption;
- process cleanup ambiguity;
- expired execution lease;
- lost local journal;
- uncertain report publication.

Uncertainty is handled by preserving evidence and reconciling identity, not replaying the command.

## Recovery and Git race safety

For an in-run network-guard interruption, required durability order remains:

```text
terminate Codex
-> collect local evidence
-> atomically persist recovery journal
-> persist pending report
-> attempt remote RECOVERY_REQUIRED CAS
```

If GitHub is unavailable, local recovery evidence remains durable and the original command is not rerun.

Deferred recovery reconciliation runs only after GitHub is reachable and may transition to `RECOVERY_REQUIRED` only when project id, canonical status, generation, run id, command id, and claimed generation still identify the exact interrupted run.

Changed generation/run identity, an already-published report, manual recovery, or terminal state means the journal is superseded/conflicting; do not overwrite newer canonical state.

Lease expiry is an orphaned-run safety fuse, not the normal recovery mechanism. An expired run transitions to `RECOVERY_REQUIRED` and clears stale active-run identity; it does not create a continuation command.

### Explicit Recovery Resolution

`RECOVERY_REQUIRED` is an intentional human gate. A reconciled recovery journal
does not mean that the interrupted product work succeeded; it only means that
the Worker durably recorded the interruption and published the recovery gate.

The operator-only `worker/recovery_resolution.py` boundary provides the one
explicit resolution transition:

```text
human forensic review
-> bind the exact project/command/run/claim identity
-> recompute and confirm the pending report SHA-256
-> publish the interrupted report as canonical evidence
-> RECOVERY_REQUIRED -> REPORT_READY
```

The canonical event is `operator.resolve_recovery` and consumes exactly one
generation. It requires the canonical snapshot to remain
`RECOVERY_REQUIRED/G/latest_command/latest_report/active_run=null`, the exact
reconciled recovery journal, the exact pending-report path, and the reviewed
report digest. The report outcome is `BLOCKED` (not `SUCCESS`) and its body
retains the interruption classification and `external_side_effects_unknown`
fact. Active recovery-only fields such as `recovery_reason` and
`last_execution_error` are removed from the resolved state; a bounded
`recovery_note` may retain the resolution identity.

The historical network-guard path predates the pending-report metadata
sidecar and therefore may have only the journal plus
`pending-report-<id>.md`. Recovery Resolution binds that legacy artifact at
operator time by requiring an explicitly reviewed SHA-256. It must fail closed
on a changed digest, changed state, changed journal identity, a different
canonical report, or a newer canonical state. Repeating the exact resolution
after `REPORT_READY` returns `ALREADY_RESOLVED` without another generation or
commit.

This is distinct from deferred recovery reconciliation:

```text
network interruption -> durable journal -> RECOVERY_REQUIRED
human review -> publish interrupted canonical report -> REPORT_READY
Supervisor review -> may publish a new command (a new command id)
```

Recovery Resolution never claims a command, starts Codex, creates a command,
reruns the interrupted command, or automatically continues product work.

If another Bridge commit advances the repository while Codex is running, the Worker may replay only the **already-produced report/state update** on top of the newer branch after re-validating the same execution lease. It may never rerun Codex because a push was rejected.

Implementation details are in `worker/recovery_journal.py`, `worker/README.md`, and `worker/CODEX_LIFECYCLE.md`.

## Owner action and HUMAN_REQUIRED recovery

`HUMAN_REQUIRED` is the project-level canonical pause state for a genuine human dependency.

Human progress is orthogonal append-only evidence:

```text
AWAITING_OWNER -> OWNER_IN_PROGRESS -> OWNER_REPORTED_DONE
```

Verification is a separate dimension:

```text
PENDING | VERIFIED | FAILED
```

Machine-readable owner-action constraints are in `protocol/v2/owner-action.schema.json`.

### Owner-action threads

Owner-action records live under:

```text
projects/<project-id>/owner-actions/
```

New records should use immutable event identity/thread fields when practical:

```yaml
action_id: owner-action-002
root_action_id: owner-action-001
relates_to: owner-action-001
blocker_key: example-blocker
owner_status: OWNER_IN_PROGRESS
verification_status: PENDING
recorded_at: 2024-01-01T00:00:00Z
evidence_summary: Owner has started the requested action.
```

- `action_id` should match the filename;
- `root_action_id` identifies the first event in the same thread;
- `relates_to` normally identifies the immediately preceding event;
- current progress comes from the newest event in the same thread;
- corrections are new events, never rewrites;
- old records without thread-link fields remain valid legacy roots and must not be rewritten solely for migration;
- conflicting/ambiguous ordering means remain conservative and do not resume.

Allowed owner-status meaning:

- `AWAITING_OWNER` — requested action has not been reported as started;
- `OWNER_IN_PROGRESS` — owner explicitly started it and it is still underway;
- `OWNER_REPORTED_DONE` — owner reports completion and supplied whatever evidence is currently available.

`OWNER_IN_PROGRESS` normally pairs only with `verification_status=PENDING` and is never resolution evidence by itself.

### Entering HUMAN_REQUIRED

Protocol v2 currently preserves the existing owner-notification flow: if a genuine blocker has not already been notified, the Supervisor first publishes one narrow notification-only `EXECUTE` command that makes no target-project source changes, sends one clear notification through the configured Worker-owned channel, reports delivery status, and performs no unrelated development.

After that notification report is reviewed, the Supervisor rechecks current evidence before entering `HUMAN_REQUIRED`. Do not repeatedly notify the same unresolved blocker.

A future control-plane notification design is a v3 candidate only; see `docs/ROADMAP.md`.

### OWNER_IN_PROGRESS

When canonical state is `HUMAN_REQUIRED` and the newest matching owner event is `OWNER_IN_PROGRESS`, Scheduled Supervisor must:

- remain `HUMAN_REQUIRED`;
- no-op for autonomous development;
- not publish a normal command;
- not publish a verification command before owner completion unless the owner explicitly asks for safe independent concurrent verification;
- not repeat the same owner notification/liveness email;
- not mutate canonical state, generation, lease, or compatibility flags merely to record progress.

Recording owner progress itself is non-canonical and consumes no Bridge generation.

### OWNER_REPORTED_DONE + PENDING

Owner self-report is a verification trigger, not proof.

From the same canonical `HUMAN_REQUIRED` snapshot, the Supervisor may publish exactly one narrow verification-only `EXECUTE` command using the normal new-id / `based_on_report` / `expected_generation=G+1` / CAS discipline.

Verification command must avoid unrelated development and prefer read-only/local checks.

Verification result must include one exact marker:

```text
OWNER_ACTION_VERIFY_JSON: {"action_id":"owner-action-001","verification":"VERIFIED"}
```

or:

```text
OWNER_ACTION_VERIFY_JSON: {"action_id":"owner-action-001","verification":"FAILED"}
```

plus concrete non-secret evidence.

### OWNER_REPORTED_DONE + VERIFIED

The Supervisor may resume directly from `HUMAN_REQUIRED` without redundant Worker verification only after sanity-checking that evidence is current, relevant, specific, and sufficient for the actual blocker.

Direct resume still uses normal command publication/CAS and moves `HUMAN_REQUIRED -> COMMAND_READY` with a fresh generation.

### OWNER_REPORTED_DONE + FAILED

Remain `HUMAN_REQUIRED` unless a concrete safe autonomous step directly addresses the verified failure. If a materially new owner action is required, open a new owner-action thread rather than repurposing the old blocker thread.

### Owner-action safety

- New owner evidence is the only normal trigger for leaving `HUMAN_REQUIRED`.
- An active `CODEX_RUNNING` lease wins; owner evidence must not race it.
- Out-of-band manual Codex App verification while paused should be read-only. If verification requires project writes, use the Bridge verification-command path so the distributed lease protects those changes.
- Owner records must never contain credentials/secrets.

## Manual fast lane

Manual acceleration is first-class but not a bypass.

`worker/bridge_manual.py` / `$bridge-manual` must publish and claim the manual command **before** the manually driven Codex turn changes any target-project file.

Normal manual start is allowed only from:

- `REPORT_READY`; or
- `COMMAND_READY` when the canonical unclaimed command is a normal `scheduled_chatgpt` `EXECUTE` command, which the manual command may supersede before claim.

A claimed command cannot be superseded.

`FINALIZING`, `FINAL_REPORT_READY`, `HUMAN_REQUIRED`, `RECOVERY_REQUIRED`, `DONE`, and existing `CODEX_RUNNING` reject a normal manual start.

Manual `FINALIZE` may start only from `REPORT_READY`.

For base generation `G`, manual publication+claim is one atomic commit but records:

```text
publication_generation = G+1
claimed_generation     = G+2
canonical generation   = G+2
```

Only after a successful manual claim may the current Codex session execute the command.

Manual finish must publish the report using the exact returned run id, command id, and claimed generation. If canonical identity changed after Codex work finished, preserve pending evidence, do not overwrite newer state, and never rerun automatically.

The manual helper uses a short-lived temporary Bridge clone for CAS writes so it does not contend with the Worker's long-lived checkout; the distributed lease remains authoritative.

## Finalization contract

Mission completion is Supervisor-owned.

When all mission acceptance criteria are satisfied, the Supervisor must not mark `DONE` directly from an ordinary report. It publishes one `FINALIZE` command (`source=finalizer`) or explicitly uses manual finalization.

Finalizer must:

1. avoid new optimization except fixes required by final verification;
2. produce a mission-level delivery report covering goal, completed outcomes, relevant tests/evidence, repository/branch/commit, and remaining limitations;
3. invoke the configured completion-notification mechanism with the mission-level final report;
4. only after successful delivery include exactly:

```text
BRIDGE_FINAL_JSON: {"final_delivery":"SUCCESS","completion_email":"SENT"}
```

Worker/manual helper parses this marker strictly. Similar prose does not count.

A verified finalizer report moves to `FINAL_REPORT_READY`. Only after reading and validating that report may the Supervisor transition to `DONE`.

Worker/Codex must never self-mark `DONE`.

## Alerts and observability

Alerts, Worker health files, launcher health files, local logs, and recovery journals are non-canonical evidence/side effects. They must never become a second state machine or alter generation/lease/recovery semantics.

Supervisor/Worker alert thresholds, dedupe, recipient routing, and notification safety live in `policies/alerts.md`.

Shared credential/evidence rules live in `policies/security.md`.

## Local Worker / process implementation

Local OS single-instance locking, Windows Job Objects, Codex final-message stability detection, launcher restart/backoff, remote-project validation, network-probe details, runtime log retention, and full-access CLI construction are implementation responsibilities documented under `worker/`.

Those implementation mechanisms must preserve the protocol invariants in `protocol/v2/INVARIANTS.md`; they are not additional canonical states.

## Conformance and evolution

Run:

```text
python protocol/v2/check_conformance.py
```

to validate current repository state/history against the machine-readable v2 profile.

Protocol v2 compatibility maintenance must preserve generation/CAS, execution lease, stale-command rejection, recovery identity, no-auto-rerun, owner-action, and finalization semantics.

Potential incompatible simplifications (for example collapsing finalization states, splitting CAS revision from logical event sequence, removing redundant compatibility flags, or moving owner notification entirely to the control plane) are documented in `docs/ROADMAP.md` and must not be introduced piecemeal into v2.
