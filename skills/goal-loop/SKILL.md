---
name: goal-loop
description: Run a durable, document-driven Claude goal loop when the user wants Codex to follow ROADMAP.md, maintain GOAL.md and EVAL.md, and supervise one Claude orchestrator coordinating up to three Claude subagents. Do not use for ordinary one-off delegation.
metadata:
  short-description: Durable Claude orchestration with hourly supervision
---

# Goal Loop

Keep one concrete goal moving through bounded Claude execution waves while Codex remains the supervisor, verifier, document owner, and final ship authority.

Use the installed `claude-team-bridge` skill for Claude execution. Goal Loop adds the durable goal lifecycle, fixed concurrency policy, checkpoint contract, and recurring Codex supervision; it does not replace or bypass the bridge.

## Defaults and hard limits

- Use exactly one Claude lead as the goal orchestrator.
- Give the lead three Claude teammates by default. Three is the hard maximum for concurrently active teammates.
- Check progress once per hour by default. The user may request a slower or faster interval.
- Do not count the lead against the three-teammate limit.
- Do not nest Claude teams. Teammates report to the lead and must not create subteams or recursively invoke Goal Loop.
- Keep all Claude work bounded by explicit paths, acceptance criteria, constraints, and verification commands.
- Drive the loop from exactly `ROADMAP.md`, `GOAL.md`, and `EVAL.md`. Claude receipts supplement these files; they do not replace them.

The durable identity is the stable `goal_id`, profile, checkpoints, receipts, and repository state. A crash or context limit may require a fresh Claude lead session. Never claim that one uninterrupted model context survived unless runtime evidence proves it.

## Start a loop

1. Establish the objective, acceptance criteria, workspace, allowed side effects, and stop conditions from the user's request and inspected repository state. Ask only for information that cannot be safely inferred.
2. Inspect the complete current Git status before delegation. Preserve unrelated user changes and use isolated worktrees or disjoint paths whenever concurrent writers are possible.
3. Read [references/document-control-plane.md](references/document-control-plane.md). Resolve or initialize exactly `ROADMAP.md`, `GOAL.md`, and `EVAL.md` without replacing human-authored content.
4. Read `ROADMAP.md`, then select the highest-priority ready roadmap item. Read `GOAL.md` and `EVAL.md` before deciding whether to dispatch, resume, verify, or stop.
5. Run `scripts/validate_goal_docs.py <workspace>` after the managed sections exist. Resolve every reported concurrency, identity, or terminal-dispatch evidence issue before launching more Claude work.
6. Load and follow `claude-team-bridge`. Verify relay health, exact workspace root, authentication boundary, and live native-team capability before submitting work.
7. Create a stable safe `goal_id` and a dedicated bridge profile. Keep durable bridge state outside the repository.
8. Read [references/orchestrator-contract.md](references/orchestrator-contract.md), then build a self-contained `team` packet for one lead and up to three teammates. Include bounded excerpts and hashes of the three control files. Submit it asynchronously so the job survives client disconnection.
9. Immediately write the returned orchestrator and teammate instance/job identifiers into the `GOAL.md` dispatch ledger. Every dispatch must reference one stable roadmap item ID and the current goal revision.
10. When the Codex automation tool is available, create one hourly heartbeat attached to the current task. Its prompt must inspect this exact `goal_id` and `job_id`, apply the supervision rules below, and avoid starting work when an active lease already exists. Prefer updating an existing matching heartbeat over creating a duplicate.
11. Return the `goal_id`, active `job_id`, workspace, roadmap item, teammate count, check-in interval, transport, and any degraded capability to the user.

Do not silently treat the CLI fallback as proof that one lead coordinated three native teammates. If native teams are unavailable, report the runtime limitation and ask before using a reduced single-worker or sequential fallback.

## Claude lead behavior

The lead owns decomposition and coordination inside the bounded goal contract. It should keep useful work moving, normally through successive waves of up to three teammates:

- Assign each teammate one concrete task with an explicit owner, path scope, expected output, and verification.
- Prefer disjoint write scopes. Use one teammate as a read-only verifier when independent review is valuable.
- Keep all three slots useful when the work genuinely supports parallelism; leave slots idle rather than inventing work.
- Reassign a slot only after the previous assignment has reached a terminal state and its evidence has been recorded.
- Integrate teammate results, run relevant checks, and emit a checkpoint before requesting another execution wave.
- Escalate ambiguity, missing authority, security-sensitive decisions, external credentials, and human taste decisions instead of guessing.

The lead and teammates must not commit, push, merge, deploy, purchase, publish, message third parties, change permissions, or perform other consequential external actions unless the user has separately authorized that action.

## Hourly supervision

At each check-in, Codex re-reads `ROADMAP.md`, `GOAL.md`, `EVAL.md`, the durable bridge receipt, and the worktree. It reconciles dispatch IDs and applies exactly one transition:

- `running` with a fresh heartbeat: inspect progress and do not launch a duplicate.
- `done` with unmet acceptance criteria: independently inspect the diff and evidence, save a bounded checkpoint, and start the next task with a new `task_id` under the same `goal_id`.
- `done` with all criteria proven: mark the goal complete and disable its heartbeat.
- `failed`, `interrupted`, or stale heartbeat: inspect the receipt and worktree, then perform at most one evidence-informed resume or replacement attempt.
- `blocked` or `waiting_user`: preserve state, stop automatic mutation, and ask for the missing decision or authority.
- Same blocker on three consecutive goal turns: stop the loop and report the concrete blocker.

A process heartbeat is only liveness evidence. Require checkpoints, diffs, command outputs, and test/runtime results before claiming progress or completion.

After each terminal Claude wave, Codex writes back in this order:

1. Update `EVAL.md` with observed evidence and Codex's verdict for each terminal dispatch.
2. Update `GOAL.md` with dispatch status, accepted/rejected outcomes, blockers, active lease, current checkpoint, and remaining criteria.
3. Update `ROADMAP.md` only when the evaluation evidence justifies changing an item to done, blocked, or ready.

Claude may propose document changes, but only Codex updates the managed control sections. Do not let Claude rewrite acceptance criteria, erase failed evaluations, or mark its own dispatch accepted.

## Concurrency and lease rules

- One active lead lease per `goal_id`.
- At most three concurrently active teammate assignments for that lead.
- Use an idempotency key derived from `goal_id` plus the scheduled time slot before launching a new wave.
- Never run concurrent writers against the same checkout or overlapping paths.
- A scheduler tick that finds a fresh active lease is a no-op, not another submission.
- After a daemon restart, classify previously running work as interrupted until the worktree and receipt are inspected.
- `GOAL.md` is the repository-visible dispatch ledger. Bridge state that cannot be reconciled to a ledger row is an orphan and must be inspected before further dispatch.

## Completion

The loop is complete only when the requested behavior exists, relevant verification passes, runtime behavior matches expectations where applicable, the complete diff has been reviewed, no known regression remains, and each acceptance criterion has evidence.

On completion, write the final evaluation first, reconcile all dispatch rows, mark the goal and roadmap item complete, disable the matching hourly heartbeat, leave durable receipts reviewable, and report what Claude performed separately from what Codex independently verified. Do not archive the task unless the user requested archival.
