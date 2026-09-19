# Codex run finalization lifecycle

## Failure mode and confirmed hang

Before this hardening, `bridge_worker.py` launched the configured Codex shim as
one `Popen` tree with `stdin`, `stdout`, and `stderr` set to `PIPE`. The Worker
then repeatedly called `process.communicate()`. The only path to reading
`--output-last-message` and publishing a report was for `communicate()` to
return.

That made stream EOF an accidental completion prerequisite. On Windows, a
Codex-started descendant can inherit a stdout/stderr write handle and later
outlive or detach from the root wrapper. Even after `cmd.exe`, `node.exe`, and
`codex.exe` exit, the descendant keeps the pipe open, so `communicate()` still
cannot return. The four-hour timeout only delays the same structural failure;
the Network Guard cleanup path also used a second `communicate()` and could hit
the same wait.

Historical failure evidence showed that Codex may already have written a valid
successful `BRIDGE_EXECUTION_JSON` final message while the Worker remains in
`communicate()` because a descendant process retains inherited stream handles.
The descendant must be handled through the process-boundary cleanup path before
the Worker can finish finalization.

## Current completion model

Every run has an immutable random run id and its own ignored runtime directory:

```text
worker/runtime/<project>/runs/<run-id>/
  final-message.txt
  stdout.log
  stderr.log
  codex-root.pid
```

Phased commands use that same per-run directory for an immutable command
snapshot, allowlisted manifest, shared projection, one current-phase
projection, and helper-owned progress. They still have one Codex process and
one lifecycle result. The lifecycle does not detect Codex's internal context
compaction; the phased bootstrap instead requires a fresh read of the current
disk projections whenever a summary or loss of context occurs. A successful
phased marker is accepted only after the Worker verifies the checkpoint
contract, and safe text compaction waits until canonical report publication.

The Worker gives stdout/stderr ordinary file handles, never anonymous pipes.
It polls three independent signals:

1. the root launch-gate process exit code;
2. the per-run process boundary's active-process count;
3. `final-message.txt`, parsed only by the hardened strict marker parser after
   size, modification time, and content hash remain stable.

A small Python launch gate is created first and waits on an absent per-run
release file. The Worker attaches that waiting gate to the process scope, then
creates the release file.
Only after release does the gate spawn the actual Codex wrapper. This removes
the race between `Popen` and Job assignment.

## Windows process boundary

The Task Scheduler launcher continues to own the existing outer
kill-on-close Job containing launcher, Worker, and all Worker descendants. Each
Codex run additionally creates a nested Windows Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Windows 8 and later support this nested
layout. Assignment is fail-closed: if the gate cannot join the run Job, it is
terminated before release and Codex is not started.

The run Job is the exact cleanup target. No process-name search or broad
`taskkill` is used by the new execution path, so other Codex sessions, the Codex
desktop app, user Python processes, and the Worker are outside the target. POSIX
uses a new process group with the same ownership semantics.

## Timeout and result rules

- Execution timeout applies only while no valid stable marker exists.
- Final grace begins when the strict marker is confirmed.
- Forced-cleanup timeout bounds the wait after the run Job/process group is
  terminated.

The marker is the business result; exit and cleanup are runtime diagnostics:

| Evidence | Business outcome |
|---|---|
| valid `SUCCESS` marker | `SUCCESS`, including forced cleanup after final |
| valid `FAILED` marker | `FAILED` |
| valid `BLOCKED` marker | `BLOCKED` |
| exit 0 without one strict marker | `FAILED` |
| nonzero exit without one strict marker | `FAILED` |
| execution timeout without one strict marker | `FAILED` with timeout diagnostics |

Raw logs remain local and gitignored. Large logs never control process lifetime:
the configured byte threshold emits a diagnostic event and execution continues.
Cross-run retention remains bounded, and only a bounded, best-effort-redacted
stderr tail can enter a Git report. The strict marker is read only from the
per-run `final-message.txt`; it never depends on scanning either log.

Marker finalization records one explicit classification. A naturally exited
process with no final file is `PROCESS_EXITED_WITHOUT_MARKER`; an unreadable or
oversized independent final file is `MARKER_CAPTURE_FAILED` and transitions the
project to `RECOVERY_REQUIRED` rather than disguising an infrastructure failure
as an ordinary Codex result. Duplicate, partial, corrupt, and invalid markers
are `INVALID_FINAL_MARKER`. Unique per-run paths must not preexist, so a stale
marker from an earlier run cannot be accepted.
