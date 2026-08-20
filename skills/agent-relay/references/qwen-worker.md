---
name: agent-relay-qwen-worker
description: Delegate bounded, low-level coding work from Codex to a second Codex CLI execution harness backed by an Ollama-hosted Qwen model. Use when Codex should offload mechanical, explicitly verifiable edits, construct delegation contracts, run lcd delegate or lcd batch, write delegation prompts, or reduce frontier-token usage without weakening sandbox, scope, or test verification.
---

# Agent Relay Qwen Worker Reference

Use Codex as the supervisor and Qwen as the bounded implementation worker. Keep
architecture, decomposition, acceptance criteria, review, repair, and integration
decisions in the parent Codex context. Let the child Codex CLI process inspect and
edit only a disposable sandbox, then let Agent Relay apply independent
scope and verification gates.

The default model target for all new runs is the exact Ollama tag
`qwen3.5:4b`. Do not call historical `qwen3:4b` results Qwen3.5 evidence; run
the exact-model direct and Codex smoke probes before benchmarking.

The skill is optimized for high verified work per frontier token, not for making
the local model autonomous. The child result is evidence; the harness and parent
Codex decide whether it is acceptable.

## Triage before delegation

Make the parent Codex decide whether a task should leave the frontier context
before building a worker prompt. Use the deterministic triage command as the
default gate:

```powershell
lcd triage --task .\task.json `
  --avoided-tokens 1800 --spent-tokens 600
```

Only delegate when the result is `DELEGATE` with `HIGH` confidence. The gate
requires all of these conditions:

- `task_kind` is one of `mechanical`, `test_generation`, `repetitive`,
  `bounded_bugfix`, or `documentation`;
- the task has one cohesive outcome and a finite, complete set of one to three
  explicit write files (never a directory, glob, or repository-wide scope);
- the parent has enumerated the complete task-specific verification manifest,
  including every check already known to be required, with at least one
  deterministic verification command declared;
- `risk_flags` is empty and the task text does not imply architecture,
  security, credentials, migrations, production work, destructive changes,
  broad refactoring, performance judgment, or ambiguity;
- the expected Codex tokens avoided are at least twice the expected frontier
  tokens spent on triage, decomposition, handoff, review, retry, and recovery.

`KEEP_LOCAL` is the correct result for work that is valid but needs frontier
judgment. `BLOCKED` means the contract is incomplete, usually because the task
kind, finite write scope, complete verification manifest, or deterministic
verification is missing. Do not bypass a
`KEEP_LOCAL` or `BLOCKED` result by asking the local model to decide whether the
task is safe. Split the task or finish the parent reasoning first.

Use this routing rule as a hard decision procedure:

| Parent observation | Route |
| --- | --- |
| A formatter, AST transform, rename tool, template, or schema compiler can do the work deterministically | Use that tool; do not spend model tokens |
| The task has an explicit low-risk kind, one outcome, a finite complete set of one to three write files, bounded context, a complete verification manifest with deterministic verification, no risk signal, and leverage at least 2x | `DELEGATE` |
| The task is understandable but requires architecture, product judgment, security, migration, performance judgment, broad debugging, or more than three files | `KEEP_LOCAL`, or split it into smaller contracts first |
| Any required field, acceptance test, scope boundary, verification manifest, economics estimate, or selected-model lane-health result is unknown | `BLOCKED` or `KEEP_LOCAL` until the parent resolves it |

Do not delegate merely because a change looks easy. The parent must know what a
correct patch looks like before the worker starts. If the task is too broad,
first decompose it into independent contracts; never ask Qwen to discover the
decomposition. If the task is eligible but the local lane is unhealthy, keep the
task in Codex or use a separately smoke-tested direct-Ollama lane. A passing
triage result is permission to attempt bounded execution, not permission to
accept the output without review.

## Delegate only bounded work

Delegate when the task is:

- one cohesive mechanical change;
- limited to a finite, explicit `allowed_files` set of one to three files;
- expressible with concrete requirements and a complete verification manifest
  containing deterministic verification commands;
- reversible and low in architectural or security risk.

Keep architecture, ambiguous requirements, broad refactors, difficult debugging,
security decisions, credentials, migrations, and untestable judgment in the parent
Codex. Split independent edits into separate tasks or a batch manifest; do not give
one child a broad project goal.

## Configure the local execution lane

Run the project environment's `lcd doctor` first. The exact model must already be
installed at the configured Ollama host; the harness rejects implicit pulls.

```powershell
$env:LCD_CODEX_OLLAMA_HOST = "http://localhost:11434"
$env:LCD_CODEX_LOCAL_PROVIDER = "ollama-chat"
$env:LCD_CODEX_PROVIDER_ID = "lcd-ollama"
$env:LCD_CODEX_WIRE_API = "responses"
$env:LCD_CODEX_SANDBOX = "danger-full-access"
$env:LCD_CODEX_MODEL = "qwen3.5:4b"
$env:LCD_CODEX_REASONING_EFFORT = "low"
$env:LCD_CODEX_TIMEOUT_SECONDS = "180"
$env:LCD_CODEX_IDLE_TIMEOUT_SECONDS = "90"
$env:LCD_CODEX_COMPAT_PROXY = "true"
$env:LCD_CODEX_DISABLE_REASONING = "true"
$env:LCD_CODEX_STRIP_TOOLS = "true"
$env:LCD_CODEX_COMPACT_PROMPT = "true"
$env:LCD_CODEX_OUTPUT_SCHEMA = "false"
# Keep bounded tasks at an 8192-token provider context. Override only after
# the exact Ollama-compatible lane has passed a live smoke with that setting.
$env:LCD_CODEX_NUM_CTX = "8192"
# Keep sampling deterministic when collecting comparable evals.
$env:LCD_CODEX_TEMPERATURE = "0"
# Optional fixed seed for reproducible benchmark cohorts.
# $env:LCD_CODEX_SEED = "17"
# $env:LCD_CODEX_NUM_PREDICT = "2048"
# Optional: explicitly pin the one bounded retry to the same target model:
$env:LCD_CODEX_RETRY_MODEL = "qwen3.5:4b"
lcd doctor --smoke --model qwen3.5:4b
lcd doctor --codex-smoke --model qwen3.5:4b
```

The harness also defaults to `low` reasoning when the environment variable is
omitted. Raise it only for a deliberately harder bounded task; do not make the
normal delegation lane pay that cost.

The Codex-backed lane defaults to an 8192-token Ollama context so a model with
an advertised 262k context does not consume excessive local memory for a small
task. Treat this as a runtime reliability bound, not as measured Codex token
savings.

`lcd doctor --smoke` proves only that the Ollama HTTP API can answer. Before a
Codex-backed batch or a new model/provider combination, require
`lcd doctor --codex-smoke` to return `ok: true`. That probe runs one mechanical
edit in a temporary Git repository through the complete Codex CLI, Ollama,
sandbox, patch, and verification path, with the same one-retry recovery contract
used by normal bounded delegation. Its caps are 120 seconds total and 90 seconds
without useful provider/process progress. It never touches the caller's
repository. If it fails, treat the Codex execution lane as unhealthy: route the
bounded task through direct `--backend ollama` when appropriate, or keep it in
the parent. Do not spend a full task timeout or bypass triage with
`--allow-untriaged` to work around a failed capability probe.

The default `lcd-ollama` lane uses the current Codex Responses wire API, which
Ollama serves directly. The loopback proxy remains available for legacy
Chat-Completions Codex releases; only that `chat` lane can claim provider-side
tool stripping, reasoning disabling, and prompt compaction. This is still Codex
CLI as the execution harness, but it is not evidence that Qwen executed Codex
tools. The result will normally be a bounded `reported_patch` or
`reported_files` candidate that the outer sandbox applies and verifies. Set
`LCD_CODEX_STRIP_TOOLS=false` only after the selected model has passed its own
live smoke with tools enabled.

`LCD_CODEX_LOCAL_PROVIDER=ollama-chat` is retained as a compatibility label in
preflight telemetry; the actual temporary Codex provider is `lcd-ollama` with
the configured `LCD_CODEX_WIRE_API`.

Keep native output-schema mode off on runtimes that return an empty final
message; the normal prompt/parser path remains the stable contract. Do not count
Qwen/Ollama token counters as Codex savings. A longer timeout accommodates cold
Qwen model startup; it is a reliability setting, not evidence of token savings.
The idle timeout is a separate no-progress watchdog: it fails a model that emits
only initial Codex events without producing output or tool progress, so a slow or
stalled model cannot consume the entire task budget repeatedly.
Proxy bytes,
provider rewrites, prompt compaction, tool stripping, result source, and each
attempt are runtime evidence; they are useful for diagnosing the lane but are
not themselves correctness or token-savings proof.

## Build a small task contract

Before delegating, inspect only the target definition and the verification test.
Put read-only evidence in `context`; it does not expand the write scope. Avoid
whole-repository context and do not repeat the same source in objective,
requirements, and context.

```json
{
  "task_id": "config-negative-timeout",
  "task_kind": "bounded_bugfix",
  "risk_flags": [],
  "objective": "Reject negative timeout values while preserving zero and positive values.",
  "allowed_files": ["src/config.py", "tests/test_config.py"],
  "context": ["src/config.py", "tests/test_config.py"],
  "requirements": [
    "Negative values raise ValueError.",
    "Zero remains valid.",
    "Preserve the public API."
  ],
  "constraints": [
    "Make the smallest valid change.",
    "Do not touch files outside allowed_files."
  ],
  "verification": ["pytest tests/test_config.py"],
  "success_criteria": [
    "The declared verification command exits 0.",
    "Only allowed files change."
  ],
  "retry_limit": 1
}
```

For a ranged Python edit, provide the exact target definition and use the
contract's ranged context mode when appropriate. The harness may provide the
complete allowed file as read-only context while preserving the declared range
as the write boundary. Require a unified diff or an exact replacement snippet,
never a guessed line-number patch. For `insert_after`, return only the new test
definition. Read
`references/prompts.md` when authoring or changing prompt text.

## Execute with the Codex harness

Use a compact handoff and keep the full patch in an artifact:

```powershell
$artifact = Join-Path $env:TEMP "lcd-task.patch"
lcd delegate --backend codex-ollama --model qwen3.5:4b `
  --task .\task.json --repo . --compact --patch-out $artifact `
  --require-triage --avoided-tokens 1800 --spent-tokens 600
```

For independent tasks, use one batch manifest so the parent receives one proof
packet while every task still receives its own sandbox, scope check, and
verification:

```powershell
lcd batch --manifest .\batch.json --repo . --model qwen3.5:4b `
  --aggregate --sample 3 --manifest-mode thin `
  --require-triage `
  --artifact-dir (Join-Path $env:TEMP "lcd-batch-artifacts")
```

With `--require-triage`, each manifest entry must provide its own economics or
inherit the global `--avoided-tokens` and `--spent-tokens` values. Put per-task
values beside the task contract:

```json
{
  "task": { "task_id": "one", "task_kind": "mechanical" },
  "triage": { "avoided_tokens": 1800, "spent_tokens": 600 }
}
```

The batch runner makes this parent decision before constructing or invoking a
worker. A rejected task is recorded as `KEEP_LOCAL` or `BLOCKED`, gets no model
invocation, and only counts as a passing batch case when the manifest explicitly
declares that refusal as its expected status. Batching is a token-saving handoff
mechanism, not a routing bypass.

The runner's outer verifier is authoritative. It applies patches only in the
disposable sandbox, checks changed paths, runs the declared commands, and keeps
the caller's main worktree unchanged.

The CLI Codex lane fails closed on missing triage even when the flag is omitted;
use `--allow-untriaged` only for a deliberately diagnostic run. A normal
delegation must carry the parent `DELEGATE` decision and token estimate into the
worker call.

When the child edits its disposable sandbox, `inner_sandbox_diff` is authoritative
even if the final message redundantly echoes a patch or file map. If the child
cannot use tools, the bounded reported patch/file fallback remains supported. In
that fallback, `READY` requires a non-empty candidate; an empty summary with
empty `patch` and `files` is a failed attempt, not a successful no-op.

## Review the proof, not the prose

Accept a result only when all of these are true:

- status is `SUCCESS`;
- every changed path is in `allowed_files`;
- every declared verification command passed;
- the patch artifact/hash exists and is reviewable;
- no main-worktree mutation or unexpected model pull occurred.

Treat `WORKER_ERROR`, `FAILED_VERIFICATION`, `SCOPE_VIOLATION`, and `TIMEOUT` as
failed delegation. `BLOCKED` is correct only when the task was genuinely
unsuitable and no edit was made. Read the patch artifact for accepted changes;
do not ask the child to restate a large patch in the frontier conversation.

Record the runtime lane separately from the patch verdict. At minimum inspect
`metadata.attempt_history`, `result_source`, `codex_provider_id`,
`codex_wire_api`, `codex_tools_stripped`, `codex_prompt_compacted`, proxy
request/forwarded-byte counts, and main-worktree status. `reported_files` and
`reported_patch` prove only that a candidate was returned; the outer sandbox
diff, scope check, and verifier decide whether it is acceptable. A zero tool
execution share is expected in the default small-Qwen compatibility lane, not a
failure, as long as the returned candidate passes the same outer gates.

Allow at most one retry. The retry must receive the first attempt's compact
failure and verification evidence, use a clean sandbox, and make the smallest
repair. Retry only a concrete recoverable candidate or deterministic verifier
failure, such as malformed output, a summary-only response, or a failing test
whose repair remains inside the original scope. Escalate to
`LCD_CODEX_RETRY_MODEL` only for a useful, bounded retry. Do not retry malformed
setup, missing-model, provider-pull, scope, ambiguous-contract, or no-progress
timeout failures blindly; repair the lane or take the task back. After one
unsuccessful retry, take the task back into the parent Codex context.

## Token-efficiency rules

1. Send one compact contract and only the context needed to implement it.
2. Prefer one batch handoff for independent tasks.
3. Return status, changed files, verification, hashes, and failure tails; omit
   raw child transcripts and patch text from the normal handoff.
4. Review all failures and a declared deterministic sample of passing artifacts.
5. Charge task decomposition, manifest input, review, repair, and recovery in
   the economics ledger. Use `--manifest-mode none` only when the task
   definitions were already known to the frontier; never omit input just to
   inflate the percentage.
6. Measure packet compaction separately from net Codex-token savings. A compact
   response is not proof that Codex spent fewer tokens overall.
7. Treat `metadata.attempt_history` as the authoritative attempt ledger. It
   includes pre-sandbox provider/harness failures and sandbox patch or
   verification attempts; sum runtime usage across all entries instead of
   pricing only the final successful response.

## Completion report

Return a short parent-facing report with:

```text
DELEGATION: SUCCESS | BLOCKED | FAILED
TASKS: accepted/eligible, first-attempt rate, retry count
PROOF: verification result, scope result, artifact path/hash
ECONOMICS: packet reduction; net Codex savings only if matched and measured
NEXT: accept, review artifact, retry once, or take back the task
```

Never claim that a local model's self-report, a successful process exit, or a
packet-size estimate proves correctness or end-to-end token savings.
