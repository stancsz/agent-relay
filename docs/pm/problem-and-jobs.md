# Problem, users, and jobs to be done

## The problem

AI agents are increasingly specialized by model, machine, tool access, credentials, operating system, and workspace. A developer can often get better results by sending a job to another agent, but the current handoff is usually a fragile combination of chat text, terminal state, copied files, and personal memory.

The resulting failures are predictable:

- the sender cannot tell which workers are actually available;
- a remote worker receives an underspecified task or too much context;
- a network or process interruption loses the job’s state;
- a retry repeats a side effect or creates conflicting edits;
- artifacts are returned without provenance or verification;
- a successful-looking response is mistaken for a completed job;
- operators cannot reconstruct what happened later.

## Personas

### The frontier supervisor

Owns architecture, prioritization, acceptance, and risk. Wants to offload mechanical work without losing control of scope or evidence.

### The specialist worker

Runs on a machine with a specific capability: local model/GPU, browser, Android toolchain, cloud credentials, or a particular agent harness. Needs a clear contract and bounded permissions.

### The verifier/operator

May be the same person as the supervisor. Needs durable receipts, logs, artifacts, and explicit blocked states. Does not want to infer success from a green process exit alone.

## Jobs to be done

### Primary job

When I have a bounded engineering task that another machine or agent can do better or cheaper, I want to submit it with enough constraints and observe it to completion, so that I can integrate the result without guessing what happened.

### Supporting jobs

- Discover workers by capability, workspace, trust, and current load.
- Choose a worker using explicit policy rather than hidden model preference.
- See progress without keeping a terminal session open.
- Reconnect and resume after a client, worker, or network interruption.
- Prevent duplicate execution when a submitter retries a request.
- Inspect changed files, artifacts, verification output, and provenance.
- Cancel a job and know whether cancellation stopped work or only stopped observation.
- Compare worker quality, latency, cost, and failure modes over time.

## First user journey

| Stage | User question | Product behavior |
| --- | --- | --- |
| Discover | “Which PC can do this?” | Agent Cards/capabilities, health, trust, workspace constraints |
| Submit | “Did the job arrive?” | Idempotent task submission with durable task ID |
| Accept | “Who owns it?” | Lease, worker identity, accepted timestamp, expiry |
| Execute | “Is it making progress?” | State updates, heartbeats, bounded logs, artifacts-in-progress |
| Recover | “What if the connection drops?” | Poll, resubscribe, resume, retry policy, no duplicate side effects |
| Verify | “Is it actually done?” | Parent-owned verification and explicit terminal status |
| Integrate | “What do I take back?” | Patch/artifact bundle, hashes, changed-file list, receipt |

## Pain severity

The highest-severity problem is not “agents cannot talk.” It is “a cross-machine job cannot be trusted after something goes wrong.” Therefore the product should prioritize lifecycle durability and proof before adding conversational richness, marketplace discovery, or broad autonomous planning.

## Out-of-scope jobs for the first product

- Coordinating arbitrary long-running multi-agent research graphs.
- Sharing unrestricted user credentials between machines.
- Automatically merging changes into production.
- Replacing repository hosting, CI, secrets management, or endpoint management.
- Optimizing every possible model/provider combination.
