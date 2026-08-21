# Goal Loop orchestrator contract

Read this reference when starting a new Goal Loop or constructing a continuation wave.

## Durable goal fields

Record these fields in bridge state or another reviewable state store outside the repository:

```yaml
goal_id: safe-stable-identifier
goal_revision: monotonically-increasing-integer
objective: concrete requested outcome
workspace_root: exact allowlisted repository root
roadmap_path: ROADMAP.md
roadmap_item_id: R-001
goal_path: GOAL.md
eval_path: EVAL.md
profile: goal-loop-<goal_id>
status: queued | running | verifying | waiting_user | blocked | failed | complete
acceptance_criteria: []
constraints: []
authorized_side_effects: []
active_job_id: null
active_lease_until: null
last_heartbeat_at: null
last_checkpoint: null
consecutive_same_blocker_count: 0
check_interval_seconds: 3600
max_teammates: 3
```

## Lead prompt requirements

The Claude lead prompt must state:

- You are the sole orchestrator for this `goal_id`.
- You may coordinate at most three concurrently active Claude teammates.
- Teammates may not create teams or recursively invoke the goal loop.
- Decompose the next bounded execution wave and assign explicit, non-overlapping ownership.
- Inspect actual repository and runtime evidence before deciding what to do.
- Do not infer authority for consequential side effects.
- Do not claim success from teammate self-reports; inspect their artifacts and verification.
- Before ending the wave, return the checkpoint schema below.
- Treat the supplied control-file excerpts as a versioned contract. Propose updates in the checkpoint; do not directly edit Codex-managed control sections.

Choose teammate objectives from the actual work. A useful default allocation is:

1. `worker-a`: highest-value bounded implementation slice.
2. `worker-b`: independent second slice, tests, or investigation with a disjoint write scope.
3. `verifier`: read-only review of the integrated result and evidence.

Do not force this allocation when the goal is documentation-only, research-only, sequential, or too coupled for safe parallelism.

## Checkpoint schema

Every execution wave must return a compact structured checkpoint:

```json
{
  "goal_id": "...",
  "goal_revision": 1,
  "roadmap_item_id": "R-001",
  "dispatch_id": "GL-...-O1",
  "job_id": "...",
  "status": "running|verifying|waiting_user|blocked|failed|complete",
  "completed": ["observable outcomes only"],
  "changed_paths": ["repository-relative paths"],
  "verification": [
    {"command": "...", "exit_code": 0, "result": "bounded factual summary"}
  ],
  "active_assignments": [
    {"agent": "...", "objective": "...", "paths": ["..."], "status": "..."}
  ],
  "next_actions": ["bounded next work"],
  "blockers": ["concrete blocker or missing authority"],
  "acceptance_remaining": ["unproven criterion"],
  "transport": "native-mcp|cli-fallback|unavailable"
}
```

Reports without concrete artifacts or command evidence are not completion proof.

## Hourly heartbeat prompt shape

Use a cohesive prompt similar to:

```text
Use $goal-loop to supervise goal <goal_id> in <workspace>. Re-read ROADMAP.md,
GOAL.md, and EVAL.md; inspect the exact active job, lease, heartbeat, checkpoint,
worktree, and receipt; and reconcile every active dispatch ID. Do not launch another
Claude run while a fresh lease is active. If the prior wave is terminal, independently
review its evidence, update evaluation then goal then roadmap, and either mark the goal
complete, preserve a waiting/blocked state, or submit one bounded continuation under
the same goal with no more than three Claude teammates. Report only meaningful state
changes or failures.
```

Keep notification preferences in the automation configuration rather than in the prompt.
