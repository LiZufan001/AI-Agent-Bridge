# Split self-maintenance: Candidate-local Codex executor

Source repository: Public Engine. Canonical maintenance command/report/Goal/Owner records: Private State. The maintenance workdir is an independent Engine-maintenance clone, disjoint from the running Engine and State and with its own Git directory.

A self-maintenance run is admitted only after independent-clone preflight. It ignores ambient user config and execpolicy rules, selects the elevated Windows backend, fixes approval policy to `never`, and selects the built-in `:workspace` permission profile. The Candidate is the complete maintenance source tree. Built-in `:workspace` keeps Candidate and ordinary system temporary directories writable while keeping protected workspace paths such as `.git` read-only. The maintenance Agent works from Candidate-local context supplied by the outer Worker. The outer Worker/Launcher owns production reads, task/context preparation, GitHub interaction, commit/push, restart, and controlled adoption.

The candidate-generation workflow is an unattended Codex run inside the Candidate workspace, followed by the post-run identity/protected-path guard, CI and independent review. Controlled adoption requires the operator-installed Private State `worker/deployment.local.json` binding and the outer Launcher's explicit `--allow-controlled-adoption` startup flag. Adoption proceeds from exact handoff evidence after admissions are drained. Restart evidence lives under State runtime and replacement Workers are explicitly rebound to the same State root.

State validation runs from an accepted immutable Engine SHA selected outside Candidate authority. Candidate code cannot self-approve by changing its validator and validation reference together. System TEMP is disposable scratch. Candidate `.git` remains outside maintenance-Agent mutation authority. Ordinary non-maintenance projects retain their configured execution mode.

Engine rollback preserves the same canonical State and recovery evidence and advances Git history with a forward rollback. State generation and canonical history remain monotonic.

## Operational evidence checklist

- **Before controlled adoption:** record the independent Candidate and base identities, accepted Candidate SHA, current main SHA and known-good SHA, the canonical State binding, completed drain with no active runs, and the rollback/restart evidence reference.
- **During probation:** verify replacement Worker identity and record the terminal handoff result. On failure, preserve the same State and use a forward rollback.
- **After rollback:** a fresh adoption uses rollback main as its base, a new independent Candidate, and a new attempt/command identity.

Controlled adoption is outer-authority owned. Unattended adoption is disabled.
