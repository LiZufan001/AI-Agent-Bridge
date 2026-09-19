# Owner console

The console is an observation and existing-control client, not a new authority. It binds only `127.0.0.1`. Worker, Coordinator, Launcher, active-run and project views come from the resolved State root; HTML/assets come from Engine. Missing or stale health files remain unavailable/stale. A successful HTTP response is not evidence that Worker is running.

From Engine, `python -B worker/dashboard_service.py open` discovers a marked sibling for read-only observation, otherwise uses synthetic data. Production must use an explicit absolute `--state-root` or `AI_AGENT_BRIDGE_STATE_ROOT`. `--allow-controls` is an explicit opt-in; it requires a writable independent State and local non-secret binding. Merely opening the console does not pause/resume, issue a command or stop a run.

Use a different loopback port for side-by-side acceptance. `python -B tools/sidecar_smoke.py --state-root <absolute-state-root>` uses an ephemeral port, rejects a control POST, checks the served root and hashes all State files before/after. It never starts Worker or Codex. Do not use a real product command as a smoke test.

Pause/resume continues to use the configured durable Owner execution-control file and its original validation. Pause blocks new work but does not kill an active run. Stop follows the original exact-run control mechanism and cannot be inferred from a stale PID. The Dashboard redacts raw prompts/secrets according to the original rendering boundary. Local runtime/log files remain private and ignored.
