# Current-state product audit

This audit describes the inspected working tree, not a released package. “Implemented” means code/tests are present; external lane availability and artifact freshness are separate questions.

## Executive assessment

Agent Relay already has a differentiated bounded-delegation core:

- structured task contracts;
- deterministic fail-closed triage;
- local Qwen/Ollama and Codex-over-Ollama execution;
- Git sandboxing, patch normalization, scope checks, verification, retry, and receipts;
- batch/evaluation/economics tooling;
- Claude task, Codex review, and Antigravity adapter surfaces.

The cross-machine control-plane foundation now exists as a versioned HTTP/SQLite
slice, including durable task state, worker discovery, scoped credentials,
leases, reconnect/replay, artifact transfer, and truthful lifecycle receipts.
The Claude adapter can also dispatch to an already-running authenticated remote
Claude A2A daemon. It is not yet a release-proven two-PC product: physical LAN
execution, identity provisioning, real adapter interruption/recovery, and
side-effect-aware retry still need external acceptance evidence.

## Feature inventory

## Intelligence escalation status

The repository has separate worker and high-reasoning review lanes, but the
current audit must distinguish those adapters from a policy that decides when
to invoke them. The escalation-policy slice adds that missing decision layer:
versioned stage signals, ordered rules, explicit `continue`/`consult`/
`require_review`/`block` outcomes, and bounded consultation evidence. Until its
focused tests and CLI exercise pass, do not claim that Agent Relay has
automatic intelligent escalation; a manually invoked `review` is only a
review-lane capability.

| Capability | State | Evidence | Product assessment |
| --- | --- | --- | --- |
| Structured `DelegationTask` contract | Implemented | [`src/agent_relay/task.py`](../../src/agent_relay/task.py) | Strong foundation; reuse as the canonical job spec. |
| Deterministic triage | Implemented | [`src/agent_relay/triage.py`](../../src/agent_relay/triage.py) | Differentiated safety/economics gate; expose its reason codes remotely. |
| Local Qwen/Ollama worker | Implemented/conditional | [`src/agent_relay/ollama.py`](../../src/agent_relay/ollama.py) | Current measured lane; keep as reference adapter. |
| Codex-over-Ollama worker | Implemented/conditional | [`src/agent_relay/codex_worker.py`](../../src/agent_relay/codex_worker.py) | Strong bounded execution path; requires truthful environment checks. |
| Disposable Git sandbox | Implemented | [`src/agent_relay/sandbox.py`](../../src/agent_relay/sandbox.py) | Useful workspace isolation, but not OS-level isolation. |
| Patch/scope/verification/retry | Implemented | [`src/agent_relay/delegate.py`](../../src/agent_relay/delegate.py) | Core proof pipeline; must become a worker conformance contract. |
| Result receipts and compact handoff | Implemented | [`src/agent_relay/result.py`](../../src/agent_relay/result.py) | Directly maps to remote artifact/receipt UX. |
| Batch/evaluation/checkpointing | Implemented | [`src/agent_relay/batch.py`](../../src/agent_relay/batch.py), [`evals/runner.py`](../../evals/runner.py) | Good internal measurement; `batch` is sequential, not a distributed scheduler. |
| Claude task bridge | Implemented/conditional | [`src/agent_relay/claude_task.py`](../../src/agent_relay/claude_task.py), [`scripts/probe_claude_lane.py`](../../scripts/probe_claude_lane.py), [`scripts/smoke_claude_task.py`](../../scripts/smoke_claude_task.py) | Supports local ephemeral bridge or an existing authenticated remote A2A daemon; native team availability remains environment-dependent. |
| Codex review lane | Conditional | [`src/agent_relay/codex_review.py`](../../src/agent_relay/codex_review.py) | Useful verifier surface; environment readiness must be deeper than executable presence. |
| Antigravity lane | Conditional | [`src/agent_relay/agy_antigravity.py`](../../src/agent_relay/agy_antigravity.py) | Good specialist boundary; accept-edits needs stronger workspace/security policy. |
| Lane registry/doctor | Partial | [`src/agent_relay/lanes.py`](../../src/agent_relay/lanes.py) | Readiness now distinguishes ready/degraded/blocked/unknown; Codex/AGY still need invocation/auth smoke. |
| Remote A2A/MCP task API | Implemented locally/conditional LAN | [`src/agent_relay/control.py`](../../src/agent_relay/control.py), [`src/agent_relay/claude_task.py`](../../src/agent_relay/claude_task.py), [`src/agent_relay/claude_mcp.py`](../../src/agent_relay/claude_mcp.py), [`src/agent_relay/mcp.py`](../../src/agent_relay/mcp.py), and `serve`/`mcp`/lifecycle CLI | Versioned authenticated coordinator, direct remote Claude A2A dispatch, live existing-Claude-MCP dispatch, and an MCP façade work in local acceptance; physical Agent Relay two-PC proof remains. |
| Agent Cards/registry | Implemented locally | [`src/agent_relay/protocol.py`](../../src/agent_relay/protocol.py), SQLite registry, `/agents`, `/tasks/claim` | Registration, scoped worker identity, filtered discovery, heartbeats, rotation, revocation, server-side workspace-policy enforcement, and coordinator-owned priority/deadline claiming are covered. |
| Durable job store | Implemented locally | [`src/agent_relay/store.py`](../../src/agent_relay/store.py) | SQLite WAL persistence and coordinator restart recovery are covered; deployment, backup, and retention policy remain. |
| Leases/idempotency | Implemented locally/conditional LAN | SQLite lease table and [`scripts/acceptance_control_plane.py`](../../scripts/acceptance_control_plane.py) | Duplicate submit, expiry reassignment, stale-owner rejection, and restart reconnect pass in a separate coordinator process; physical multi-worker recovery remains. |
| Artifact transfer/store | Implemented locally/partial operations | Hash-checked artifact endpoint, receipt refs, scoped parent-artifact grants, and acceptance artifact hash | Patch artifacts and terminal receipts survive restart; retention and larger artifact policy remain. |
| Identity/authentication | Partial/conditional | Shared coordinator bearer plus enrolled/revocable worker credentials, actor binding, optional TLS, and [`scripts/acceptance_tls.py`](../../scripts/acceptance_tls.py) | HTTPS and worker-scoped authorization are implemented; certificate/CA provisioning and finer-grained client roles remain. |
| Cancellation semantics | Partial | `cancel_requested`, Claude bridge cancellation, worker-confirmed `cancelled`/`blocked` receipts, bounded retryable bridge-failure recovery, and separate-process Claude interruption smoke | Local-Qwen stop support, physical two-PC interruption, and adapter resume cohorts remain. |
| Progress/update transport | Implemented locally/partial adapters | JSON event replay plus bounded SSE `/events/stream` | Reconnect and missed-event replay are covered; client library and adapter interruption propagation remain. |
| Durable follow-up chains | Implemented locally | `POST /chains/{chain_id}/steps`, `POST /chains/{chain_id}/reconcile`, `GET /chains/{chain_id}`, and `chain-submit` | Deferred steps auto-materialize on terminal completion, restart reconciliation is coordinator-owned, idempotency includes a request hash, and workers receive only hash-verified declared parent inputs; physical LAN evidence remains. |
| Observability/audit UI | Partial | Durable event history, bounded receipts, CLI `watch`/`inspect`, MCP `inspect`/`watch` | Operator CLI and MCP surfaces exist; a richer dashboard, metrics, and retention controls remain. |

## Evidence-backed strengths

- The task contract has explicit files, context, constraints, verification, success criteria, task kind, and risk flags.
- Triage is deterministic and fail-closed for broad, unsafe, unverifiable, or uneconomic tasks.
- The delegation path uses a disposable Git sandbox, applies returned patches through parent-owned checks, and supports one bounded retry.
- Receipts carry status, changed files, verification evidence, patch hashes, blockers, and artifact paths.
- The coordinator acceptance harness passes idempotent submit, expired-lease reassignment,
  stale-worker rejection, artifact/receipt persistence, coordinator restart, SSE
  replay, scoped credential survival, and revocation checks in separate processes.
- The real Claude lane passes capability discovery and a disposable bounded edit;
  the parent verifier confirms the declared check and the caller worktree remains
  unchanged. Native Agent Teams are not available in the recorded environment,
  so the bounded CLI fallback is explicit.
- Agent Relay also submits through a real in-process HTTP instance of the existing
  Claude A2A daemon, authenticates the durable job, polls it, and maps its
  receipt/patch. That proves the remote adapter wiring; it does not prove an
  actual second-PC network or production Claude quality.
- A real Claude cancellation smoke now reaches `running`, records
  `cancel_requested`, and returns a worker-confirmed `cancelled` receipt with
  `execution_stopped: true`; a regression test preserves durable job-state
  precedence over a nested failed adapter result.
- Retryable Claude bridge liveness/connection failures now produce a durable
  `waiting` recovery event, release ownership, and consume the task's bounded
  `retry_limit` instead of becoming an immediate terminal failure.
- The real separate-process Claude interruption smoke now kills a worker after
  `running`, observes lease-expiry reassignment, and produces a fresh verified
  success receipt with the caller worktree unchanged; the physical two-PC
  version remains unproven.
- The current Qwen3.5:4B cohort records 45 eligible tasks, 45/45 after retry, zero scope violations, correct blocked decisions, and a measured 97.390% frontier-token reduction under the documented accounting method. This is a narrow cohort result, not a general coding claim.

## Release and operational risks still visible

The current local source and package skill validators pass. The remaining risks below are product/release hardening work that must stay on the roadmap.

1. Codex and Antigravity still need invocation/auth/model capability smokes before they can be marked `ready`.
2. Verification commands use `shell=True` and timeout does not guarantee descendant-process cleanup; the repository explicitly does not claim OS-level sandboxing.
3. The current package/configuration changes risk removing historical `lcd`/`LCD_*` compatibility without a migration window.
4. The coordinator now has enrolled/revocable worker credentials, actor binding, and an HTTPS path with CA verification, but still uses a shared bearer for admin/client operations and leaves certificate/CA provisioning to deployment.
5. The separate-process acceptance harness is not a physical LAN test and has not exercised real remote adapter execution plus interruption recovery across two PCs.
6. Claude cancellation is implemented and has a real local worker-plane smoke; a multi-machine interruption/recovery cohort and local-Qwen stop support are still unproven.
7. Automatic follow-up chaining is implemented locally with deferred recipes and coordinator-owned reconciliation; physical two-PC execution and interruption recovery remain external gates.

## Product maturity judgment

**Current maturity:** measured local bounded-delegation MVP with conditional heterogeneous adapters.

**Next maturity threshold:** LAN-capable A2A job plane where a task can be submitted, owned, observed, resumed, verified, and inspected across two machines without relying on an open terminal or undocumented human coordination.
