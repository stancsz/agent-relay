# Product decision log

## D-001 — Product wedge is the A2A job control plane

**Decision:** Position Agent Relay as reliable cross-machine job delivery and proof for heterogeneous agents.

**Rationale:** The repository already owns task contracts, sandboxing, verification, retry, and receipts. The highest-value missing capability is durable remote execution, not another prompt abstraction.

**Consequences:** Prioritize lifecycle, identity, persistence, resume, artifacts, and operator evidence before broad swarm planning or a hosted marketplace.

## D-002 — Use A2A at the agent boundary and MCP inside the worker

**Decision:** Map external jobs to A2A-compatible concepts and keep MCP as the tool/data integration boundary inside each agent.

**Rationale:** The protocols are adjacent rather than interchangeable. This lets Agent Relay interoperate without owning every tool schema or agent reasoning loop.

## D-003 — Local-first, LAN-first deployment

**Decision:** Prove a self-hosted two-PC workflow before building a hosted coordinator.

**Rationale:** The target users already own machines and need control over code, credentials, workspaces, and artifacts. A local/LAN deployment also makes failures inspectable and reduces early tenancy complexity.

## D-004 — Bounded task contracts remain mandatory

**Decision:** Do not make unrestricted natural-language delegation the primary API.

**Rationale:** Explicit scope, verification, risk, and retry policy are what make safe routing and independent proof possible.

## D-005 — “Ready” and “complete” are evidence states

**Decision:** A worker is ready only after a capability smoke; a task is complete only with a terminal state, artifacts/result, verification evidence, and receipt.

**Rationale:** Current shallow health checks and response-only success semantics are the most dangerous sources of false confidence.

## D-006 — Do not build a distributed scheduler yet

**Decision:** Defer advanced scheduling, quotas, marketplace discovery, and multi-tenant hosting.

**Rationale:** These features amplify operational complexity before the basic job lifecycle is durable. They become reasonable after P2/P3 release gates pass.

## D-007 — Use configurable intelligence escalation, not static model routing

**Decision:** Route routine bounded work to ordinary workers and use an ordered,
versioned escalation policy to summon configurable high-intelligence Codex
planner/verifier lanes at high-leverage gates.

**Rationale:** Planning, ambiguity resolution, repeated-failure recovery, and
independent review can change the outcome of a large downstream run. Paying the
frontier cost for every edit wastes capacity, while a static “coding goes to
model X” router cannot observe the actual failure or evidence state.

**Consequences:** The policy must expose explicit stages, deterministic signals,
matched-rule receipts, and fail-closed behavior. High-model output is never a
substitute for scope, workspace, authorization, or deterministic verification.
