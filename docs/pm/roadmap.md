# Product roadmap structure

The executable backlog is the root [`ROADMAP.md`](../../ROADMAP.md). This page explains the sequencing logic so future edits do not turn the roadmap into a list of unrelated integrations.

## Sequence

### Phase 0 — make the current foundation truthful

Repair release artifacts, compatibility, readiness probes, CI installation coverage, and verifier-process boundaries. This reduces the chance of shipping a polished-looking control plane on top of false health signals.

### Phase 1 — define the job and worker boundary

Freeze the canonical task/state/artifact/receipt schemas, publish Agent Cards, define capability and trust vocabulary, and specify the transport/version policy.

### Phase 2 — build the durable control plane

Implement coordinator persistence, idempotent submission, worker registration, leases, heartbeats, polling, streaming updates, cancellation, and reconnect/resume.

### Phase 3 — connect real workers

Wrap local-Qwen/Codex, Claude, Codex review, and Antigravity behind the common lifecycle. Measure actual readiness per environment and keep conditional lanes explicitly conditional.

The intelligence-escalation slice belongs here: ordinary workers handle bulk
execution, while configurable policy gates summon a high planner or verifier
for ambiguity, high consequence, repeated failure, missing proof, and release
decisions.

### Phase 4 — make evidence portable

Transfer artifacts, hashes, logs, workspace fingerprints, verification results, and receipts across machines. Add audit/replay and bounded retention.

### Phase 5 — productize the operator experience

Ship a clean CLI, setup/upgrade path, examples, troubleshooting, TUI or minimal web console, and multi-machine evaluation fixtures.

### Phase 6 — scale only when earned

Add multi-user identity, quotas, scheduling, policy approvals, hosted coordination, and broader ecosystem integrations only when the LAN MVP has strong recovery and proof metrics.

## Prioritization rule

An item is higher priority when it improves the probability that a remote job is completed, recovered, verified, or safely understood. A new model adapter is lower priority than a missing lease, receipt, or artifact hash unless it unlocks the first end-to-end scenario.
