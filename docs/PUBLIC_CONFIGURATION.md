# Public distribution configuration and privacy boundaries

The example is intentionally unconfigured: no projects, notifications or network
probe is active and the unattended executor rejects `codex_execution_mode=disabled`.
This is not a new sandbox mode. To enable execution, an owner must configure a
private instance, explicit allowed roots and the existing full-access mode/flag
inside an independently accepted isolation boundary. This distribution does not
change already deployed private configuration or silently lower its privileges.

## Credentials and outbound heartbeat

Only an explicit API token or `BRIDGE_GITHUB_TOKEN` is used by default. General
`GH_TOKEN` / `GITHUB_TOKEN` variables are not borrowed. An explicitly set invalid
Bridge token fails closed. Optional Git Credential Manager lookup requires
`BRIDGE_ALLOW_GIT_CREDENTIAL_MANAGER=1` in the local process environment; it is not
a license to broaden the token permissions. Keep all token values outside Git.

An outbound heartbeat must bind the real local host with the private environment
variable `BRIDGE_LOCAL_WORKER_HOST`. Its existing bootstrap `worker_id` is instead
a distinct pseudonymous external alias. A missing/mismatched binding or alias equal
to the hostname disables external heartbeat publication without corrupting Worker
state. Update the reader's expected alias together with the private deployment
binding. Heartbeat timing and the chosen alias remain visible to its destination;
this minimizes disclosure, not anonymity against arbitrary outside information.

## Local console identity

The advertised console `root_id` is a random rendezvous identifier, persisted in
a user-local temporary directory. A path digest is used only as a local lookup
filename; it is never the identifier returned by the HTTP API. Copying State does
not export this local registry. Each running console instance must use the current random rendezvous identity contract.
A registry entry is not an authorization token. Host/Origin checks protect browser
requests, not mutually hostile programs sharing one OS account. Console health,
logs and offline rendered views remain private operational evidence.

## Stable instance identifiers and operator boundaries

Persisted helper protocol names, task names and profile prefixes are operational identifiers. Change them only through an exact-identity operator plan that preserves installed helper, policy and State bindings.

Operational evidence, concrete deployment mappings and credentials belong to the private instance. Public source changes do not by themselves authorize host-service, task, credential or State mutations.
