# Product requirements

## MVP definition

The MVP is complete when two independently running PCs can execute the first user journey in [Problem and jobs](problem-and-jobs.md) over an authenticated LAN connection, with durable task state, reconnect/resume, bounded workspace policy, artifacts, verification, and an inspectable receipt.

The MVP does not require every current lane to work remotely. One production-quality worker adapter plus a second adapter in conformance testing is enough to validate the control plane.

## Functional requirements

### R1. Worker identity and discovery

- A worker can publish an Agent Card containing identity, protocol version, capabilities, supported task kinds, workspace constraints, artifact limits, and current readiness.
- A coordinator can list, filter, and select workers.
- A readiness result distinguishes `ready`, `degraded`, `blocked`, and `unknown`.
- A worker is not considered ready solely because an executable exists.

### R2. Canonical task submission

- A submitter sends the existing policy-rich task contract with a stable task ID or idempotency key.
- The coordinator returns an acknowledgement containing task ID, accepted timestamp, protocol version, and current state.
- Repeating the same idempotent submission does not create duplicate execution.

### R3. Durable lifecycle

- Task state survives coordinator restart and client disconnect.
- The lifecycle includes at least `submitted`, `accepted`, `running`, `waiting`, `succeeded`, `failed`, `blocked`, `cancel_requested`, `cancelled`, and `expired`.
- Every transition has a timestamp, actor, reason, and correlation ID.
- Unknown or contradictory adapter states fail closed into an inspectable error, not a false success.

### R4. Leases and execution ownership

- A worker must hold a renewable lease before executing a task.
- Lease expiry makes ownership explicit and prevents silent duplicate execution.
- A retry policy distinguishes safe-to-retry work from side-effecting work.
- The coordinator can show which worker currently owns a task.

### R5. Progress and recovery

- The submitter can watch progress through polling and a streaming update path.
- A disconnected submitter can reconnect using task ID and receive the current state plus missed events or a bounded snapshot.
- A worker can resume an owned task after a transient process/transport failure when the adapter supports it.
- Cancellation reports request-sent and execution-stopped as separate facts.

### R6. Workspace and security policy

- Each task declares permitted workspace/repository identity, allowed paths, and whether edits or side effects are allowed.
- The worker records a workspace fingerprint before and after execution.
- Verification commands are trusted configuration, not arbitrary untrusted input, until a stronger isolation boundary exists.
- LAN transport is authenticated; credentials are never copied into task payloads or receipts.

### R7. Artifacts and verification

- A successful task returns a bounded artifact manifest with content hashes, sizes, media/type metadata, and provenance.
- Patch artifacts are separated from logs and human-readable summaries.
- Parent-owned verification runs and records command, exit status, duration, and bounded output.
- A receipt links task, worker, workspace fingerprint, artifacts, verification, and final status.

### R8. Adapter conformance

- Every adapter implements the same submit/observe/cancel/result lifecycle.
- Adapter health probes cover actual invocation, authentication/model access, output parsing, and cancellation where supported.
- A lane can be marked conditionally available without being presented as release-ready.

### R9. Configurable intelligence escalation

- The coordinator or local orchestrator can evaluate a task at explicit
  `plan_end`, `execute`, `review_end`, `recovery`, and `release` stages; `plan`
  and `review` are compatibility aliases.
- The default policy summons the configured high planner (gpt-5.6-sol in the
  example policy) after the ordinary planning pass and the configured high
  verifier after the ordinary review pass.
- If either high gate returns actionable rejection, the selected bulk worker
  receives the bounded feedback, revises, reruns deterministic checks, and is
  rechecked. The maximum revision count and exhausted outcome are configurable;
  infinite retries and silent acceptance are prohibited.
- The escalation policy is versioned, configurable, ordered by priority, and
  fail-closed when malformed.
- A decision distinguishes `continue`, `consult`, `require_review`, and
  `block`, and records the matched rule, observed signals, selected profile,
  and evidence requirements.
- High-intelligence model names, lanes, and reasoning effort are configuration
  values; the policy must not assume a particular provider entitlement.
- A consultation failure cannot be silently downgraded to the bulk worker or
  treated as a satisfied review.
- A high-model consultation is advisory or a review gate according to the
  decision, but deterministic verification, scope, workspace, and authority
  controls remain mandatory.

## Non-functional requirements

| Area | MVP target |
| --- | --- |
| Recovery | Client reconnect returns a correct task snapshot after coordinator restart or network interruption. |
| Idempotency | Duplicate submit requests do not create duplicate task ownership. |
| Audit | Every terminal task has a durable receipt and transition history. |
| Security | Authenticated LAN transport; no secrets in task/artifact payloads. |
| Boundedness | Logs, artifacts, and event history have configurable size/retention limits. |
| Compatibility | Protocol version and adapter capability versions are explicit. |
| Developer UX | A user can complete submit/watch/inspect/resume from CLI without hand-editing state. |

## MVP acceptance scenario

Given a clean repository checkout on PC B and a ready worker:

1. Start a relay worker on PC B.
2. Discover it from PC A.
3. Submit a bounded file change with allowed paths and verification.
4. Confirm the task is accepted and leased exactly once.
5. Disconnect the client during execution.
6. Reconnect and observe the same task without resubmitting it.
7. Receive an artifact manifest and verification result.
8. Confirm the receipt is inspectable after restarting the coordinator.
9. Force a scope violation and confirm no out-of-scope change is accepted.
10. Force a worker interruption and confirm the task becomes recoverable or explicitly blocked rather than falsely succeeded.
