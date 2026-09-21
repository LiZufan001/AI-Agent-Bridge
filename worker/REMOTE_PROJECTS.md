# Remote project registry

Engine publishes `../examples/remote-projects.example.json` and the generic loader. Each State instance owns `worker/remote-projects.json`: concrete product repository, workdir, execution-enabled flag and per-project runtime policy. Registry execution `enabled` is separate from Supervisor Owner selection in `supervisor/portfolio.json`.

Preserve product repository identities and verify each workdir independently. State and the running Engine are not executor workdirs.

Bridge self-maintenance uses an independent Candidate-local maintenance checkout. Candidate execution is confined to its workspace and normal TEMP, while the outer authority owns Git integration, production restart and controlled adoption. Unattended adoption is disabled.
