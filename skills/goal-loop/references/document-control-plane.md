# Goal Loop document control plane

Read this reference when starting or supervising a Goal Loop. These files are repository-visible coordination state. Keep runtime receipts outside the repository, but link them by stable IDs.

## Authority and resolution

- `ROADMAP.md` owns sequence, priority, dependencies, and readiness.
- `GOAL.md` owns the current objective, remaining criteria, leases, checkpoints, and Claude dispatch ledger.
- `EVAL.md` owns acceptance tests, observed evidence, dispatch verdicts, and release blockers.
- Codex owns all three managed sections. Claude may recommend changes but cannot accept its own work or rewrite the contract.

The filenames are fixed: `ROADMAP.md`, `GOAL.md`, and `EVAL.md`. Do not use aliases. Create or update the Goal Loop managed section only in these three files.

For pre-existing documents, add or update only content between matching managed markers. Preserve all other text and unrelated changes.

```markdown
<!-- goal-loop:managed:start -->
...
<!-- goal-loop:managed:end -->
```

## ROADMAP.md managed section

Use stable item IDs. Codex chooses the highest-priority `ready` item whose dependencies are satisfied.

```markdown
<!-- goal-loop:managed:start -->
## Goal Loop Roadmap

| roadmap_id | outcome | dependencies | status | evidence_gate |
|---|---|---|---|---|
| R-001 | Observable outcome | none | ready | E-001 |
<!-- goal-loop:managed:end -->
```

Allowed statuses: `planned`, `ready`, `active`, `blocked`, `done`, `dropped`. Only Codex changes roadmap status, and `done` requires a passing evaluation gate.

## GOAL.md managed section

Increment `goal_revision` whenever Codex changes objective wording, acceptance criteria, roadmap binding, or authority. Ordinary heartbeat/status updates do not require a revision increase.

```markdown
<!-- goal-loop:managed:start -->
## Goal Loop Control

- goal_id: GL-product-quality
- goal_revision: 1
- status: running
- roadmap_path: ROADMAP.md
- roadmap_item_id: R-001
- eval_path: EVAL.md
- active_lease_until: 2026-01-01T01:00:00Z
- last_checkpoint: CP-001
- remaining_criteria: E-001

## Claude Dispatch Ledger

| dispatch_id | parent_id | role | instance_id | job_id | roadmap_id | scope | status | started_at | last_seen_at | checkpoint |
|---|---|---|---|---|---|---|---|---|---|---|
| GL-product-quality-O1 | codex | orchestrator | claude-instance-id | bridge-job-id | R-001 | coordinate R-001 | running | 2026-01-01T00:00:00Z | 2026-01-01T00:10:00Z | CP-001 |
| GL-product-quality-S1 | GL-product-quality-O1 | subagent | claude-agent-id | team-task-id | R-001 | bounded implementation | running | 2026-01-01T00:02:00Z | 2026-01-01T00:09:00Z | CP-001 |
<!-- goal-loop:managed:end -->
```

Use identifiers from the actual bridge receipt:

- Orchestrator: bridge `job_id` plus the native `team_name` or session identifier as `instance_id`.
- Subagent: `teammate_id` or `agent_id` from the spawn receipt as `instance_id`, plus its native task ID as `job_id`.
- If the runtime omits an identifier, record `unresolved` and stop further dispatch until Codex reconciles the native team/task artifacts. Never substitute a friendly member name as proof of identity.

Allowed dispatch statuses: `queued`, `running`, `verifying`, `waiting_user`, `blocked`, `failed`, `accepted`, `rejected`, `cancelled`, `interrupted`.

Exactly one orchestrator row may be active. At most three subagent rows may be active. Historical rows remain append-only except for status, heartbeat, and checkpoint updates.

## EVAL.md managed section

Evaluation is written before progress is accepted.

```markdown
<!-- goal-loop:managed:start -->
## Goal Loop Evaluation

| criterion_id | requirement | verifier | evidence_required | status |
|---|---|---|---|---|
| E-001 | Observable acceptance requirement | Codex | focused test and runtime output | unproven |

## Dispatch Evaluations

| dispatch_id | receipt | changed_paths | verification | codex_verdict | notes |
|---|---|---|---|---|---|
| GL-product-quality-S1 | bridge-job-id | src/example.py | pytest exit 0 | accepted | scope reviewed |
<!-- goal-loop:managed:end -->
```

Criterion statuses: `unproven`, `passing`, `failing`, `blocked`, `waived`. Only a user can authorize a waiver. Dispatch verdicts: `accepted`, `rejected`, `inconclusive`, `not-reviewed`.

Every terminal dispatch row in `GOAL.md` must have a corresponding dispatch-evaluation row. A receipt, process exit, or Claude self-report without independent inspection is `inconclusive`, not accepted.

## Codex control loop

1. Read all three documents and current Git/runtime state.
2. Validate the managed sections and reconcile dispatch IDs, native team/agent IDs, and task IDs with bridge jobs/team receipts.
3. Choose one ready roadmap item and update `GOAL.md` before dispatch.
4. Dispatch one Claude orchestrator with default three and maximum three active subagents.
5. Record returned identities immediately in `GOAL.md`.
6. On each check-in, update liveness and checkpoints without creating duplicate work.
7. On terminal work, independently inspect scope, diff, tests, runtime, and logs.
8. Write `EVAL.md`, then `GOAL.md`, then `ROADMAP.md`.
9. Continue only if acceptance remains unproven and a safe ready action exists.
