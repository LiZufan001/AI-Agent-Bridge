# engine-maintenance project template

This directory is a policy/context template for Bridge self-maintenance. It is not a live project and does not create Protocol State, commands, Reports, Owner evidence or run evidence.

Use it to bootstrap project-specific context around the production Bridge architecture:

1. create or verify the intended independent maintenance checkout from current repository evidence;
2. resolve its workdir, branch, origin and source identity through current registry/configuration and Worker preflight;
3. instantiate `MISSION.md`, `SUPERVISOR.md` and an optional local `WORKDIR.md` note without introducing a second mutable authority;
4. create canonical project State only through the normal Protocol-v2 path;
5. derive executable work from the current Goal, current source/runtime facts, Owner control and staged publication gateway.

Every self-maintenance operation establishes its Candidate identity from current evidence and verifies it at execution time. Live adoption claims require the production controlled-adoption and health boundary.
