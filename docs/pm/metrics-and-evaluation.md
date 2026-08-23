# Metrics and evaluation

## Escalation metrics

The intelligence-escalation policy is successful only if it improves verified
job outcomes without turning every task into a high-model call. Record one
decision event per evaluated stage with the policy version, matched rule,
action, profile/model, latency, and eventual outcome. Track escalation rate by
stage and rule, actionable defects found by high review, recovery success after
consultation, false-positive consultations, high-model cost/latency per
verified job, and blocked jobs caused by unavailable high-lane capability.

A high-model response is not a quality label by itself. The acceptance record
must still include artifacts, scope/workspace checks, deterministic
verification, and—when required—a bounded independent review receipt.

## North-star metric

**Verified recoverable job completion rate** = terminal jobs with valid artifacts, verification evidence, and inspectable receipts ÷ accepted jobs.

Report this by adapter, task kind, machine pair, repository size, and retry policy. Do not aggregate away blocked or unknown outcomes.

## Reliability metrics

- **Acceptance rate:** accepted submissions ÷ valid submissions.
- **Completion rate:** succeeded jobs ÷ accepted jobs.
- **Recovery success:** jobs resumed or correctly recovered after interruption ÷ interrupted jobs.
- **Duplicate execution rate:** tasks with more than one worker side effect ÷ accepted tasks; target zero for idempotent submissions.
- **Receipt completeness:** terminal jobs with all required evidence fields ÷ terminal jobs.
- **Scope violation rate:** rejected out-of-scope outputs ÷ executed jobs; target zero accepted violations.
- **False readiness rate:** workers reported ready but failing a real capability smoke ÷ probed workers; target zero.

## User experience metrics

- Time to first acknowledgement.
- Time to first progress event.
- Time to terminal result.
- Time to reconnect after client/network loss.
- Number of operator actions required to inspect and integrate a result.

## Economics and quality

Continue the existing evaluation distinction:

- case success;
- frontier-token accounting;
- wall-clock overhead;
- scope and safety review;
- verification completeness;
- human or independent-review quality.

The existing 97.390% frontier-token reduction is valid only for the recorded Qwen3.5:4B cohort and accounting method in [`EVALS.md`](../../EVALS.md). It should be treated as an internal bounded-task signal, not as a market-wide or general coding claim.

## Evaluation layers

1. **Contract tests:** schema, path boundaries, state transitions, idempotency, and invalid inputs.
2. **Adapter tests:** capability probes, parsing, cancellation, timeout, and result normalization.
3. **Coordinator integration tests:** two processes, persistence, leases, reconnect, restart, and artifact transfer.
4. **Adversarial tests:** duplicate submit, stale lease, worker crash, partial artifact, malicious path, oversized output, secret leakage, and contradictory status.
5. **Human acceptance:** a developer can understand what happened and decide whether to integrate without opening worker internals.
6. **Cohort benchmarks:** representative task families across multiple machine/provider combinations, with exact environment recorded.

## Evidence policy

Every product claim should be labeled as one of:

- source/test evidence;
- local environment observation;
- measured cohort result;
- intended behavior;
- external dependency or blocker.

“Available” must mean capable of the requested operation in the current environment, not merely installed. “Completed” must mean verified and receipted, not merely returned a response.
