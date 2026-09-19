# engine-maintenance project template

This directory is a policy/context template for Bridge self-maintenance. It is not a live project and must not be copied into `projects/engine-maintenance` with invented Protocol state, commands, reports, owner evidence, or run evidence.

Use it only to bootstrap project-specific context around the current Bridge architecture:

1. create or verify the intended maintenance checkout from current repository evidence;
2. resolve its real workdir, branch, origin, and source identity through the current registry/configuration and Worker preflight;
3. instantiate `MISSION.md`, `SUPERVISOR.md`, and an optional local `WORKDIR.md` note without introducing a second mutable authority;
4. create canonical project state only through the normal Protocol-v2 path; and
5. derive the first real command from the current Goal, current source/runtime facts, Owner control, and the normal staged publication gateway.

There is no standing `feature/synthetic-candidate` branch, bootstrap SHA, stable controller, candidate controller, or fixed phase sequence in this template. Older development documents and historical commands/reports remain evidence of how the system was developed, not current setup instructions.

If an isolated checkout is required for a future self-maintenance operation, establish that isolation from current policy and verify it at execution time. Never treat a prompt-supplied path, stale branch name, historical base SHA, fixture result, or CI-only result as proof of live adoption.
