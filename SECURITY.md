# Security reporting and supported boundary

This project is source software, not a hosted service. A passing CI run is not
permission to publish private deployment data or to perform a production cutover.
No warranty is supplied; see LICENSE.

Do not disclose live tokens, passwords, private State, personal paths, raw logs,
or working exploit details in public issues. Use the repository's private
vulnerability reporting feature if it is enabled. If no private channel is
available, ask the maintainer to establish one without posting sensitive details.

The console binds to loopback and is intended for a trusted local operator. Its
random service identifier is not authentication against other processes running
as the same operating-system user. Reverse-proxy exposure is outside this
boundary and needs a separate access-control and deployment review.

The example configuration does not enable execution, alerts or outbound probes.
Unattended execution and optional native enforcement require an explicitly
configured private instance and independently accepted deployment controls.
Do not grant untrusted contributions production runner access or secrets.

Reports should identify the affected source revision, platform, minimal synthetic
reproduction, impact and any conditions required. Maintainers must establish a
supported release list before advertising production support.
