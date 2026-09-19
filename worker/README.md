# Worker and local operator entry points

Read `../docs/ENGINE_STATE_BOUNDARY.md` first. Run code from Engine and pass an explicit absolute State root. Python import roots are Engine; canonical persistence, registry, publication inbox, runtime journals and local config are State. Never set a product WORKDIR to State or the running Engine.

`bridge_worker.py`, `bridge_worker_hardened.py` and `windows_worker_launcher.py` require a marked independent State Git root and verified local deployment policy. Relative runtime/config paths are resolved inside State. They do not pull Engine code when syncing State. A missing private control binding blocks execution.

`bridge_manual.py` has a global `--state-root` option before its subcommand. Its temporary State Git clone inherits only the reviewed non-secret control binding. Existing CAS and publication preflight remain mandatory. `bridge_doctor.py` is diagnostic; recovery and repair entry points retain their existing explicit write guards and now resolve State separately.

Synthetic regression: from Engine run `python -B tools/check_test_isolation.py --output ../worker-tests.json`. Tests use local temporary Git repositories and dummy child processes, not real product commands. The runtime retains existing bounded child process, report, lease, recovery and operator-safety gates. Windows-only integration must be verified on Windows before production use.
