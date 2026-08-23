# Release gates

## Gate P0 — honest and installable foundation

Before calling the current package a reliable release:

- source and embedded skill archives validate identically (the current local check passes; CI must enforce it on every build);
- CI validates the installed wheel, not only the source tree;
- lane readiness reports real capability state and preserves blocked/unknown distinctions;
- `lcd`/`LCD_*` compatibility is either preserved for a deprecation window or intentionally documented as a breaking change;
- verification-process limitations are explicit and trusted-command policy is enforced;
- existing tests, compile checks, and source validators pass.

## Gate P1 — single-host durable job plane

- A coordinator can persist task state and recover after restart.
- A worker can register an Agent Card and pass a real smoke test.
- Submit/watch/inspect/cancel are available from the CLI.
- Idempotency and lease behavior have integration tests.
- A receipt can be reconstructed from durable event history.

## Gate P2 — LAN A2A MVP

- Two PCs complete the MVP acceptance scenario in [`requirements.md`](requirements.md).
- Transport is authenticated and secrets are not embedded in task payloads.
- Reconnect/resume is tested under client disconnect and worker interruption.
- Artifacts are transferred with hashes and bounded retention.
- At least one adapter is release-ready and a second adapter passes conformance tests.

## Gate P3 — operational product

- Cancellation, retry, and lease expiry semantics are observable and tested.
- Metrics and audit views expose completion, recovery, scope, false-readiness, and receipt completeness.
- Install/upgrade/rollback documentation works on a clean Windows machine.
- A representative multi-machine evaluation cohort exists.

## Gate P4 — scale and teams

Only after P2/P3 are stable:

- multi-user identity and authorization;
- quotas, scheduling, and policy approvals;
- optional hosted coordinator;
- retention, tenancy, and enterprise audit controls;
- broader adapter ecosystem and marketplace/discovery features.

## Stop-ship conditions

- A task can report success without verification or a durable receipt.
- Duplicate submission can cause duplicate side effects without an explicit opt-in.
- A worker can access files outside its declared policy without the result being rejected.
- A disconnected operator cannot determine whether work is still running.
- A lane is advertised as ready based only on executable presence.
- Secrets appear in logs, artifacts, task payloads, or receipts.
