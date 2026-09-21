# Owner escalation and notification policy

This file defines Supervisor semantic policy for genuine owner-only blockers and owner notification. Canonical `HUMAN_REQUIRED`, owner-action, generation/CAS and resume semantics remain defined by `PROTOCOL.md` / `protocol/v2/`.

## Escalation threshold

Do not ask the Owner merely to acknowledge normal progress, green CI, routine completion, an autonomously repairable bug, or a prerequisite that current evidence already resolves.

Escalate only when current evidence shows a genuine unresolved owner-only action/decision/input, or safe continuation cannot be derived without Owner authority. Typical examples include physical-device subjective acceptance, credentials supplied only through approved local channels, destructive-operation approval, ambiguous repository/workdir identity, security/elevation ambiguity, unreconciled external side effects, or canonical control-plane integrity that automation cannot safely repair.

After BLOCKED/FAILED/RECOVERY_REQUIRED/interruption/partial completion, first classify the direct current blocker and prefer the smallest safe corrective action when mechanically derivable.

## Current blocker identity

Every owner escalation must bind to the exact current owner-action/blocker identity and current evidence. Use the newest linked Owner event and current evidence to determine whether the dependency remains active, is resolved, or is superseded.

When creating a new root owner-action event, set `root_action_id` to its own `owner-action-NNN` id and `relates_to: null`. Only later progress/resolution events in that owner-action thread may use `relates_to`, and it must name an earlier `owner-action-NNN`; owner feedback/report/goal provenance belongs in its dedicated fields rather than `relates_to`.

For an existing `HUMAN_REQUIRED` state, always read the **newest linked owner-action event** before describing the project as waiting for the Owner. An immutable root event that still says `AWAITING_OWNER` is not current progress when a newer event in the same thread says `OWNER_IN_PROGRESS` or `OWNER_REPORTED_DONE`.

`OWNER_REPORTED_DONE` means the Owner action itself is no longer outstanding. If `verification_status=PENDING`, the remaining dependency is verification/resume, not another Owner request. If `verification_status=VERIFIED`, any remaining `HUMAN_REQUIRED` container is a Protocol resume/publication boundary. Do not ask the Owner to repeat the action in either case unless fresh verification specifically proves the earlier action failed or a materially new Owner action is required.

## Notification-before-HUMAN_REQUIRED safety rule

Until a future deterministic `PAUSE_FOR_OWNER` gateway is explicitly implemented and accepted, the following safety rule remains mandatory:

1. identify the exact current owner-action id and blocker key from durable evidence;
2. inspect durable notification receipt evidence for that same exact blocker;
3. if authoritative successful notification evidence is missing, do **not** enter a new `HUMAN_REQUIRED` transition yet;
4. when Worker execution is available, use the bounded notification-only execution path. A new notification-only command must contain exactly one schema-v2 Worker declaration near the command header and exactly one explicit plain-text body block:

   ```text
   <!-- bridge-owner-notification: {"schema_version":2,"owner_action_id":"owner-action-NNN","blocker_key":"stable-blocker-key","transport":"worker-email","subject":"concise optional subject"} -->
   <!-- bridge-owner-notification-body:start -->
   Exact secret-safe text the Owner should receive.
   <!-- bridge-owner-notification-body:end -->
   ```

   The command body is durable input to the Worker. It must not contain credentials, recipient addresses, cookies, tokens or other secrets. Codex must **not** call SMTP or any other mail sender. Its notification-only job is to re-check the exact blocker semantics and return `SUCCESS` only if this exact notification is still current and should be sent;
5. only after that Codex semantic check returns `SUCCESS`, the Worker owns the side effect. Before attempting SMTP it atomically writes a local `pending` journal record under `worker/runtime/owner-notifications/` for the stable project + owner-action + blocker notification identity. A pre-existing `pending` or `failed` record suppresses automatic resend and requires reconciliation; a pre-existing `sent` record is a dedupe hit;
6. the Worker uses the provider-neutral Worker SMTP boundary to make the one bounded send. Provider acceptance produces a Worker-generated `SENT` receipt. The Worker persists that receipt in the local journal and after the Codex final response in the canonical Report under `## Worker owner-notification receipt` as one `BRIDGE_OWNER_NOTIFICATION_RECEIPT: {...}` line. If SMTP succeeded but post-send journal persistence fails, the pre-send `pending` record still prevents automatic resend and the Report receipt may carry the durable provider-accepted evidence;
7. only that Worker-generated receipt, or an explicit append-only reconciliation receipt described below, is authoritative. Codex/LLM prose, a child-process exit code, `Completion email sent...`, app-server `smtp_success` diagnostics, or a similar marker inside the Codex response is not by itself authoritative for the Bridge control plane;
8. on a later Supervisor planning boundary, re-read current evidence and verify that the same blocker is still unresolved and owner-only before entering `HUMAN_REQUIRED`;
9. do not notify the same unresolved exact blocker again after authoritative `SENT` evidence exists;
10. if SMTP or another external notification side effect may already have happened but a native receipt is missing, **do not mechanically resend**. Reconcile the existing side effect first. Only when evidence proves no send occurred may a bounded retry be considered;
11. do not send catch-up mail for stale, resolved, superseded, deferred, owner-reported-done, or owner-paused prerequisites;
12. if a current `HUMAN_REQUIRED` blocker lacks required notification evidence, treat it as a recoverable protocol-compliance defect and perform at most one bounded remediation notification when execution is available **only if the blocker is still current, the Owner has not already reported it done, and there is no unresolved prior send side effect**.

When Worker is unavailable, still complete blocker semantics/reconciliation and report exactly what notification, verification, resume, or pause transition is deferred. Do not reduce the project to “review later”.

## Receipt semantics

`SENT` intentionally means only what the current transport can prove: the Worker-owned SMTP/provider call returned accepted/success and the Worker durably bound that acceptance to the exact project, owner-action, blocker, command and run. It does **not** mean the Owner opened, read or acknowledged the message. Owner observation belongs to a separate `ACKNOWLEDGED`/owner-feedback boundary.

A native Worker receipt uses `evidence_type=provider_accepted`, `provider_result=accepted`, the exact `source_command_id` and `source_run_id`, and the stable logical notification identity derived from project + owner-action + blocker. Receipt data must remain non-secret and minimal: do not persist recipient addresses, message bodies, cookies, tokens or SMTP credentials. The pre-send journal may store only non-secret identity plus a body hash, never the body itself.

When a native Worker receipt is unavailable, notification delivery may be reconciled only from concrete delivery evidence plus an explicit Owner observation or equivalent authoritative evidence. Store such evidence append-only under `projects/<project-id>/notification-receipts/`. It must use `evidence_type=reconciled_owner_observation` (or another explicitly documented reconciliation type), reference the bound command/report, state why native evidence is unavailable, and clearly identify the evidence type. A reconciliation receipt suppresses duplicate resend for the same exact blocker and leaves the bound Report immutable.

The Supervisor must not normally inspect sender or app-server diagnostics directly. Raw sender diagnostics and direct-sender stdout are troubleshooting/reconciliation input only, not the normal control-plane receipt API.

## Notification / dependency status in output

When reporting an owner-related boundary, use the status that matches the newest exact evidence:

- `SENT` — current unresolved Owner blocker has an authoritative native or reconciled notification receipt;
- `PENDING` — notification for a genuinely current Owner blocker is pending and no prior side effect is unresolved;
- `RECONCILIATION_REQUIRED` — a send side effect may already have happened but receipt identity is unresolved; do not resend;
- `MISSING/REMEDIATION_REQUIRED` — current blocker still requires notification and authoritative send evidence is missing;
- `NOT_REQUIRED` — current durable Protocol/policy does not require notification;
- `OWNER_IN_PROGRESS` — Owner has already started the action; do not repeat the ask;
- `OWNER_REPORTED_DONE / VERIFICATION_PENDING` — Owner reports completion; verification is the remaining dependency;
- `OWNER_REPORTED_DONE / RESUME_PENDING` — action is verified/resolved but canonical resume/publication is waiting for a legal execution boundary.

Do not label the last two cases `blocked-owner` or say the Owner still needs to perform the resolved action.

## Email discipline

Owner email is for genuine current unresolved owner action under the implemented notification mechanism. Do not email for routine progress, normal completion, green CI, self-repairable blockers, stale/superseded dependencies, owner-reported-done verification/resume, or paused/deferred Bridge development.

Never expose secrets/raw credentials in notification, logs, reports, receipts or staged requests.

## Deferred deterministic hardening

A future narrow `PAUSE_FOR_OWNER` deterministic gateway is deferred. Do not implement it as part of the current control-plane refactor and do not remove this policy/prompt protection until that gateway, if adopted, has passed focused and real Scheduled acceptance.

The complementary future hardening should also cover the **resume side**: exact owner-action thread identity, latest-event ordering, verified/current resolution evidence, root-event supersession, idempotent resume, and fresh canonical CAS. A future gateway must always classify the dependency from the newest linked event before creating an Owner ask.

Any future gateway may mechanically verify generation/report identity, owner-action/blocker identity, canonical notification report and `SENT` evidence, duplicate/stale handling and fresh CAS. It must not decide whether the blocker is genuinely owner-only or whether the product should pause; those remain Supervisor semantic judgments. It must reuse the existing Protocol transition rather than adding a new canonical state unless a separately reviewed Protocol change explicitly proves that necessary.
