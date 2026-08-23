# A2A and MCP product direction

## Research conclusion

A2A and MCP solve adjacent problems. The [A2A specification](https://a2aproject.github.io/A2A/latest/specification/) is designed for communication between independent agents, including discovery, task lifecycle, messages, artifacts, streaming, and asynchronous/push patterns. The [MCP architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture) describes host/client/server interaction for exposing tools, resources, and prompts to an agent. The [MCP server overview](https://modelcontextprotocol.io/specification/2025-03-26/server) makes the control split explicit: prompts are user-controlled, resources are application-controlled, and tools are model-controlled.

That creates a clean product boundary:

```text
User / supervisor
        |
        |  A2A-compatible job, task state, artifacts, updates
        v
Agent Relay: delivery, identity, routing, leases, resume, policy, proof
        |
        |  local agent harness invocation
        v
Worker agent: reasoning, tools, model choice, workspace execution
        |
        |  MCP tools/resources/prompts
        v
Tool and data systems
```

## What Agent Relay should own

- Agent Card publication and capability matching.
- Task submission, idempotency, durable state, and terminal outcomes.
- Worker identity, trust, authorization, and workspace policy.
- Routing, leases, heartbeats, cancellation, retry, and resume.
- Artifact exchange, hashes, retention, and provenance.
- Verification policy and evidence receipts.
- Operator audit, event history, metrics, and replayable diagnostics.

## What Agent Relay should not own

- The internal prompt/tool reasoning loop of each agent.
- Every tool schema or data connector exposed to a worker.
- A proprietary replacement for MCP.
- A forced common model or common vendor runtime.
- Unbounded autonomy without a task contract and policy boundary.

## Compatibility strategy

1. Use A2A concepts at the external job boundary: Agent Card, task, message, artifact, status, and update stream.
2. Preserve the existing `DelegationTask` as the internal policy-rich contract, mapping it to an A2A task envelope rather than discarding fields such as allowed files, verification, risk flags, and retry limits.
3. Keep adapter-specific execution behind a common lifecycle interface.
4. Use HTTP(S), JSON-RPC, SSE, and push/webhook patterns only where they improve interoperability; keep a simple local/LAN transport available for first-party deployments.
5. Treat protocol version, capability version, and relay implementation version as separate fields in receipts.

## Why this direction fits the repository

The repository already has the hard-to-fake parts of a controlled execution path: task contracts, triage, sandboxing, patches, verification, retries, receipts, and multiple lane adapters. Reframing those as the policy and proof layer around a durable A2A job makes the existing work cumulative.

By contrast, pursuing a broad “multi-agent swarm” product now would require solving orchestration, shared memory, planning, scheduling, and UI simultaneously. That would dilute the strongest current asset: bounded work with evidence.

## Competitive/adjacent landscape implication

Projects such as [AGNTCY](https://agntcy.org/) demonstrate that discovery, identity, messaging, and observability are becoming recognized infrastructure concerns for multi-agent systems. Agent Relay should differentiate through developer-grade execution proof and workspace safety, not by attempting to recreate every ecosystem service.

## Protocol decisions to make early

- Whether Agent Relay speaks a strict A2A surface or a compatibility subset with explicit extensions.
- Which task states are canonical and which are adapter-local.
- Whether artifacts are content-addressed locally first, with optional object storage later.
- How worker identity is provisioned on a self-hosted LAN.
- Whether “cancelled” means a cancellation request was sent or worker execution was confirmed stopped.
- How side-effecting jobs are declared and what retry/idempotency guarantees they receive.
