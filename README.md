# AI-Agent-Bridge Public Engine

Reusable Git-backed Protocol v2 coordination, Worker, Coordinator, Launcher, Supervisor policy and loopback Owner console. This repository contains **programs and synthetic fixtures**, not an Owner's operational portfolio or credentials.

## Two roots, one canonical authority

`ENGINE_ROOT` contains this code. A separate, independent State Git repository contains `bridge-state.json`, `projects/`, `supervisor/bootstrap.json`, `supervisor/portfolio.json`, `worker/remote-projects.json` and staged publication history. Product source repositories are separate from both. Existing APIs named `bridge_root` receive the **State root**. They do not synchronize or merge the Engine repository.

Set `AI_AGENT_BRIDGE_STATE_ROOT` to the absolute State root or pass `--state-root`. Mutation requires an explicit, marked independent State Git repository disjoint from Engine. Read-only tools can discover a marked sibling named `<engine-directory>-State`; otherwise they use `tests/fixtures/synthetic-state`. A missing production binding never silently becomes writable fixture state.

```powershell
$Engine = (Get-Location).Path
$State = (Resolve-Path '..\<engine-directory>-State').Path
$env:AI_AGENT_BRIDGE_STATE_ROOT = $State
python -B worker/dashboard_service.py open --state-root $State --port 8766
```

This opens a **read-only** console. Enabling controls requires explicit `--allow-controls` and a verified local deployment binding. The service binds only `127.0.0.1`. It does not manufacture Worker availability when no Worker is running.

## Try without any private repository

Python 3.11+ and Git are needed. Standard-library tests require no credentials and never execute a real product command.

```text
python -B protocol/v2/check_conformance.py
python -B tools/validate_state.py --state-root tests/fixtures/synthetic-state
python -B tools/sidecar_smoke.py
python -B tools/check_test_isolation.py --output ../engine-tests.json
python -B tools/privacy_scan.py --root . --tree --allowlist tools/public-test-exceptions.json --json ../privacy.json --report ../privacy.md
```

The built-in scanner is a **content/history linter, not public-release approval**.
Its CLI stdout, `--json` and `--report` contain aggregate rule counts only; output
files must be new and outside the scanned tree. A successful exit means only that
this bounded lint found no unexplained matches. Every summary explicitly sets
`publication_authorized` to false. No runtime command-publication authority is changed.

A local reviewer may supply `--identifier-policy <external-policy.json>` for known
private identifiers. This policy and owner-specific exact exception records stay outside the
Engine and outside CI. The shipped `tools/public-test-exceptions.json` contains
only byte/line/rule-bound exceptions for two deliberately invalid synthetic
mailbox literals in a scanner regression test. It contains no private terms or
matched-value digests and is not release authority. Identifier hashes can be dictionary-matched: hashing is
not anonymization. In-process `audit()` details contain file locations and file
content hashes, and remain private. The exact exception format is now
`path`, `line`, `rule`, `file_sha256`, `reason`; legacy `match_sha256` entries are
rejected, not silently reused. Renew exceptions only after reviewing the exact bytes.

`validate_state.py` normalizes its input path to an absolute root; runtime entry points require explicit absolute paths. Its strict State audit has two fail-closed tiers: every non-terminal, enabled, or Owner-selected project uses the full operational `project_view` invariants; a project is archival only when Protocol says it is terminal (`DONE`/`FAILED`), `active_run` is null, it is explicitly disabled on every configured host, and it is not Owner-selected. Archival validation still checks state/Protocol, goal absence, Owner event identity/root/link/order, staged-publication exclusion, privacy, and immutable bytes; it only stops treating historical blocker-key corrections as current resume authority. Re-enabling or re-selecting the project immediately restores full operational validation.

In a private staging workspace, inspect a prospective clean first commit and all reachable history **before any public upload**:

```text
python -B tools/privacy_scan.py --root . --history --allowlist tools/public-test-exceptions.json --require-clean-history --json ../initial-history.json
```

The single-root requirement applies only to initial publication. Subsequent CI scans all reachable history without requiring that normal development stop at one commit. Use a deliberately public-safe Git author identity; commit metadata is audited too. Scanner rules are conservative heuristics, not a proof that arbitrary new content is safe.
The two repository workflows run regression/lint only, never publish a release,
never carry an owner's private policy or State, and do not upload raw diagnostics.
They cannot protect content already pushed to a public repository.

The release decision stays with one independently trusted, private whitelist
process outside candidate code. It binds reviewed paths and bytes, checks dependency
closure and provenance, runs independent secret checks and integration acceptance,
then exports only that exact approved snapshot. A candidate, a green linter, or
candidate-controlled CI cannot approve itself. Unknown files and incomplete checks
block publication; runtime reports and retention archives remain private.

## Safety spine

Scheduled Supervisor -> compact portfolio accounting -> exactly one focus -> existing staged publication gateway -> generation/CAS -> active-run claim/lease -> executor -> exact-run Report. Protocol v2 transitions, publication preflight, Owner execution control and recovery evidence remain the authority. Uncertainty never authorizes replay, generation rewind, clearing an active run or direct canonical edits.

Execution-control and heartbeat locations belong to private instance bootstrap. The operator installs a **non-secret**, ignored `worker/deployment.local.json` binding after independent review. Missing or mismatching binding fails closed. Tokens, passwords, private keys and raw secret prompts belong outside Git, including private State Git.

Optional Worker alerts use the provider-neutral `BRIDGE_SMTP_HOST`, `BRIDGE_SMTP_PORT`, `BRIDGE_SMTP_USER`, `BRIDGE_SMTP_PASSWORD`, `BRIDGE_SMTP_TO` and `BRIDGE_SMTP_FROM_NAME` variables. An env file is used only when an explicit `alerts.env_path` or `BRIDGE_SMTP_ENV_FILE` is supplied; missing configuration fails closed.

## Entry points and documentation

- `worker/bridge_worker.py`, `worker/bridge_worker_hardened.py`, `worker/windows_worker_launcher.py`: explicitly bound production entry points; do not start them as a read-only demo.
- `worker/bridge_dashboard.py`, `worker/dashboard_service.py`, `worker/bridge_manual.py`: console and explicit operator tools.
- `protocol/v2/`, `PROTOCOL.md`: the original protocol implementation and schemas.
- `SUPERVISOR_ENTRYPOINT.md`, `policies/`: generic planning and routing policy.
- `docs/ENGINE_STATE_BOUNDARY.md`, `docs/ARCHITECTURE.md`: locations, durability and deployment.
- `docs/SELF_MAINTENANCE_BOOTSTRAP.md`: why unattended split self-maintenance is currently disabled.

Copy generic project templates to a **new State repository**, never to Engine `projects/`. `examples/remote-projects.example.json` is a schema-shaped example, not a live registry. Read `docs/OWNER_CONSOLE.md` before enabling controls. This candidate is not a declaration that any existing production deployment has been replaced.

## License

Apache License 2.0; see `LICENSE` and `docs/LICENSING.md`. This source license
does not authorize disclosure of private State, credentials or review artifacts.
