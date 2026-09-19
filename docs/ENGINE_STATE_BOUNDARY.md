# Engine / State boundary, interface 1

## Resolution

`worker/state_roots.py` is the only new location abstraction. Priority: CLI `--state-root`, then `AI_AGENT_BRIDGE_STATE_ROOT`. Read-only fallback is a marked sibling, then the bundled synthetic fixture. Writes require an explicit root, supported marker, complete layout, independent Git directory, no root symlink/junction, and disjoint Engine/State trees. Linked worktrees are not State roots. There is no fallback to Engine `projects/`.

`bridge_root` in pre-existing APIs means State. `engine_root()` means code/assets/schema location. `git_store` fetch, pull, clean checks, CAS, commit, push, tracked staged-request freshness and recovery all operate on the same State Git repository. Engine code-change detection uses Engine Git. A State update cannot hot-load Engine code.

## Storage and restart

| Location | Authority / durability | Git |
| --- | --- | --- |
| State `projects/*/state.json` | Canonical generation and active-run lease | Private tracked |
| State commands/reports/Owner evidence/Goals | Immutable operational evidence, current pointers | Private tracked |
| State bootstrap/portfolio/registry | Concrete non-secret instance and selection config | Private tracked |
| State `worker/staged-publications/` | Existing tracked publication inbox and processed history | Private tracked |
| State `worker/runtime/` recovery journals, pending reports and WAL | Durable local evidence across restart; never interpret absence as success | Ignored; protected local backup |
| State runtime locks/heartbeat/PID/health/dashboard files | Derived observations and process liveness; stale data is not permission | Ignored; recreate after evidence checks |
| State `worker/logs/` | Private local diagnostics, may contain sensitive output | Ignored; external retention |
| State `worker/config.local.json`, `deployment.local.json` | Explicit deployment policy and non-secret binding | Ignored; installed by operator |
| Environment / protected external credential mechanism | Credential values | Never tracked |
| Engine `tests/fixtures/synthetic-state` | Synthetic test data; not deployment authority | Public tracked |

Staged publication must stay with State: tracked-Git freshness checks must see the same revision as canonical state. Moving its inbox to Engine would weaken an existing check. Recovery remains attached to the State authority, not whichever Engine version happens to be running. A crash restarts reconciliation from canonical active-run identity plus exact local journal/pending-report evidence; it does not regenerate a command or infer permission from a heartbeat.

## Control and self-maintenance

The original Owner control file remains the single authority. Private bootstrap declares its location and local ignored deployment binding pins it. Missing/invalid/read-failed control blocks new admission. Pause is not an active-run kill. Console stop uses the existing exact-run path.

Self-maintenance commands and reports remain State data. Candidate Engine branches cannot approve State migrations or choose their own trusted validator. The current full-access same-user executor is **not** an isolation boundary: split self-maintenance and in-process code adoption are therefore refused at production entry points. Retained generic adoption code is not permission to enable it. Independent review, accepted immutable Engine commit and quiescent operator deployment are required. No additional daemon, queue, HTTP State service or state machine is introduced.

## Validation

Engine CI uses only temporary/synthetic State. State validation imports the independently accepted Engine implementation by a full commit SHA; State contains no implementation copy. Schema/interface version is 1 over unchanged Protocol v2. No canonical State schema migration is performed in this package. Future schema changes require a separately reviewed compatibility plan and exact pre-change snapshot.

Default test fixtures contain invented project identities and local Git remotes. Credentials and private network endpoints are never needed for the default regression suite. Windows-specific containment/Task Scheduler integration still requires Windows acceptance; Linux skips are not Windows evidence.
## Terminal historical State validation

The split does not rewrite append-only Owner evidence merely to satisfy newer operational projection rules. `tools/validate_state.py` therefore distinguishes current execution authority from archival evidence without adding a Protocol state. A project may use archival validation only when all of these are true at the same State snapshot: its canonical status is a Protocol terminal state (`DONE` or `FAILED`), `active_run` is null, the project is explicitly disabled in every configured host registry entry, and it is not Owner-selected in the portfolio. Missing registry evidence does **not** count as disabled.

Operational projects keep the existing `project_view` Owner-thread rules unchanged, including `OWNER_BLOCKER_MISMATCH`. Archival projects still validate Owner event file identity, roots, links and order, cannot retain an active goal, and cannot have a staged publication request. The archival tier relaxes only the assumption that historical corrections must preserve one current resume blocker key after the project has no execution entry. If a terminal project is re-enabled or re-selected, it immediately returns to the operational tier and the full blocker invariant applies again.



## Public release is not runtime command publication

The Protocol publication gateway authorizes commands against private canonical
State. It does not authorize making a repository public. Repository release has
one owner-controlled whitelist process outside the candidate tree. Candidate CI,
`tools/privacy_scan.py`, State validation, retention export and sidecar smoke are
non-authoritative checks, not alternative release mechanisms.

Keep private identifier policies, exact exceptions, cross-file linkage analysis,
provenance mappings and raw audit evidence outside Engine. Credential redaction
of a report does not remove host/path/time linkage. The linter CLI returns only
aggregate diagnostics; its detailed Python API and other operational tool output
remain private. Apply final privacy review to the exact release bytes and public
Git metadata before uploading, separately from real Windows cutover acceptance.
