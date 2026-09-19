# Bridge security policy

This file centralizes security policy that applies across Bridge projects. It does not replace project-specific restrictions in `MISSION.md` or local runtime configuration.

## Secret boundary

Never commit or copy into Bridge state/history:

- access tokens, PAT values, OAuth tokens/codes, passwords, cookies, private keys;
- SMTP authorization codes or credentials;
- Authorization headers or signed private URLs;
- raw credential-file contents.

Commands and reports should describe where a secret is expected only at the minimum level required to perform the task. Prefer local ignored configuration or environment variables for runtime credential locations.

## Execution boundary

The unattended Worker may run Codex with full local access only inside a validated registered project workdir. Remote project mapping and local allowed-root checks are defense in depth; they do not turn full-access execution into a strong sandbox.

Do not weaken repository/workdir validation merely to make a command run.

## Network guard

When enabled, network guard is fail-closed for unattended Codex execution. Unsafe egress before claim leaves the command unclaimed. Unsafe egress during a run triggers targeted termination, durable local recovery evidence, and exact-identity reconciliation; it never authorizes an automatic rerun.

Network Guard is a **route-safety** control. It answers whether the host's current network route is acceptable for unattended execution. It does not by itself constrain which destinations the Codex process tree may contact.

## Executor egress isolation

Executor egress enforcement is a separate, optional boundary. The requirements below are normative; enabling it requires acceptance evidence for the exact platform, helper and policy. A disabled or unavailable mechanism is not an isolation guarantee.

The egress boundary SHALL:

- be owned by the exact unattended RunScope/executor lifetime;
- preserve required provider-control connectivity while constraining project-facing egress according to explicit policy;
- deny undeclared project egress by default once the accepted enforcement mechanism is enabled for production;
- resist representative direct-socket/descendant-process bypass rather than relying only on proxy environment variables;
- emit only bounded, secret-safe destination-class evidence;
- fail closed before claim when required enforcement cannot be established;
- never treat a missing egress log as proof that no egress occurred;
- never authorize automatic replay of an interrupted command.

Network Guard and executor egress isolation are complementary. Neither may be disabled merely because the other is healthy.

A project may explicitly require broader network compatibility, but broader egress reduces recovery certainty. If an interrupted run could have produced an unreconciled external side effect, Bridge remains conservative and uses the existing recovery boundary rather than guessing.

## Evidence minimization

Persist only the minimum non-secret evidence required to diagnose or verify behavior. Raw Codex logs remain local and ignored; reports may contain only bounded, best-effort-redacted diagnostics.

Security or destructive ambiguity is a valid reason to reduce autonomous task scope or require owner action.