# Agent Relay

Formerly Local Code Delegate. The `LCD_*` environment variables and `lcd`
command remain compatibility interfaces for existing evaluations; new project
documentation should use Agent Relay and `agent-relay`.

## Unified subagent expansion

The repository's unified subagent surface now has four explicit lanes:

| Lane | Role | Default target | Authority boundary |
|---|---|---|---|
| `local-qwen` | local/free mechanical worker | Qwen3.5:4B | bounded disposable sandbox and parent verification |
| `claude-task` | primary Claude implementation/team worker | host policy | authenticated task bridge and Git gates |
| `codex-review` | subscription QA verifier | GPT-5.6 Sol, high | read-only Codex CLI review receipt |
| `agy-antigravity` | Google-stack scout/planner | Gemini 3.1 Pro, high | plan consultation; parent owns edits and validation |

The evidence-backed role matrix and local receipts are maintained in
`docs/SUBAGENT_ROLES.md`. Current readiness is lane-specific: Qwen passed its
exact-model smoke; Claude authentication and a live CLI probe passed while its
native Agent Teams agent type is unavailable; Codex review is blocked by the
installed CLI/model compatibility; and AGY is blocked by a denied permission
prompt. These are explicit runtime boundaries, not reasons to silently fall
back to another lane.

## Agent-harness routing boundary

Most model routers operate at the LLM layer: they relay a prompt, completion,
or API request to a selected model. Agent Relay operates one level higher. It
routes complete agent harnesses, including their tools, permissions,
workspace, sandbox, task contract, execution policy, and verification path.

The central design claim is therefore **agent-harness-level relay/routing**,
not merely model selection. Codex, Claude, local Qwen, and Antigravity are
different execution environments with different authority and proof
boundaries; the router must select and supervise the runtime, not only forward
text to an LLM.

## 1. Mission

Build a lightweight, single-machine delegation layer that allows Codex or
another frontier coding agent to offload well-bounded, low-level coding tasks
to an inexpensive local model.

The frontier agent owns:

- the user's goal and repository-level reasoning
- architecture and task decomposition
- risk assessment and delegation decisions
- the exact task contract and acceptance criteria
- review, repair, and integration decisions

The local model is an implementation worker, not an autonomous architect. The
project exists to maximize verified local engineering work per Codex token
spent.

Detailed measurement rules live in EVALS.md.

The primary reusable deliverable is the `agent-relay` skill in
`skills/agent-relay/`. Its Qwen worker reference teaches the parent Codex how to decide what to
delegate, construct a minimal task contract, invoke a second Codex CLI process
backed by Qwen through Ollama, and review a compact verified handoff. Its prompt
kit is part of the implementation, not an informal example. The skill must
preserve the same sandbox, scope, verification, retry, and economics invariants
as the Python runner.

## 2. MVP Decision: Ollama plus three harness lanes

Ollama is the mandatory MVP inference backend.

The mandatory target model for all new runs is Ollama's exact tag
`qwen3.5:4b`. Existing `qwen3:4b` artifacts are historical evidence only and
must not be presented as Qwen3.5 results. The selected Ollama host must have
the exact tag installed before a smoke or evaluation run.

The first experiment is deliberately concrete:

> Can Codex delegate a tightly bounded coding task to a small Ollama-hosted
> model, apply the resulting patch in an isolated sandbox, verify it, and
> return concise evidence?

The local model still comes from Ollama. The target comparison matrix has
three execution harnesses over that same exact Qwen tag:

| Harness | CLI lane | Local-provider boundary | Default tool posture |
|---|---|---|---|
| Claude Code | `claude-ollama` | loopback Anthropic Messages adapter to Ollama | tools disabled; outer verifier applies the candidate |
| Codex CLI | `codex-ollama` | temporary Codex provider and loopback Ollama adapter | compatibility proxy strips unnecessary tools/reasoning by default |
| DeepSeek Harness | `deepseek-ollama` | official `dsh --profile headless` with `llm-pi-ai` OpenAI-compatible route | tools run only inside a disposable inner sandbox |

Direct `ollama` remains the simple local baseline and `fixture` remains a
deterministic control. The implementation work required to expose the three
new lanes is still a future milestone; this goal defines the contract they
must meet. All four non-control execution lanes must share the task contract, outer
sandbox, scope check, verification, retry limit, and compact proof packet. A
harness being installed is not evidence that it successfully used Qwen; each
lane must pass a disposable capability smoke and record the exact
model/provider path.

The 8 GB operating profile is deliberately conservative: `qwen3.5:4b`, an
8,192-token Ollama context, thinking disabled for bounded mechanical work,
one active generation at a time, a 10-minute keep-alive, and no implicit model
pulls. Increase context, output, concurrency, or reasoning only as an
explicit measured eval variant. The objective is to reduce frontier tokens
without trading away verified quality, useful patches, or wall-clock speed.

The implementation must support:

~~~text
OLLAMA_HOST=http://localhost:11434
LOCAL_MODEL=qwen3.5:4b
OLLAMA_API_KEY=<optional key for an authenticated Ollama-compatible gateway>
OLLAMA_SEED=<optional seed for reproducible evals>
LCD_CODEX_OLLAMA_HOST=<Ollama host used by Codex CLI local mode>
LCD_CODEX_MODEL=qwen3.5:4b (already installed at that host)
LCD_CODEX_LOCAL_PROVIDER=ollama-chat
LCD_CODEX_PROVIDER_ID=lcd-ollama
LCD_CODEX_WIRE_API=responses (current Codex CLI; use chat only for legacy Codex)
LCD_CODEX_SANDBOX=danger-full-access (only inside the disposable Git worktree)
LCD_CODEX_REASONING_EFFORT=low|medium|high
LCD_CODEX_TIMEOUT_SECONDS=180 (raise for cold local-model startup)
LCD_CODEX_IDLE_TIMEOUT_SECONDS=90 (fail a silent lane, count proxy progress)
LCD_CODEX_COMPAT_PROXY=true (temporary loopback custom-provider adapter)
LCD_CODEX_DISABLE_REASONING=true (small-model compatibility mode)
LCD_CODEX_STRIP_TOOLS=true (small-model compatibility mode)
LCD_CODEX_COMPACT_PROMPT=true (compact redundant provider context)
LCD_CODEX_NUM_CTX=8192 (bounded default; override only after smoke)
LCD_CODEX_NUM_PREDICT=<optional provider output bound>
LCD_CODEX_TEMPERATURE=0 (deterministic bounded-eval default)
LCD_CODEX_SEED=<optional fixed seed, e.g. 17, for reproducible evals>
LCD_CODEX_OUTPUT_SCHEMA=false (experimental; provider compatibility required)
LCD_CODEX_RETRY_MODEL=qwen3.5:4b (optional explicit same-model retry)
OLLAMA_NUM_CTX=8192
OLLAMA_KEEP_ALIVE=10m
LCD_CLAUDE_BIN=claude.cmd (optional executable override)
LCD_CLAUDE_OLLAMA_HOST=http://localhost:11434
LCD_CLAUDE_MODEL=qwen3.5:4b
LCD_CLAUDE_EFFORT=low
LCD_DEEPSEEK_BIN=dsh.cmd (official DeepSeek Harness CLI)
LCD_DEEPSEEK_OLLAMA_HOST=http://localhost:11434
LCD_DEEPSEEK_MODEL=qwen3.5:4b
LCD_DEEPSEEK_NUM_CTX=8192
~~~

The planned harness seam is intentionally narrow rather than a generalized
provider framework. Claude Code and DeepSeek Harness should be explicit
adapters with availability records; missing executables, unreachable Ollama,
missing exact models, and startup failures must be reported as infrastructure
unavailability, not counted as model-quality failures and never trigger an
automatic fallback.

The first CLI surface is:

~~~text
lcd doctor
lcd doctor --codex-smoke --model <model>
lcd triage --task task.json --avoided-tokens <n> --spent-tokens <n>
lcd delegate --require-triage --task task.json --repo <path> \
  --avoided-tokens <n> --spent-tokens <n>
lcd batch --require-triage --manifest batch.json --repo <path> \
  --avoided-tokens <n> --spent-tokens <n>
lcd eval --backend ollama --model <model> --suite bounded-basic
lcd delegate --backend codex-ollama --require-triage --task task.json --repo <path>
lcd eval --backend codex-ollama --model <model> --suite bounded-basic
# Target CLI surface after the three adapter milestones:
lcd delegate --backend claude-ollama --require-triage --task task.json --repo <path>
lcd eval --backend claude-ollama --model <model> --suite bounded-basic
lcd delegate --backend deepseek-ollama --require-triage --task task.json --repo <path>
lcd eval --backend deepseek-ollama --model <model> --suite bounded-basic
~~~

## 3. Milestone 1

Codex can call delegate_local, which:

1. classifies the task with a deterministic delegate-or-keep-local gate
2. validates a structured task contract
3. gathers only bounded context
4. calls Ollama through the selected local inference path
5. receives a reviewable patch or bounded complete-file candidate
6. rejects paths outside allowed_files
7. applies the patch only in an isolated Git sandbox
8. runs the declared verification commands
9. retries at most once with failure evidence
10. returns a concise structured result
11. leaves the caller's main worktree unchanged
12. fails silent/overlong harness/Ollama lanes without leaking child processes
13. records proxy normalization, prompt-compaction, and transport-progress evidence

Milestone evidence must include the request model, response status, patch,
changed files, verification commands and exit codes, final result status, and
sandbox/main-worktree state.

## 4. Core principle

> Use frontier intelligence for decisions. Use local compute for mechanical
> execution.

Delegate only when the work is explicit, local in scope, low ambiguity, low
architectural impact, reversible, and mechanically verifiable.

The parent must use the triage gate before invoking the worker. A candidate is
eligible only when it has an explicit low-risk `task_kind`, one to three allowed
write files, deterministic verification, no risk flags or high-risk language,
and expected Codex-token leverage of at least 2x, including the parent triage
decision cost. Unknown or incomplete
contracts stay in the parent until they are made explicit; safe-looking work
without priced review/recovery cost is not assumed to save tokens.

For every future harness backend, a direct Ollama smoke is not sufficient
runtime evidence. The parent should require the matching disposable capability smoke
(`lcd doctor --codex-smoke`, `lcd doctor --claude-smoke`, or
`lcd doctor --deepseek-smoke`) before starting a long run. A failed or unknown
lane-health probe means the harness is unavailable; it does not justify a
silent fallback or bypassing triage.

Do not delegate architectural decisions, security-sensitive reasoning,
unclear product requirements, difficult debugging, broad cross-cutting work,
or judgment that cannot be verified.

Use deterministic tools before any LLM when a formatter, AST transformation,
symbol rename, template, schema compiler, or dependency tool can safely solve
the task.

## 5. Workflow boundary

~~~text
USER GOAL
  |
  v
FRONTIER AGENT
  | understands the repository
  | designs the solution
  | defines a bounded task
  | sets requirements and verification
  | records the triage decision and expected token economics
  v
LOCAL CODE DELEGATE
  | builds minimal context
  | calls Ollama directly or through the selected execution harness
  | validates and applies a patch in a sandbox
  | runs deterministic verification
  | reports evidence
  v
FRONTIER REVIEW
  | accept
  | reject
  | repair
  | integrate
~~~

The frontier agent must never trust a local model blindly. A worker report is
evidence to inspect, not proof of correctness.

## 6. Task contract

Each task must contain:

~~~yaml
task_id:
task_kind: mechanical | test_generation | repetitive | bounded_bugfix | documentation
risk_flags: []
objective:
allowed_files:
context:
context_mode: replace | insert_after  # optional bounded range edit mode
requirements:
constraints:
verification:
success_criteria:
~~~

The minimum Python interface is:

~~~python
result = delegate_local(
    model="qwen3.5:4b",
    task_kind="bounded_bugfix",
    risk_flags=[],
    objective="Add validation for negative timeout values",
    allowed_files=["src/config.py", "tests/test_config.py"],
    requirements=["Negative values raise ValueError", "Zero remains valid"],
    constraints=["Do not change the public API"],
    verification=["pytest tests/test_config.py"],
)
~~~

The worker must:

- perform only the assigned task
- stay within allowed_files
- prefer the smallest valid diff
- return BLOCKED when the request is ambiguous
- report unrelated failures instead of fixing them
- never claim verification it did not run
- return structured output and a patch
- never run an unlimited retry loop

`context` is read-only evidence and may include tests or neighboring files
outside `allowed_files`. The supervisor enforces `allowed_files` as the only
write scope.

## 7. Initial architecture

~~~text
agent-relay/
├── GOAL.md
├── EVALS.md
├── README.md
├── pyproject.toml
├── src/
│   └── agent_relay/          # canonical Python module
│       ├── __init__.py
│       ├── cli.py
│       ├── task.py
│       ├── result.py
│       ├── worker.py
│       ├── ollama.py
│       ├── patch.py
│       ├── sandbox.py
│       ├── verifier.py
│       └── delegate.py
├── evals/
│   ├── cases/
│   ├── fixtures/
│   └── runner.py
└── tests/
~~~

The planned execution-harness additions are a narrow lane-selection and
availability seam, a Claude Code loopback protocol adapter, the existing
`codex_worker.py` Codex CLI path, and a DeepSeek Harness adapter. `batch.py`
and `frontier.py` must retain the same compact-handoff accounting. They must
keep the task contract, sandbox, verifier, result schema, and accounting
boundary shared with direct Ollama execution.

The first implementation is standard-library Python wherever practical. It
must be easy to run on one Windows machine and easy to test with a local
Ollama-compatible HTTP server.

## 8. Implementation order

~~~text
1. Ollama integration and doctor/smoke path
2. Task contract and result schema
3. delegate_local() primitive
4. Isolated Git worktree/copy sandbox
5. Deterministic patch and verification gates
6. Concise patch/result reporting
7. Shared harness selection and availability records
8. Codex CLI provider, loopback compatibility, and small-model prompt lane
9. Claude Code Anthropic-to-Ollama adapter and no-tools smoke
10. DeepSeek Harness headless profile, OpenAI-compatible route, and inner sandbox
11. Capability smoke and fail-fast lane health for all three harnesses
12. Codex Qwen delegation skill and prompt kit
13. Deterministic parent triage and fail-closed routing
14. Compact frontier handoff and triage-aware independent-task batching
15. First 10 bounded-basic eval cases
16. Matched three-harness Qwen evals with direct Ollama and fixture control
17. Measure frontier token, local throughput, quality, GPU-fit, and review effort
18. Reprice recorded matched runs under explicit frontier manifest assumptions
19. Expand to the 50-task benchmark and tune routing from measured results
~~~

The first implementation must not add autonomous planning, distributed
scheduling, learned routing, or unlimited recursive loops.

## 9. Safety invariants

- The main worktree is never modified by a delegated task.
- Patch paths are checked before application and after execution.
- Verification is determined by the harness, not by the model.
- At most one local retry is allowed.
- Every command result includes exit code, output, error output, and duration.
- Every result has an explicit status:
  SUCCESS, FAILED_VERIFICATION, SCOPE_VIOLATION, BLOCKED, WORKER_ERROR, or
  TIMEOUT.
- The parent agent receives a bounded summary, patch, changed files, and
  verification evidence, not hidden model reasoning.
- Credentials are read only from the explicit Ollama API-key setting and are
  never written to task records or prompts.
- Verification commands are trusted contract inputs; the disposable sandbox
  isolates ordinary repository changes but is not an OS-level command sandbox.

## 10. Success criteria

The MVP must pass the gates defined in EVALS.md on a representative benchmark
of at least 50 tasks:

- at least 80% bounded acceptance after at most one retry for each available
  harness, with no quality denominator contamination from unavailable lanes
- at least 50% net Codex token reduction against a matched Codex-only baseline
- less than 1% scope violations, ideally zero
- less than 15% substantial Codex repair
- at least 85% verification pass rate after at most one retry
- wall-clock completion no more than 25% slower than Codex-only
- correct BLOCKED behavior for unsuitable tasks

The current authoritative Qwen3.5:4B cohort satisfies these gates: 45/45
bounded acceptance, 45/45 verification, 0 scope violations, 2.22% measured
frontier repair, 97.39% measured net Codex-token reduction, and 37.31x
Frontier Token Leverage. The complete evidence and cohort-specific caveats are
recorded in EVALS.md section 14.4.

The north-star is:

> **Maximum verified local engineering work performed per Codex token spent.**

A local model benchmark score or successful HTTP request alone is not a
project success measure. Each selected lane must also preserve verification,
scope discipline, usefulness, availability classification, and the 8 GB
speed/fit target.

## 11. Deferred expansion

The three requested Ollama-backed harnesses are the bounded comparison scope.
Do not add llama.cpp, vLLM, distributed scheduling, or additional agent
frameworks until the three lanes have matched quality/speed/economics evidence.
Any future adapter must preserve the task contract, sandbox, verifier, patch
policy, availability records, and result schema.

## 12. Non-goals

This project is not:

- a replacement for Codex
- an autonomous software company
- a general multi-agent framework
- a distributed scheduler
- an AI project manager or IDE
- a generic chatbot
- an architecture agent
- an unlimited recursive agent loop

<!-- goal-loop:managed:start -->
## Goal Loop Control

- goal_id: GL-agy-cli-delegation
- goal_revision: 1
- status: verifying
- roadmap_path: ROADMAP.md
- roadmap_item_id: R-AGY-CLI-001
- eval_path: EVAL.md
- active_lease_until: 2026-08-22T03:03:20Z
- last_checkpoint: CP-001
- remaining_criteria: E-AGY-001

## Claude Dispatch Ledger

| dispatch_id | parent_id | role | instance_id | job_id | roadmap_id | scope | status | started_at | last_seen_at | checkpoint |
|---|---|---|---|---|---|---|---|---|---|---|
| GL-agy-cli-delegation-O1 | codex | orchestrator | a2a-GL-agy-cli-delegation-O1-42882c0d | GL-agy-cli-delegation-O1-42882c0d6f36 | R-AGY-CLI-001 | coordinate standalone AGY CLI adapter, tests, docs, and verification | failed | 2026-08-22T01:46:11Z | 2026-08-22T01:46:11Z | CP-000 |
| GL-agy-cli-delegation-O2 | codex | orchestrator | a2a-GL-agy-cli-delegation-O2-24fafbcb | GL-agy-cli-delegation-O2-24fafbcbc82a | R-AGY-CLI-001 | coordinate standalone AGY CLI adapter, tests, docs, and verification with configured worker/verifier types | failed | 2026-08-22T01:48:21Z | 2026-08-22T01:48:21Z | CP-000 |
| GL-agy-cli-delegation-O3 | codex | orchestrator | a2a-GL-agy-cli-delegation-O3-2e1512ba | GL-agy-cli-delegation-O3-2e1512ba2294 | R-AGY-CLI-001 | coordinate standalone AGY CLI adapter, tests, docs, project agent definitions, and verification | blocked | 2026-08-22T01:50:47Z | 2026-08-22T01:50:47Z | CP-000 |
<!-- goal-loop:managed:end -->
