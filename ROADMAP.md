# Agent Relay roadmap

## Outcome

Agent Relay will make a bounded engineering job travel safely from one machine and agent to another, survive interruption, and return proof that a human can inspect and trust.

## Product thesis

Agent Relay is not primarily a model router. It is the A2A job control plane around heterogeneous agents: discovery, delivery, policy, leases, recovery, artifacts, verification, and receipts. A2A is the interoperability boundary between agents; MCP remains the tool/data boundary inside an agent. See [`docs/pm/a2a-direction.md`](docs/pm/a2a-direction.md).

## Current state

The repository is a measured local bounded-delegation MVP with an implemented local/separate-process durable coordinator and worker lifecycle. It already contains task contracts, deterministic triage, Ollama/Codex execution, Git sandboxing, patch and scope checks, verification, retry, receipts, batch/evaluation tooling, and conditional Claude/Codex-review/Antigravity adapters. Physical two-PC LAN execution and interruption recovery remain external acceptance gates.

Current evidence and limitations are recorded in [`docs/pm/current-state-audit.md`](docs/pm/current-state-audit.md), [`README.md`](README.md), [`GOAL.md`](GOAL.md), and [`EVALS.md`](EVALS.md).

## Priority model

- **Now:** release truthfulness and the smallest durable job-plane foundation.
- **Next:** authenticated LAN execution with discovery, leases, persistence, resume, artifacts, and proof.
- **Later:** polished operations, broader adapters, team controls, and hosted scale.

No item should be marked complete because code exists. It is complete only when its acceptance evidence is recorded and the relevant regression tests pass.

## Now — make the foundation truthful and define the product

### AR-001 — Freeze the canonical job boundary

**Status:** implemented locally · **Owner:** core · **Depends on:** none

Deliverables:

- Define versioned schemas for Agent Card, Task, Message/Update, Artifact, Receipt, and lifecycle events.
- Map the existing `DelegationTask` fields into the external job envelope without losing allowed files, verification, risk flags, retry, or workspace policy.
- Define canonical states, terminal-state rules, idempotency semantics, and cancellation vocabulary.
- Add schema fixtures and compatibility tests.

Acceptance evidence:

- A task can be serialized/deserialized without loss of policy-critical fields.
- Invalid transitions and missing terminal evidence are rejected.
- Schema/version fixtures are checked in and referenced by adapter tests.

Current evidence: [`src/agent_relay/protocol.py`](src/agent_relay/protocol.py),
[`tests/test_protocol.py`](tests/test_protocol.py), and the durable coordinator
tests. This is a local protocol boundary, not yet an A2A standards conformance
claim.

### AR-002 — Remove current release blockers

**Status:** partial · **Owner:** release · **Depends on:** none

Deliverables:

- Keep source and embedded skill archives synchronized and validate them on every build (the current local comparison passes).
- Add clean-wheel install and embedded-archive CI coverage.
- Restore or explicitly deprecate historical `lcd`/`LCD_*` compatibility with a migration window.
- Replace shallow lane readiness with capability smoke states: `ready`, `degraded`, `blocked`, `unknown`.
- Document and constrain trusted verification commands; define process-tree cleanup/isolation work.

Acceptance evidence:

- Package validator passes for both archives.
- CI installs the built wheel in a clean environment and validates the installed skill.
- `doctor --all` cannot report a lane ready when invocation/authentication/model access fails.
- Release docs accurately state the OS-isolation boundary.

### AR-003 — Define the first end-to-end LAN slice

**Status:** implemented locally · **Owner:** product/core · **Depends on:** AR-001, AR-002

Deliverables:

- Write the two-PC acceptance harness from [`docs/pm/requirements.md`](docs/pm/requirements.md).
- Select one reference worker adapter and one conformance-only adapter.
- Define the minimum CLI: `serve`, `agents`, `submit`, `watch`, `inspect`, `cancel`, `resume`.
- Record exact environment, trust model, workspace assumptions, and failure injection points.

Acceptance evidence:

- A clean operator can run the scenario without undocumented manual state copying.
- Every failure injection produces a truthful, inspectable state.

Current evidence: loopback coordinator, real HTTP coordinator/worker integration,
reference worker, idempotent submit, lease, artifact, cancel, resume, restart,
SSE replay, Claude capability probe, bounded Claude edit smoke, and Claude
cancellation smoke. Physical two-PC acceptance remains AR-013.

## Next — build the durable A2A job plane

### AR-004 — Agent registration and capability discovery

**Status:** partial · **Owner:** control-plane · **Depends on:** AR-001

Deliverables:

- Worker registration and Agent Card publication.
- Capability matching for task kind, model/provider, OS, tools, workspace, artifact limits, and trust.
- Heartbeats and real readiness probes.
- Registry inspection from CLI and machine-readable output.

Acceptance evidence:

- Two workers with different capabilities are correctly filtered and selected.
- A worker whose external dependency is unavailable is shown as blocked/degraded, never ready.
- Registry state survives coordinator restart.

Current evidence: `/agents` supports task-kind, capability, and readiness
filters; heartbeat updates persist through SQLite restart; scoped worker lease
requests are checked against enrolled backend/capability/task-kind policy; the
`POST /tasks/claim` path selects the highest-priority compatible task, then the
earliest deadline and oldest creation time, and skips live
leases/incompatible policies; the reference worker reports `unknown` at
registration, `ready` after a successful bounded result, and `degraded` after
an adapter failure. Overdue queued work receives a durable coordinator expiry
receipt; active execution is not interrupted. Physical two-PC selection remains
outstanding.

### AR-005 — Authenticated transport and identity

**Status:** planned · **Owner:** security/control-plane · **Depends on:** AR-001, AR-004

Deliverables:

- Per-machine identity and enrollment/revocation.
- Authenticated LAN transport with explicit protocol version.
- Authorization policy for submit, observe, cancel, artifact read, and worker administration.
- Secret redaction and payload/log hygiene.

Acceptance evidence:

- Unenrolled workers cannot accept jobs.
- Revoked credentials cannot reconnect or fetch artifacts.
- Secret-seeding tests prove credentials do not appear in payloads, logs, or receipts.

Current evidence: the coordinator supports scoped worker credentials supplied
through headers, actor binding for worker mutations, credential rotation on
registration, admin revocation, and optional HTTPS with private-CA client
verification. Certificate/identity provisioning and a richer role/permission
model remain deployment work.

### AR-006 — Durable task store, idempotency, and leases

**Status:** partial · **Owner:** control-plane · **Depends on:** AR-001, AR-004

Deliverables:

- Coordinator-owned durable task/event store.
- Idempotent submit keyed by task ID/idempotency key.
- Renewable worker leases, expiry, ownership transitions, and heartbeats.
- Retry classification for safe versus side-effecting work.

Acceptance evidence:

- Coordinator restart preserves task state and event history.
- Duplicate submit creates one logical task and one lease.
- Stale lease cannot silently produce a second accepted side effect.
- Every ownership transition is attributable to a worker and timestamp.

Current evidence: SQLite restart/idempotency/ownership tests pass; the worker
renews leases during adapter execution and polls expired `running` tasks so the
store can make stale ownership explicit before reassignment. Side-effect-aware
retry policy and physical multi-worker acceptance remain outstanding.

### AR-007 — Progress, cancellation, reconnect, and resume

**Status:** partial · **Owner:** control-plane/adapters · **Depends on:** AR-006

Deliverables:

- Polling plus streaming task updates.
- Bounded progress events and missed-event replay/snapshot.
- Explicit cancel-requested versus execution-stopped states.
- Adapter resume contract and worker interruption recovery.

Acceptance evidence:

- Client disconnect does not lose the job.
- Reconnect returns the correct state without duplicate submission.
- Worker interruption results in resume, safe retry, or explicit blocked state.
- Cancellation never claims stopped until the worker confirms the boundary.

Current evidence: polling plus bounded SSE replay are exposed through the CLI
and HTTP API; numeric event IDs support reconnect, and cancellation remains
`cancel_requested` until a worker supplies stopped evidence. Claude cancellation
now uses the bridge's asynchronous job cancellation API; non-stoppable
adapters produce an explicit `blocked` receipt with
`execution_stopped: false`. The real local Claude worker-plane cancellation
smoke passes with `execution_stopped: true`; retryable Claude bridge transport
failures now return to `waiting` with a released lease and bounded retry;
the separate-process Claude interruption smoke now passes with lease-expiry
reassignment and a fresh verified receipt; physical two-PC interruption and
adapter resume cohorts remain outstanding.

### AR-008 — Portable artifacts, verification, and receipts

**Status:** partial · **Owner:** execution/proof · **Depends on:** AR-001, AR-006

Deliverables:

- Content-addressed or hash-verified artifact manifest.
- Transfer and retention policy for patches, logs, reports, and generated files.
- Workspace fingerprints before/after execution.
- Parent-owned verification and durable receipt reconstruction.

Acceptance evidence:

- A receipt can be inspected after worker and coordinator restart.
- Artifact hash mismatch fails closed.
- Out-of-scope changes are rejected and included in the receipt.
- Verification output is bounded, attributed, and reproducible where possible.

### AR-009 — Adapter conformance and truthful lane support

**Status:** partial · **Owner:** adapters · **Depends on:** AR-004, AR-006, AR-007, AR-008

Deliverables:

- Common adapter lifecycle interface.
- Conformance tests for local-Qwen/Codex, Claude, Codex review, and Antigravity.
- Capability smoke tests for invocation, authentication/model access, parsing, timeout, and cancellation.
- Support matrix distinguishing implemented, conditional, measured, and release-ready.

Acceptance evidence:

- One adapter passes the full P2 scenario.
- A second adapter passes lifecycle conformance without being falsely advertised as production-ready.
- Adapter failures become structured task states and receipts.

Current evidence: Claude capability discovery, bounded edit, cancellation,
fallback labeling, and worker-plane integration pass. Native team execution and
physical adapter interruption cohorts remain conditional.

### AR-013 — Physical two-PC LAN acceptance

**Status:** blocked pending external environment · **Owner:** release/operations · **Depends on:** AR-003, AR-005, AR-007, AR-008, AR-009

Deliverables:

- Run the documented [physical two-PC acceptance](docs/pm/lan-acceptance.md) with PC A as coordinator and PC B as a Claude worker.
- Use real HTTPS certificates, CA verification, firewall restriction, worker enrollment, and scoped credentials.
- Submit a bounded task, disconnect the client, stop the worker during execution, allow lease expiry, restart the worker, and inspect the final receipt/artifact from PC A.
- Record OS versions, repository revision, adapter/backend, model, timestamps, task ID, event history, artifact hash, and receipt.

Acceptance evidence:

- PC A discovers PC B and receives a real Agent Card.
- A task is executed on PC B and observed/reconnected from PC A without duplicate submission.
- Worker interruption becomes reassignment, safe resume, or explicit blocked state—never false success.
- Revocation prevents further worker mutation, and artifact/receipt inspection remains available after coordinator restart.

Current blocker: this checkout has only one available host, so the physical network and machine-failure portion cannot be honestly claimed from loopback tests.

### AR-014 — Continuous orchestrator follow-up chaining

**Status:** implemented locally · **Owner:** orchestration/product · **Depends on:** AR-003, AR-006, AR-007, AR-008

Deliverables:

- Add a durable chain/goal contract for parent and child tasks.
- Let the orchestrator submit a follow-up only after an explicit predecessor state, such as `succeeded`.
- Pass only declared parent artifacts, receipt fields, or bounded messages into the child task.
- Persist chain step IDs, event cursors, idempotency keys, and failure policy across orchestrator restarts.
- Support bounded back-and-forth messages without forwarding full transcripts or uncontrolled context.

Acceptance evidence:

- A successful worker result can trigger exactly one idempotent follow-up.
- A failed, blocked, or cancelled predecessor cannot trigger a child unless policy explicitly allows it.
- Reconnecting or rerunning the orchestrator does not duplicate a chain step.
- Child tasks cannot access undeclared parent context or artifacts.

Current evidence: `POST /chains/{chain_id}/steps` and `GET /chains/{chain_id}`
persist linear step IDs, predecessor IDs, idempotency keys, policy, and child
envelopes across SQLite restart. `defer_until_ready` stores a pending recipe;
terminal completion or artifact insertion atomically materializes the child,
and `POST /chains/{chain_id}/reconcile` replays pending activation after
restart or repair. Child workers receive only explicitly declared parent
artifacts/messages, with hash-verified artifact-fetch evidence in the receipt.
Physical LAN validation remains a separate operational gate.

## Later — operational polish and earned scale

### AR-010 — Operator experience and installation

**Status:** partial · **Owner:** product/CLI · **Depends on:** AR-003, AR-007, AR-008

Deliverables:

- Guided setup, enrollment, upgrade, rollback, and troubleshooting.
- Clear CLI output plus JSON mode for automation.
- `agents`, `watch`, `inspect`, and `history` views that answer “what is happening?” quickly.
- Minimal TUI or local web console only if CLI evidence shows a real visibility gap.
- MCP façade exposing durable submit, bounded dispatch, inspection, watch,
  cancellation, and explicit chain submission to MCP clients.
- Optional single-machine local-worker mode for one-call `run`/`Agent` receipt
  behavior without bypassing the durable coordinator.
- Claude-MCP-compatible prompt/workdir input and an explicit `claude-mcp`
  worker transport for existing streamable-HTTP Claude MCP services.

Acceptance evidence:

- A new user can install two workers and run the acceptance scenario from documentation alone.
- Every terminal outcome is understandable without reading raw logs.
- An MCP client can discover and invoke the durable lifecycle without learning
  the coordinator's REST paths.
- A configured existing Claude MCP endpoint can be health-probed and can
  complete a durable read-only task with remote-authority evidence.

### AR-011 — Observability, audit, and evaluation productization

**Status:** planned · **Owner:** quality/operations · **Depends on:** AR-006, AR-008, AR-009

Deliverables:

- Metrics for completion, recovery, duplicate execution, scope violations, false readiness, and receipt completeness.
- Correlation IDs and replayable event history.
- Multi-machine evaluation fixtures and representative task cohorts.
- Adapter-by-adapter quality/latency/cost reports.

Acceptance evidence:

- North-star and SLO metrics can be computed from durable events.
- At least one interruption/recovery cohort is published with exact environment and limitations.
- Claims are traceable to source, test, runtime, or measured evidence.

### AR-012 — Team policy and hosted coordination

**Status:** later · **Owner:** product/platform · **Depends on:** AR-005, AR-006, AR-011

Deliverables:

- Multi-user identity, roles, quotas, approvals, and policy management.
- Optional hosted coordinator with tenancy and retention boundaries.
- Scheduling policy expansion beyond bounded priority/deadline queue ordering.
- Broader ecosystem integrations.

Acceptance evidence:

- Tenant isolation, authorization, audit, and deletion/retention behavior are tested.
- Hosted mode preserves the same task/receipt semantics as local/LAN mode.

## Deferred until the core is reliable

- Generic multi-agent swarm planning.
- Marketplace economics or open worker exchange.
- Unrestricted production side effects.
- Broad model/provider expansion without a measurable customer workflow.
- A dashboard that hides rather than improves lifecycle evidence.

## Definition of done for the roadmap

The roadmap is successful when a developer can submit a bounded job from PC A, have a compatible agent on PC B execute it under explicit policy, lose and regain the connection, and obtain a verified artifact/receipt without duplicate side effects or ambiguous success.

The supporting PM artifacts are in [`docs/pm/`](docs/pm/). Engineering benchmark controls remain in [`GOAL.md`](GOAL.md) and [`EVALS.md`](EVALS.md); they should be updated only when implementation and evidence change.
