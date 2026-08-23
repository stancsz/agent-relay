# Agent Relay product management

This folder is the product-management source of truth for Agent Relay. It complements, rather than replaces, the engineering controls in [`GOAL.md`](../../GOAL.md), [`EVALS.md`](../../EVALS.md), and [`docs/RELEASE.md`](../RELEASE.md).

## Executive decision

Agent Relay should become a reliable A2A job control plane for heterogeneous agents running across multiple PCs. Its job is not to be another agent framework, model marketplace, or generic swarm API. Its job is to make a remote agent job deliverable, bounded, resumable, inspectable, and provably complete.

The product wedge is:

> Submit one bounded engineering job from one machine, have the best available compatible agent execute it on another machine, and receive a recoverable evidence package that a human or verifier can trust.

The repository now includes a local and separate-process cross-machine control
plane slice: canonical task lifecycle, discovery, scoped worker identity,
persistence, leases, reconnect/replay, artifact transfer, and truthful
readiness. The remaining product gap is release evidence across two physical
PCs, including real adapter interruption/recovery, LAN TLS/identity
provisioning, and side-effect-aware retry.

## Read this first

- [Product strategy](product-strategy.md) — positioning, ICP, wedge, north star, and non-goals.
- [Problem and jobs](problem-and-jobs.md) — the user problem and the first end-to-end workflow.
- [Current-state audit](current-state-audit.md) — what is implemented, conditional, planned, or blocked.
- [Remote Claude/A2A QA report](qa-report.md) — executed validation and the remaining physical-LAN gate.
- [A2A direction](a2a-direction.md) — how Agent Relay should layer on A2A and MCP.
- [Requirements](requirements.md) — MVP product requirements and acceptance criteria.
- [Physical LAN acceptance](lan-acceptance.md) — two-PC coordinator/worker runbook and interruption evidence.
- [Metrics and evaluation](metrics-and-evaluation.md) — product metrics, SLOs, and evidence policy.
- [Escalation policy](escalation-policy.md) — configurable gates for bulk workers and high-intelligence consultation.
- [Release gates](release-gates.md) — what must be true before each release stage.
- [Decision log](decision-log.md) — durable product decisions and their rationale.
- [Product roadmap](roadmap.md) — phase structure and sequencing; the root [`ROADMAP.md`](../../ROADMAP.md) is the executable backlog.

## Status language

- **Implemented** means present in the inspected working tree and covered by code or tests; it does not imply released or operationally healthy.
- **Conditional** means the adapter or path exists but depends on an external executable, service, credential, model, or environment that is not guaranteed.
- **Measured** means supported by a recorded evaluation run with a defined cohort and accounting method.
- **Planned** means a product decision, not an existing feature.
- **Blocked** means a specific failure or missing prerequisite prevents a truthful release claim.

## Product boundary

Agent Relay owns cross-agent job delivery and proof. An individual agent owns its reasoning and tool use. MCP remains the natural protocol for the tools and data inside an agent; A2A is the natural interoperability surface between independent agents; Agent Relay adds the operational control plane around those interactions.

The escalation policy is part of that control plane: it decides when a high-
intelligence planner or verifier is worth consulting, while deterministic tests,
scope checks, workspace policy, and receipts remain the acceptance authority.

See the [A2A direction](a2a-direction.md) for the boundary in detail.
