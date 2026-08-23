# Product strategy

## Mission

Make heterogeneous AI agents useful together across real machines by turning delegation into a reliable job lifecycle with explicit boundaries and proof.

## Product thesis

Frontier agents should spend attention on decomposition, judgment, and acceptance. Cheaper or specialized agents should perform bounded, repeatable work. Agent Relay creates the missing reliability layer: it carries the contract, chooses a compatible worker, isolates execution, tracks progress, preserves artifacts, verifies results, and returns a compact receipt.

The model-routing corollary is configurable escalation: spend high-intelligence
Codex calls on high-leverage planning, ambiguity, recovery, and independent
review, not on routine file edits or formatting. See [Escalation policy](escalation-policy.md).

The thesis is only valuable if it survives a machine boundary. A local delegation that cannot be discovered, authenticated, resumed, or audited remotely is a useful library feature but not yet the product.

## Target customer and first wedge

### Primary ICP

Solo developers and small engineering teams who have two or more PCs, workstations, or GPU machines and already use more than one agent harness. They want to dispatch work to the machine that has the right model, credentials, operating system, browser, GPU, or repository checkout without manually coordinating terminals and chat windows.

### First high-value workflow

1. A developer on PC A submits a bounded repository task.
2. Agent Relay discovers a compatible worker on PC B.
3. PC B accepts a lease and executes in its permitted workspace.
4. The submitter watches durable progress and can reconnect after a network interruption.
5. The worker returns a patch or artifact bundle plus verification evidence.
6. Agent Relay records a receipt tied to task, worker, workspace, artifacts, and verification.
7. The developer reviews and integrates the result on PC A.

For difficult jobs, the flow has explicit policy gates before execution and
before acceptance. A high planner can shape the work, while a separate high
verifier can challenge the candidate result. Neither opinion bypasses the
task contract or deterministic proof.

This is concrete enough to demonstrate value and narrow enough to avoid building a full distributed agent platform on day one.

## Positioning

### One-sentence positioning

Agent Relay is the A2A job control plane for developers running heterogeneous agents across multiple machines: route the right bounded job, keep it recoverable, and return proof of what happened.

### What it is not

- Not a replacement for Codex, Claude, Ollama, Antigravity, or another agent harness.
- Not a model router whose only output is a prompt-response pair.
- Not a general-purpose distributed scheduler before job durability and safety are proven.
- Not an enterprise multi-tenant control plane in the first release.
- Not a claim that every lane is equally mature or that benchmark economics generalize beyond the recorded cohort.

## Value proposition

| Customer pain | Agent Relay promise | Proof required |
| --- | --- | --- |
| The right agent is on another machine | Discover and route to a compatible worker | Agent Card/capability record and routing trace |
| Remote jobs disappear when a terminal or network drops | Persist task state and resume safely | Reconnect/resume integration tests |
| Agents modify too much or in the wrong place | Enforce task, workspace, and artifact boundaries | Scope review, workspace fingerprint, negative tests |
| A plausible answer is not a completed job | Return artifacts and independent verification | Receipt with evidence and verifier outcome |
| Multiple agents are hard to compare | Normalize lifecycle, status, and result contracts | Adapter conformance suite |

## North-star outcome

**Verified jobs completed with recoverable evidence per unit of frontier attention and infrastructure cost.**

This prevents optimizing for raw delegation volume. A job counts only when it has a terminal state, a bounded artifact/result, verification evidence, and a receipt that can be inspected after the worker disconnects.

## Strategic bets

1. **Reliability is the wedge.** The first durable advantage is not another model integration; it is making remote work trustworthy.
2. **A2A-compatible at the boundary.** Use standard concepts where they fit, while keeping Agent Relay’s internal scheduling, sandbox, and verification policy explicit.
3. **Local-first and LAN-first.** Earn trust with self-hosted, inspectable operation before adding a hosted coordinator.
4. **Evidence over claims.** Every adapter and benchmark must distinguish source support, environment readiness, measured quality, and released support.
5. **Bounded jobs before arbitrary autonomy.** A narrow task contract is a product asset: it makes routing, safety, retries, and verification tractable.

## Product risks

- **Protocol drift:** A2A and MCP evolve; pin versions and maintain conformance fixtures.
- **False readiness:** An executable being on `PATH` is not proof that a lane can authenticate, run, or produce a valid result.
- **Security boundary confusion:** A Git worktree is not an OS sandbox, and a trusted verification command is not safe for untrusted input.
- **Distributed-systems complexity:** Persistence, leases, cancellation, retries, and idempotency can create duplicate side effects if designed casually.
- **Over-expansion:** Adding more adapters before the core lifecycle works will increase support surface without increasing customer value.
