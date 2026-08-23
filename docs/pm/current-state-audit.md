# Current-state product audit

This audit describes the inspected working tree, not a released package. “Implemented” means code/tests are present; external lane availability and artifact freshness are separate questions.

## Executive assessment

Agent Relay already has a differentiated bounded-delegation core:

- structured task contracts;
- deterministic fail-closed triage;
- local Qwen/Ollama and Codex-over-Ollama execution;
- Git sandboxing, patch normalization, scope checks, verification, retry, and receipts;
- batch/evaluation/economics tooling;
- Claude worker, Sol reviewer, and Antigravity adapter surfaces.

The cross-machine control-plane foundation now exists as a versioned HTTP/SQLite
slice, including durable task state, worker discovery, scoped credentials,
leases, reconnect/replay, artifact transfer, and truthful lifecycle receipts.
The one-PC 10.x flight below exercises that plane through a real interface and
the Claude adapter can dispatch to an already-running authenticated remote
Claude A2A daemon. This is not a claim of physical two-PC acceptance: identity
provisioning, real adapter interruption/recovery across machines, and
side-effect-aware retry remain separate hardening work.

## Implementation coverage

The feature inventory below contains 23 capability rows. On the current tree,
19/23 (83%) have an implemented source/test slice plus local fixture or flight
evidence. The denominator is explicit and unweighted: one row counts once, and
external entitlement or production deployment is not silently counted as
proof. Two rows (identity/authentication and cancellation) remain partial, and
two rows (Sol and Antigravity) remain conditional-only; those four are excluded
from the 19-row numerator. Lane readiness and observability still have product
hardening gaps, but their local source/test slices are present and therefore are
included in the implementation-coverage numerator. This is not a product-
readiness score, and the physical two-PC gate remains unclaimed.

The most important gaps are tracked explicitly rather than hidden inside that
percentage: physical PC-A/PC-B execution and interruption recovery, real LAN
certificate/CA and firewall provisioning, native Claude Agent Team availability
in the installed runtime, side-effect-aware retry, local-Qwen stop support,
dashboard/metrics/retention controls, finer-grained client roles, and
production backup/deployment policy. Automatic result-driven multi-step
orchestrator submission is also not implemented; the existing follow-up-chain
APIs are durable local primitives, not proof of that end-to-end behavior.

## 2026-08-23 one-PC 10.x flight findings

- Coordinator source was launched on `10.0.0.149:8793` with an admin bearer;
  worker `lan-worker-10x` registered with a scoped credential and communicated
  over that address. Submission, claim, lease ownership, `running`, lease
  renewal, terminal receipt, artifact upload, coordinator restart, and event
  replay were observed from the persisted SQLite store.
- The first remote Claude task returned a bounded PM patch but had no declared
  deterministic verification. The acceptance gate correctly produced a failed
  receipt with blocker `deterministic verification evidence is missing`; Sol
  was not invoked because the earlier gate failed.
- The next run exposed that remote Claude results were not being rehydrated into
  a parent-owned verification sandbox. That is now fixed in
  `src/agent_relay/claude_task.py`, with a regression test; the parent applies
  only declared patch paths to a disposable sandbox and runs the declared
  checks before Sol review.
- A subsequent run exposed dirty-checkout patch-base drift: the remote patch
  was relative to `HEAD` while the parent sandbox copied the caller's dirty
  target. The verifier now restores only declared targets from `HEAD` when
  available before applying the remote patch. A stale or non-applying patch
  remains a truthful worker error.
- A final retry (`lan-flight-task-v4`) exercised the corrected lifecycle but
  Claude's fallback worker returned `Server error mid-response` after producing
  an untrusted patch artifact. Agent Relay recorded `WORKER_ERROR`, did not run
  parent verification or Sol acceptance, and did not accept the artifact. The
  intermittent Claude bridge/API response failure remains an explicit
  transport blocker rather than a success claim.
- The first flight used the repository source explicitly via `PYTHONPATH=src`
  because the pre-existing PATH executable was stale and lacked `serve`. The
  editable package was then refreshed with `py -3 -m pip install --editable .`;
  the installed command now exposes `serve`, `worker`, and `submit`. The stale
  installation was a packaging/version skew found and corrected during the
  flight.
- Claude Code 2.1.233 exposed `Agent` but not native team task tools. The live
  successful team review therefore used explicit `transport: cli-fallback`; it
  must not be reported as a native Agent Team run.

## 2026-08-23 remote collaboration and Sol-context hardening

- Claude A2A packets now carry a digest-linked, size-limited
  `collaboration` contract. It requires the remote worker to state observable
  remote state, assumptions, exact `questions_for_orchestrator`, a
  `recommended_next_prompt`, scope, verification evidence, and blockers. The
  contract explicitly limits the exchange to declared inputs and target paths;
  transcripts and repository dumps remain rejected.
- The same contract is rendered into single-worker, native-team, and existing
  remote-MCP prompts. Parent receipts retain the bounded context inputs, packet
  digest, adapter authority, and worker handoff so the orchestrator can form a
  next prompt without guessing at the remote machine's unseen codebase.
- Before Sol runs, the parent review prompt now includes task constraints,
  success criteria, declared verification commands, bounded input excerpts and
  hashes, the collaboration contract, worker handoff, and verification
  authority. Deterministic parent-owned verification remains a separate gate.
- A fresh current-tree verifier bridge accepted the new packet but Claude CLI
  returned no result. The job left HEAD/content/status unchanged and is
  recorded as inconclusive; this is a runtime Claude availability failure, not
  evidence that the contract was accepted by an external reviewer.

## 2026-08-23 babystep evidence enforcement

- Claude task packets now carry a bounded `babystep-evidence` contract with
  `inspect`, `plan`, `execute`, `verify`, and `handoff` steps. Each step requires
  an exact `PROGRESS <step> | status=... | evidence=...` line; `not_applicable`
  still requires a concrete explanation.
- The A2A native MCP path, CLI fallback receipts, parent Claude adapter, and
  existing remote MCP adapter all fail closed when a required step is missing or
  marked `blocked`. The parsed evidence is retained in the receipt and passed
  into the Sol review prompt before review eligibility.
- This closes the specific failure mode where a worker returns no useful work or
  verification but the verifier receives an apparently successful handoff. It
  does not prove Claude produced good work; it proves the handoff cannot be
  accepted without step-level evidence.

## Feature inventory

## Intelligence escalation status

The repository has separate worker and high-reasoning review lanes, but the
current audit must distinguish those adapters from a policy that decides when
to invoke them. The escalation-policy slice now adds a local decision and
consultation surface: versioned stage signals, ordered rules, explicit
`continue`/`consult`/`require_review`/`block` outcomes, and bounded
consultation evidence. The default local flow summons Sol high at `plan_end`
and `review_end`. Durable remote submissions still need an orchestrator or
follow-up chain to carry the same decision and receipt; a manually invoked
`review` remains a manual Sol-reviewer capability; the primary Claude-worker
path now invokes the same read-only gate automatically.

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
| Claude worker/orchestrator backend | Implemented/conditional | [`src/agent_relay/claude_task.py`](../../src/agent_relay/claude_task.py), [`lanes/claude-task/SKILL.md`](../../lanes/claude-task/SKILL.md), [`scripts/probe_claude_lane.py`](../../scripts/probe_claude_lane.py), [`scripts/smoke_claude_task.py`](../../scripts/smoke_claude_task.py) | Supports local ephemeral bridge or an existing authenticated remote A2A daemon; native team availability remains environment-dependent. |
| Claude worker acceptance gate | Implemented locally | [`src/agent_relay/acceptance.py`](../../src/agent_relay/acceptance.py), [`src/agent_relay/worker_plane.py`](../../src/agent_relay/worker_plane.py), [`src/agent_relay/store.py`](../../src/agent_relay/store.py) | `claude-task` is the default worker; deterministic verification and a passing `sol-reviewer` receipt are required before success is accepted. |
| Sol reviewer lane | Conditional | [`src/agent_relay/codex_review.py`](../../src/agent_relay/codex_review.py), [`src/agent_relay/acceptance.py`](../../src/agent_relay/acceptance.py) | Read-only `gpt-5.6-sol` review; environment readiness must be deeper than executable presence. |
| Antigravity lane | Conditional | [`src/agent_relay/agy_antigravity.py`](../../src/agent_relay/agy_antigravity.py) | Good specialist boundary; accept-edits needs stronger workspace/security policy. |
| Lane registry/doctor | Implemented locally; readiness partial | [`src/agent_relay/lanes.py`](../../src/agent_relay/lanes.py) | Readiness distinguishes ready/degraded/blocked/unknown and has source/tests; Codex/AGY still need invocation/auth smoke. |
| Remote A2A/MCP task API | Implemented locally/one-PC LAN | [`src/agent_relay/control.py`](../../src/agent_relay/control.py), [`src/agent_relay/claude_task.py`](../../src/agent_relay/claude_task.py), [`src/agent_relay/claude_mcp.py`](../../src/agent_relay/claude_mcp.py), [`src/agent_relay/mcp.py`](../../src/agent_relay/mcp.py), and `serve`/`mcp`/lifecycle CLI | Versioned authenticated coordinator, digest-linked bounded collaboration handoff, direct remote Claude A2A dispatch with parent-owned verification, live existing-Claude-MCP dispatch, and an MCP façade work in local acceptance; one-PC `10.0.0.149` evidence is recorded, physical two-PC proof remains unclaimed. |
| Agent Cards/registry | Implemented locally | [`src/agent_relay/protocol.py`](../../src/agent_relay/protocol.py), SQLite registry, `/agents`, `/tasks/claim` | Registration, scoped worker identity, filtered discovery, heartbeats, rotation, revocation, server-side workspace-policy enforcement, and coordinator-owned priority/deadline claiming are covered. |
| Durable job store | Implemented locally | [`src/agent_relay/store.py`](../../src/agent_relay/store.py) | SQLite WAL persistence and coordinator restart recovery are covered; deployment, backup, and retention policy remain. |
| Leases/idempotency | Implemented locally/one-PC LAN | SQLite lease table and [`scripts/acceptance_control_plane.py`](../../scripts/acceptance_control_plane.py) | Duplicate submit, 10.x lease ownership/renewal, stale-owner rejection, and restart reconnect pass; physical multi-worker recovery remains. |
| Artifact transfer/store | Implemented locally/one-PC LAN; retention partial | Hash-checked artifact endpoint, receipt refs, scoped parent-artifact grants, and acceptance artifact hash | The flight stored a patch artifact and terminal receipt across restart; retention and larger artifact policy remain. |
| Identity/authentication | Partial/conditional | Shared coordinator bearer plus enrolled/revocable worker credentials, actor binding, optional TLS, and [`scripts/acceptance_tls.py`](../../scripts/acceptance_tls.py) | HTTPS and worker-scoped authorization are implemented; certificate/CA provisioning and finer-grained client roles remain. |
| Cancellation semantics | Partial | `cancel_requested`, Claude bridge cancellation, worker-confirmed `cancelled`/`blocked` receipts, bounded retryable bridge-failure recovery, and separate-process Claude interruption smoke | Local-Qwen stop support, physical two-PC interruption, and adapter resume cohorts remain. |
| Progress/update transport | Implemented locally/partial adapters | JSON event replay plus bounded SSE `/events/stream` | Reconnect and missed-event replay are covered; client library and adapter interruption propagation remain. |
| Durable follow-up chains | Implemented locally | `POST /chains/{chain_id}/steps`, `POST /chains/{chain_id}/reconcile`, `GET /chains/{chain_id}`, and `chain-submit` | Deferred steps auto-materialize on terminal completion, restart reconciliation is coordinator-owned, idempotency includes a request hash, and workers receive only hash-verified declared parent inputs; physical LAN evidence remains. |
| Observability/audit UI | Implemented locally; productization partial | Durable event history, bounded receipts, CLI `watch`/`inspect`, MCP `inspect`/`watch` | JSON/SSE replay and persisted event inspection were exercised; a richer dashboard, metrics, and retention controls remain. |

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
- A one-PC 10.0.0.149 LAN flight of the authenticated coordinator/worker HTTP
  plane was observed on a single host. That run exercises the authenticated
  coordinator/worker HTTP round-trip on one host only; physical two-PC
  execution evidence is not claimed, and the documented two-PC gap above is
  therefore unchanged.
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
