# Bridge alerting policy

Alerting is a side effect around Protocol v2. It never changes generation, CAS, lease, recovery identity, execution outcome, or rerun policy.

## Supervisor liveness alerts

The Owner notification route is explicit deployment configuration supplied through the existing external mechanism; no personal recipient is embedded in the Engine.

Prefer a sender independent of the Windows Worker (for example the connected ChatGPT-side Gmail channel) so a local machine/network failure can still be reported.

Thresholds:

- same canonical `COMMAND_READY` remains unclaimed for one complete Scheduled Supervisor interval (about 1 hour): one alert;
- same `CODEX_RUNNING` run remains active for at least 2 hours from `active_run.claimed_at`: one alert;
- new `RECOVERY_REQUIRED`: one alert;
- new `FAILED`: one alert;
- `HUMAN_REQUIRED`: use owner-action notification semantics rather than a duplicate liveness alert.

Alerts are notification-only. Never kill Codex, mutate `state.json`, rerun a command, publish speculative recovery work, or state an unproven root cause merely to respond to an alert threshold.

Deduplicate the same abnormal episode. A short recovery notice is allowed after clear recovery but is not required.

## Worker anomaly alerts

The local Worker may send one detailed secret-safe alert when it has run-specific evidence for a network-guard interruption, recovery transition, expired lease, deferred-recovery conflict, or unsafe recovery exception.

Ordinary short Git/probe failures, controlled exit-75 hot reload, and successful execution are not alert incidents.

Worker alert failure must not block recovery or alter canonical state. Persistent dedupe records remain local/ignored.

## Content safety

Alert bodies may include project id, command id, state, generation, run id, timestamps, elapsed duration, and non-secret diagnostic classifications.

Never include credentials, tokens, cookies, Authorization headers, SMTP auth codes, raw private prompts, or unbounded raw logs.