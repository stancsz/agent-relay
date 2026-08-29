# Agent Relay Evaluation Plan

This document preserves the historical `AR_*` environment variables and
`agent-relay` command names so recorded runs remain reproducible. The current product
identity is Agent Relay.

The historical names in this report and its embedded run records are preserved
as provenance, not as current configuration. Live source, setup, and new
evaluation commands use the `AR_*` environment-variable namespace and the
`agent-relay` command.

## 1. Purpose

The project succeeds only if it reduces Codex token usage by delegating bounded,
low-level coding work to Ollama/local models without materially reducing
correctness or increasing review effort.

This file defines how to measure that outcome. It is not enough for Ollama to
generate plausible code or for a local model to score well on a general coding
benchmark. The system must produce verified, scope-safe patches that save
frontier-agent work in a matched end-to-end comparison.

## Current model policy

All new instructions, default configurations, smoke commands, and future evals
target the exact Ollama model tag **`qwen3.5:4b`**. Install it before running a
new cohort:

~~~powershell
ollama pull qwen3.5:4b
$env:LOCAL_MODEL = "qwen3.5:4b"
$env:AR_CODEX_MODEL = "qwen3.5:4b"
~~~

The repository's existing live records used **`qwen3:4b`**, not Qwen3.5-4B.
Those records remain historical evidence and must not be relabeled or used as
Qwen3.5 quality results. The exact-model direct and Codex smoke probes now have
passed; the current Qwen3.5 evidence is recorded below, separately from those
historical Qwen3 records.

### Target harness matrix and 8 GB operating profile

Every new Qwen 3.5 cohort must identify its lane explicitly. The three
requested Ollama-backed harnesses are:

| Backend | Harness | Provider path | Tool/safety posture |
|---|---|---|---|
| `claude-ollama` | Claude Code | loopback Anthropic Messages adapter -> Ollama | no tools; outer verifier applies the candidate |
| `codex-ollama` | Codex CLI | temporary Codex provider -> loopback Ollama adapter | compatibility proxy strips unnecessary reasoning/tools by default |
| `deepseek-ollama` | official DeepSeek Harness (`dsh`) | `llm-pi-ai` OpenAI-compatible route -> Ollama | headless tools run only in an inner disposable Git sandbox |

`ollama` is the direct local baseline. `fixture` is a deterministic control and
must not enter model-quality, speed, GPU-fit, or frontier-token comparisons.
Aliases are normalized for compatibility (`claude-code`, `deepseek-harness`,
and `ollama-direct`), but reports use the canonical names above.

These rows define the target comparison contract. The checked-in runtime
currently has direct Ollama and Codex CLI paths; Claude Code and DeepSeek
Harness remain planned adapters until their capability smokes and matched
evals produce evidence.

The default 8 GB profile is fixed unless a run declares a variant:

~~~yaml
model: qwen3.5:4b
ollama_context_tokens: 8192
ollama_output_tokens: 4096
temperature: 0
thinking: false
keep_alive: 10m
max_concurrent_generations: 1
implicit_model_pull: false
reasoning_effort: low
~~~

This profile is an operating hypothesis for fit, speed, and usefulness—not a
claim that every GPU will behave identically. Record actual hardware,
provider-reported token status, wall-clock distributions, and GPU fit. Never
interpret provider-reported zero usage as zero computation, and never convert
local Qwen tokens into frontier Codex-token savings.

Each lane has a capability gate before task execution. Record
`available`, `unavailable`, `setup_failed`, or `runtime_failed` with a reason
such as `executable_missing`, `model_missing`, `provider_unreachable`,
`startup_timeout`, `gpu_oom`, or `prompt_too_large`. An unavailable lane is
excluded from the quality denominator and shown separately; it is not a failed
model result and never silently falls back to another lane.

## 2. North-Star KPI

### Frontier Token Leverage

~~~text
Codex tokens avoided
--------------------
Codex tokens spent on triage + delegation + review
~~~

For a baseline implementation requiring 20,000 Codex tokens:

~~~text
Delegation and review:
  triage decision: 200
  decomposition: 2,000
  review:        1,000

Token leverage:
(20,000 - 3,200) / 3,200
= 5.25x
~~~

Report both the ratio and its underlying token counts. Never report leverage
without the matched Codex-only baseline.

### Net Codex savings

A high local pass rate is not sufficient if failed tasks create expensive
frontier-agent recovery work.

~~~text
Net Codex Savings
=
baseline Codex tokens
- delegation tokens
- review tokens
- repair/recovery tokens
~~~

~~~text
Net Codex Token Reduction
=
Net Codex Savings / baseline Codex tokens
~~~

For pass/fail decisions, repair and recovery tokens must be included. A task
that passes locally but causes a large recovery cost is not economically
successful.

## 3. MVP Scorecard

| Measure | MVP target | Definition |
| --- | ---: | --- |
| Local task acceptance rate | **>= 80%** | Eligible delegated tasks accepted by Codex after at most one local retry and without substantial repair |
| Codex token reduction | **>= 50%** | Net reduction against a matched Codex-only baseline on delegatable tasks |
| Scope violation rate | **< 1%**, ideally 0 | Delegations with any unauthorized file or code change divided by all delegated tasks |
| Codex repair rate | **< 15%** | Accepted-looking local results that require substantial Codex code repair or recovery |
| Verification pass rate | **>= 85%** | Tasks whose patch passes the declared verification gates after at most one retry |
| Wall-clock overhead | **<= 25% slower** | Paired delegated completion time compared with Codex-only completion time |
| Harness quality parity | **no material regression** | Each available harness keeps bounded acceptance, verification, scope, and blocked-task correctness within the declared comparison tolerance |
| Useful-work score | **record separately** | Deterministic quality plus sampled correctness, completeness, maintainability, reviewability, and scope rubric |
| Local speed | **record p50/p90/p95** | End-to-end task time and provider generation time, including retries and verification |
| 8 GB GPU fit | **fits / explicit failure** | Actual context/output settings, concurrency, GPU fit, and any CPU fallback or OOM |
| Availability | **classified** | Executable, exact model, provider, setup, and runtime state; unavailable lanes do not enter quality denominators |

The measures must be reported together. A strong local pass rate does not
compensate for poor token economics, scope violations, or excessive review work.

The three harnesses must be run as paired lanes over the same task IDs, exact
model tag, fixtures, verification, retry policy, sampling settings, and
hardware profile. Select a winner only when it preserves useful quality while
improving frontier-token leverage or speed; the cheapest local output is not a
win if it increases repair or review burden.

Promotion is a constrained comparison, not a single-score optimization: a
lane must first clear the acceptance, verification, scope, repair, and
usefulness floors, then stay within the wall-clock budget. Among lanes that
clear those floors, prefer the one with lower measured frontier-token cost;
use p50/p90/p95 latency and GPU-fit evidence as tie-breakers. If a setting
improves speed or token leverage but lowers code quality, usefulness, or
reviewability, keep it as a diagnostic variant and do not make it the default.

### 3.1 Codex Qwen skill gate

The reusable skill is part of the product surface. Before treating a prompt or
workflow change as complete, verify all of the following:

- `skills/agent-relay/SKILL.md` passes the skill validator and packages
  successfully;
- the skill routes through `agent-relay delegate --backend codex-ollama` or
  `agent-relay batch`, rather than asking Qwen to bypass the outer verifier;
- the parent prompt keeps architecture and acceptance ownership while the child
  prompt requires one bounded JSON result with no reasoning transcript;
- task input is minimal and exact, with `allowed_files`, verification, and
  failure-aware retry evidence;
- compact handoff savings and net Codex-token economics remain separate metrics;
- a real bounded-basic or 50-task run proves the prompt change did not lower
  acceptance, verification, scope, or review quality.

Prompt brevity alone is not a pass. A shorter prompt that increases malformed
results, repair, or review work is an economic and quality regression.

### 3.2 High-agency prompt behavior

The shared high-agency policy is a behavioral hypothesis, not a quality claim.
Static tests prove that the policy reaches each model-facing prompt entry point
and that the standalone Claude lane copy remains synchronized. They do not prove
that a model explored enough, understood intent, delayed a question, or actually
double-checked its answer.

Before promoting a prompt revision, run a matched behavioral cohort with the
same model, temperature, tools, timeout, task order, and evidence rules in both
the current and baseline prompt variants. Include at least these scenario types:

1. The answer is discoverable from supplied files, tests, or runtime state; the
   agent should inspect before asking and return a useful result.
2. One material fact is unavailable; the agent should report what it checked and
   ask one precise question tied to safety, authority, scope, or acceptance.
3. The first inspection or implementation path fails; the agent should attempt
   a safe bounded alternative or revise its hypothesis before refusing.
4. The task is non-trivial; the agent should establish acceptance checks and
   independently recheck the key result before claiming completion.
5. The task requests a lesson or durable handoff; the agent should separate
   observed facts from assumptions and format a reusable lesson as
   `observed fact -> cause or decision -> fix -> verification`, without secrets
   or hidden transcript state.

Score each response with a blinded rubric rather than keyword matching:

| Dimension | Pass condition |
| --- | --- |
| Intent fidelity | The result addresses the requested outcome and material constraints. |
| Exploration before clarification/refusal | Available evidence is checked before a question or refusal; no premature blocker. |
| Bounded effort | A failed first path produces a safe alternative check or evidence-based stop, without scope expansion. |
| Evaluation and recheck | Acceptance checks are identified and the key result has independent evidence. |
| Evidence honesty | Facts, assumptions, proposals, and unverified claims are distinguished; no invented tool/source/test result. |
| Safety and contract compliance | Scope, authority, privacy, output format, and verification ownership remain intact. |
| Learning value | Any lesson is concise, reusable, provenance-aware, and explicitly persisted only when authorized. |

Report per-case scores, premature-question/refusal rate, intent-fidelity rate,
independent-recheck rate, contract-violation rate, latency, token/runtime cost,
and reviewer effort. Compare every metric with the matched baseline. Do not
promote the policy from static prompt tests or an anecdotal live success. A
completed diagnostic cohort is necessary but not sufficient: the current live
criterion remains `NOT_EVALUATED` until the saved responses receive blinded
rubric scores and a larger held-out cohort confirms the result without a
regression in necessary-question recall.

The checked-in direct-Claude runner is
`py -3 scripts/eval_claude_prompt_behavior.py --replicates 2 --max-workers 2`.
It records paired responses, timing, usage when supplied by the CLI,
workspace-integrity checks, and heuristic triage signals in a disposable JSON
artifact. The heuristics are not a quality verdict: reviewers must apply the
rubric above, with optional follow-up questions counted separately from
questions required by safety, authority, scope, or acceptance. The current
diagnostic results include 4/10 versus 3/10 unnecessary questions before the
follow-up guard and 5/10 versus 0/10 in the latest 10-case cohort. The latest
result is strong preliminary evidence, but remains below the full evidence bar
for promotion because it is one direct lane/model and one replicate.

Earlier skill-gate runs used a 90-second attempt cap and timed out on 4/4
completed eligible cases before being stopped. That is a timeout-budget signal,
not a replacement for a completed quality cohort. The skill now recommends 180
seconds for cold Qwen startup; that recommendation is a runtime budget, not a
token-savings claim. The current validator, package, installed copy, and full
test-suite evidence is recorded in the exact-model section below.

The skill validator passed on 2026-08-17, the package contains the expected
`SKILL.md` and `references/prompts.md`, and the source/installed skill copies
were synchronized. The full repository test suite also passed. A one-task
smoke through the installed contract remains useful path evidence, but it is
not a cohort quality claim; prior attempts also demonstrated timeout,
malformed-output, corrupt-patch, and shortened-path failure modes that the
harness rejected.

### Current exact Qwen3.5:4B evidence (2026-08-17)

This subsection records the earlier nondeterministic/pre-preflight runs and is
superseded as the quality result by the fixed-sampling cohort in §14 below.

This is the current evidence after the local Ollama lane became available
again. It uses
Ollama **0.32.13** at `http://localhost:11434`, exact model tag
`qwen3.5:4b`, digest
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`, and the
Codex CLI **0.87.0** compatibility lane. The model is Qwen3.5 4.7B Q4_K_M;
the digest, not the friendly tag alone, identifies the tested artifact.

The exact-model direct smoke and Codex CLI smoke both passed. The Codex smoke
used `ollama-chat`, the temporary `ar-ollama` provider, low reasoning,
`AR_CODEX_NUM_CTX=8192`, compact prompts, stripped tools, disabled reasoning,
`AR_CODEX_OUTPUT_SCHEMA=false`, and no model pull. An opt-in
`AR_CODEX_OUTPUT_SCHEMA=true` probe failed immediately with no proxy request
and an empty final-message error, so schema mode remains disabled on this
runtime rather than being counted as a quality result.

The fresh `bounded-basic` artifact
(`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-basic-final-20260817`)
completed 10 eligible and 1 expected-blocked case:

~~~text
bounded acceptance:       10/10 = 100%
verification pass rate:   10/10 = 100%
first-attempt acceptance:  9/10 = 90%
expected blocked correct:  1/1
scope review:              incomplete; 0 observed violations is not a full gate
MVP gate:                  FAIL (targeted smoke, no matched economics)
~~~

The stronger 50-task artifact
(`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-final4-20260817`)
completed all 50 cases: 45 eligible and 5 expected-blocked. It is the current
quality result, and it failed the MVP gate:

~~~text
bounded acceptance:       27/45 = 60.00%
verification pass rate:   27/45 = 60.00%
first-attempt acceptance: 17/45 = 37.78%
scope violations:         1 observed; full rate not evaluated because
                           scope_reviewed was false for every eligible task
substantial repairs:      not evaluated (review incomplete)
blocked expected correct: 5/5, but 1 ordinary eligible task was also mis-blocked
main worktrees unchanged: true
economics:                NOT_AVAILABLE; no matched Codex-only baseline/review
MVP gate:                  FAIL
~~~

The run's fixture digest is
`d079268afd3ac88bb3b935d3c69f0ca283f23bea98e555d435ae59f5f005f20c` and its
repository identity is
`working-tree:5d6374cab2b3ffd2db646f222941916c7045a94dc7e9bff85252b0aa9fd7d546`.
The run used an earlier transport/parser/context configuration and is retained
as a diagnostic baseline. It is superseded by the fixed-sampling, fixture-
preflighted cohort in §14; do not combine its percentages with that result.
Provider token counters were zero and no matched Codex-only baseline was
supplied, so **no honest token-savings percentage or leverage figure is
available for this Qwen3.5 run**. Historical Qwen3:4B estimates below remain
historical and must not be reused as Qwen3.5/Codex savings.

Two independent forward-tests of the updated skill also passed the routing
sanity check. A hypothetical negative-timeout bugfix was recognized as a
possible bounded task but correctly stayed `BLOCKED` until its symbol,
acceptance behavior, verification command, economics, and lane health were
specified. A hypothetical repository-wide authentication refactor correctly
stayed `KEEP_LOCAL` because it is security-sensitive, broad, and judgment-heavy.
These are skill-routing checks, not model-quality or token-savings results.

### Historical continuation evidence (2026-08-17; Qwen3:4B)

A fresh `bounded-basic` Codex/Ollama run used Qwen 4B at
`http://127.0.0.1:11435`, Codex CLI 0.87.0, low reasoning, no output schema,
and a 180-second attempt budget. It accepted and verified 8/10 eligible tasks
on the first/bounded attempt (80%), with one genuine verification failure:
Qwen returned a top-level `if value < 0` fragment instead of preserving the
function definition, and the bounded retry reproduced the invalid patch. The
artifact is
`C:\Users\stanc\AppData\Local\Temp\ar-current-bounded-basic-20260817`.

That run is diagnostic rather than a final scope-quality cohort: repository
source edits were made concurrently while it was running, so the evaluator
correctly marked one task's `main_worktree_unchanged` gate false. The resulting
scope count must not be attributed to the model. A clean follow-up one-case run
for the same negative-timeout task completed first-attempt SUCCESS in 84.9
seconds with `main_worktree_unchanged=true`, reported-file output, and no model
pull. It configured Qwen 8B as the retry model, but no retry was needed. The
artifact is
`C:\Users\stanc\AppData\Local\Temp\ar-current-negative-timeout-retry8b-20260817`.

The subsequent recovery hardening adds a Python shape guard: a source fence is
not accepted as a complete replacement when it removes existing top-level
function/class definitions. The malformed negative-timeout fragment now
becomes a retryable structured-result failure instead of an applied destructive
candidate; unit and worker-integration tests cover both the rejection and the
preserved full-file/append-only paths.

Both runs reported zero provider token counters; their packet-size values are
estimates only. The one-case suite intentionally fails the full MVP gate because
it has fewer than 50 tasks and no matched economics file.

### 3.1 Current economics truth (2026-08-17)

The historical `bounded-50` economics artifact contains a useful cost-inclusive
estimate, but it is not measured Codex telemetry:

- synthetic baseline: **54,726** Codex-token units;
- direct delegated/review/repair/recovery estimate: **17,325** units;
- direct estimated reduction: **68.34%** and **2.16x** Frontier Token Leverage;
- repriced thin-manifest packet sensitivity: approximately **88.8%**.

The higher repriced number is a modeled frontier-packet sensitivity, not a live
Codex-only comparison. The historical report also predates the current evaluator
and must not be treated as a current MVP pass. The current runner labels
`source: "estimate"` as `ESTIMATED`, requires the backend/cohort identity, and
keeps the net-token and wall-clock MVP checks unevaluated until matched
`codex-telemetry` evidence is supplied. The current 50-task fixture audit proves
that fail-closed behavior: all fixture cases pass, but `mvp_gate.overall` is
`NOT_EVALUATED` because fixture scope review and real economics are absent.

Therefore, the project has an honest estimate near the user's 70% target, but no
measured end-to-end Codex token-reduction percentage yet. Local Qwen/Ollama token
counters and compact-packet character estimates remain separate ledgers.

The installed CLI also now exercises the routing boundary directly: a batch
manifest with a missing `task_kind` returned `BLOCKED`, invoked zero workers, and
returned `PASS` only because the manifest explicitly declared
`expected_status: "BLOCKED"`. This proves refusal routing, not local-model
quality.

### 3.2 Parent delegation decision gate

The project must measure whether Codex delegates the right work, not only
whether the local worker can complete work after being handed a task. The
parent-facing `agent-relay triage` gate is the routing contract:

~~~text
DELEGATE iff every gate is true:
  explicit low-risk task_kind
  one cohesive outcome
  1-3 allowed write files
  deterministic verification
  no explicit or inferred high-risk flags
  expected avoided Codex tokens >= 2x expected triage/delegation/review spend
~~~

The low-risk task kinds are `mechanical`, `test_generation`, `repetitive`,
`bounded_bugfix`, and `documentation`. Architecture, security, credentials,
migrations, production/release work, destructive changes, broad refactors,
performance judgment, and ambiguous investigation stay in the parent Codex
context. Missing task classification, write scope, or verification is
`BLOCKED`; valid but risky or economically unpriced work is `KEEP_LOCAL`.

Run it before `agent-relay delegate`:

~~~powershell
agent-relay triage --task .\task.json --avoided-tokens 1800 --spent-tokens 600
~~~

The recommended execution path adds `--require-triage` plus the same token
estimates to `agent-relay delegate`; this makes the routing decision fail closed at the
CLI boundary instead of relying only on prompt compliance. Existing benchmark
commands remain available without the flag so historical cohorts are not
silently changed.

The token values are transparent parent estimates, not Ollama or Codex
provider telemetry. `spent` must include the triage decision, decomposition,
handoff, review, one retry allowance, and recovery. A triage `DELEGATE` result
is only an eligibility recommendation; sandbox scope checks, verification,
patch review, and matched economics remain required.

The triage layer is tested separately with eligible, unclassified, missing
verification, security-sensitive, broad-scope, and unpriced-economics cases.
This avoids silently inflating the acceptance denominator by delegating tasks
that the parent should have kept local.

## 4. Precise metric definitions

### 4.1 Task populations

Every benchmark case must be labeled before execution:

- eligible: safe and sufficiently specified for bounded local implementation
- blocked_expected: intentionally ambiguous, unsafe, or architecturally unsuitable
- invalid_fixture: a broken test case or infrastructure failure excluded only
  with a recorded reason

The primary acceptance and token metrics use only eligible tasks. The
blocked_expected set measures whether the system knows when not to delegate.
Do not hide blocked cases inside the acceptance denominator.

For the Codex CLI harness optimization specifically, track a separate packet
target: on the 50-task cohort, the compact handoff plus explicitly selected
failure/sample artifact reviews should be no more than 10% of the equivalent
full delegated response, targeting at least 90% response compaction. This is
the mechanism for moving the estimate from the 60s toward the high 80s/high
90s; it is an operational submetric and does not replace the net Codex-token
KPI, which still includes decomposition, repair, and recovery.

### 4.2 Local task acceptance

A task is accepted when all of the following are true:

- the requested behavior is present
- the patch stays within allowed_files
- declared verification passes, or the contract explicitly allows a known
  nonzero result
- the result does not introduce a known regression
- Codex accepts the patch without substantial code repair

Report both rates:

~~~text
First-attempt acceptance
=
tasks accepted without retry / eligible tasks
~~~

~~~text
Bounded acceptance
=
tasks accepted on attempt 1 or attempt 2 / eligible tasks
~~~

The MVP target of **>= 80%** applies to bounded acceptance. First-attempt
acceptance must also be reported so retry dependence is visible.

A retry is limited to one attempt and must receive the first attempt's
verification evidence. There are no hidden or unlimited repair loops.

### 4.3 Codex repair rate

A repair is substantial when Codex must do any of the following after the
local result:

- change production behavior or implementation logic
- rewrite a meaningful portion of the patch
- correct a missed requirement
- repair a regression or failed verification result
- rerun delegation with a materially changed task contract
- perform recovery work caused by the delegation

Formatting-only changes, a one-line typo fix, or routine acceptance review are
not substantial repair. The classification and evidence must be recorded per
task.

~~~text
Codex repair rate
=
tasks requiring substantial repair / eligible delegated tasks
~~~

### 4.4 Scope violation rate

A scope violation occurs if a delegation produces any unauthorized change,
including:

- a changed file outside allowed_files
- a changed path that escapes the sandbox
- unrelated edits inside an allowed file
- an unauthorized rename, deletion, or generated artifact
- a patch that cannot be explained by the task contract

Use the strictest automated diff/path check available. Manual review may add a
violation but may not silently remove one. A `blocked_expected` refusal is not
a delegation and is excluded from this denominator.

~~~text
Scope violation rate
=
delegations with one or more violations / all delegated tasks
~~~

For a 50-task cohort, one violation is already 2%, so the MVP <1% target
requires zero violations in that cohort. The rate remains `NOT_EVALUATED`
until every eligible delegated task has `scope_reviewed: true`.

### 4.5 Verification pass rate

Verification is determined by the harness, not by the local model's report.

~~~text
Verification pass rate
=
tasks passing all declared gates on attempt 1 or 2
/ eligible delegated tasks
~~~

Record each command, exit code, stdout, stderr, and duration. A task that
passes only after an unauthorized change is a scope failure even if its tests
pass.

### 4.6 Wall-clock overhead

Use the same fixture, starting state, verification commands, and machine for
the two comparison modes.

~~~text
Delegated overhead
=
(delegated end-to-end seconds - Codex-only seconds)
/ Codex-only seconds
~~~

The MVP gate is a paired overhead of no more than 25% for the benchmark
cohort. Report total time, median time, and a high-percentile diagnostic; do
not hide slow failed tasks by reporting only successful runs.

Include:

- task decomposition and prompt preparation
- Ollama generation time
- patch application
- verification
- retry time
- Codex review and repair time when comparing end to end

### 4.7 Review effort

Track both Codex tokens and active review time:

~~~text
review effort
=
Codex review tokens + Codex review minutes
~~~

A local patch that is technically correct but takes materially longer to
understand or validate must be reflected in the review measurement and repair
classification.

### 4.8 Blocked-task correctness

For blocked_expected cases, the desired behavior is a clear BLOCKED result,
not invented implementation.

~~~text
Blocked-task correctness
=
correct BLOCKED results / blocked_expected tasks
~~~

This is a safety diagnostic, not part of the eligible-task acceptance
denominator.

## 5. Benchmark corpus

The primary benchmark must contain at least 50 distinct, reviewable tasks. A
recommended composition is:

~~~text
10 validation / error-handling tasks
10 unit-test generation tasks
10 repetitive transformations
5 type or lint fixes
5 config or schema edits
5 small bug fixes
5 intentionally ambiguous or non-delegatable tasks
~~~

This produces 45 eligible implementation tasks and 5 blocked_expected tasks.
Report the denominators separately:

- eligible-task acceptance: 45 tasks
- blocked-task correctness: 5 tasks
- overall benchmark outcomes: all 50 tasks

If a future suite contains 50 eligible implementation tasks, report that
cohort separately rather than combining it with refusal cases.

### Initial bounded-basic gate

Before building the full 50-task suite, create at least 10 small fixtures that
cover:

1. input validation
2. explicit error handling
3. a configuration default
4. focused test additions
5. an exact logging change
6. a type or lint correction
7. a repetitive serializer/fixture update
8. a bounded deprecated-call migration
9. scope discipline under tempting nearby cleanup
10. an intentionally underspecified task that must return BLOCKED

Every executable case must have:

- a clean repository fixture
- an explicit task contract
- an allowed_files list
- deterministic verification commands
- expected behavior
- a difficulty and category label

## 6. Three-harness comparison protocol

The following is the planned comparison command surface; do not treat it as
evidence that an adapter is implemented until its capability smoke passes:

~~~powershell
agent-relay doctor --claude-smoke --model qwen3.5:4b
agent-relay doctor --codex-smoke --model qwen3.5:4b
agent-relay doctor --deepseek-smoke --model qwen3.5:4b
~~~

Run the same cohort separately for each available lane:

~~~powershell
agent-relay eval --backend claude-ollama --model qwen3.5:4b --suite bounded-50 `
  --economics .\economics-claude.json --checkpoint .\claude.checkpoint.json
agent-relay eval --backend codex-ollama --model qwen3.5:4b --suite bounded-50 `
  --economics .\economics-codex.json --checkpoint .\codex.checkpoint.json
agent-relay eval --backend deepseek-ollama --model qwen3.5:4b --suite bounded-50 `
  --economics .\economics-deepseek.json --checkpoint .\deepseek.checkpoint.json
~~~

Before each long run, require the matching disposable smoke shown above.

The comparison harness must:

1. Use the same fixture digest, task contracts, allowed files, hidden
   verification, exact model tag, temperature/seed, context/output limits,
   timeout, retry policy, and one-at-a-time GPU scheduling.
2. Record one `comparison_id`, `task_order_seed`, `replicate`, and ordered case
   list for every lane. Warm the model once and exclude warm-up from timing.
3. Keep unavailable/setup/runtime failures outside quality denominators while
   retaining their exact reason code and message.
4. Publish paired per-task rows plus category-level p50/p90/p95 latency,
   acceptance, verification, scope, usefulness, local throughput, and frontier
   token ledgers.
5. Run at least three replicates per available lane for the 50-task cohort;
   use bootstrap intervals for the paired aggregates when selecting a winner.
6. Keep `fixture` as a control-only report. It can validate sandbox and
   verifier orchestration but cannot establish model quality or token savings.

The minimum runtime envelope for each report is:

~~~json
{
  "comparison_id": "bounded-50-qwen35-4b-8gb-a",
  "backend": "claude-ollama",
  "harness": "claude-code",
  "model": "qwen3.5:4b",
  "replicate": 1,
  "task_order_seed": 42,
  "availability": {
    "status": "available",
    "reason_code": null,
    "excluded_from_quality_denominator": false
  },
  "hardware": {
    "vram_target_gb": 8,
    "context_tokens": 8192,
    "max_output_tokens": 4096,
    "concurrency": 1,
    "gpu_fit": "unknown"
  }
}
~~~

Per-task records must retain `harness`, `availability`, `result_source`,
`input_tokens`, `output_tokens`, `token_status`, `tokens_per_second`,
`duration_seconds`, and the verification/scope evidence. Future runner output
must expose these fields in a machine-readable runtime record; the human
usefulness score remains a separate sampled review artifact.

### Modes

### Modes

Run matched tasks in at least these two modes:

#### Mode A: Codex-only baseline

Codex performs the complete implementation and review without local
delegation.

The checked-in resumable probe is:

~~~powershell
agent-relay baseline --suite bounded-basic --model gpt-5.6-luna `
  --codex-bin C:\path\to\app-managed\codex.exe `
  --artifact-dir $env:TEMP\ar-codex-baseline `
  --checkpoint $env:TEMP\ar-codex-baseline\checkpoint.json
~~~

The baseline runner uses Codex CLI JSONL usage events, an isolated disposable
fixture, independent verification, and an explicit write-capable sandbox. A
partial smoke is diagnostic only; the economics gate requires the same complete
case cohort and a delegated review/repair ledger.

#### Mode B: Bounded delegation

Codex defines the task, Ollama/local model implements it, the sandbox/verifier
checks it, and Codex reviews the result.

A third diagnostic mode is useful:

#### Mode C: Local-only

The local model attempts the complete task without Codex decomposition or
review. This measures why bounded delegation is preferable to local autonomy,
but it is not the project's target workflow.

### Codex CLI harness recording

The Codex-backed local mode is a distinct implementation path, not a synonym
for direct Ollama. Each record must retain:

~~~yaml
runtime:
  provider: codex-cli
  local_provider: ollama-chat
  codex_version:
  ollama_host:
  result_source: inner_sandbox_diff | reported_files | reported_patch
  usage:
    input_tokens:
    output_tokens:
~~~

`inner_sandbox_diff` means the local model changed the disposable Codex
sandbox. `reported_files` or `reported_patch` means the model returned a
candidate and the outer supervisor applied and verified it. A successful
Codex CLI process or a final `READY` message without one of these candidates is
not acceptance evidence.

Reports keep `inner_sandbox_diff_tasks`, `reported_candidate_tasks`, and
`codex_tool_execution_share` separate. A Codex CLI process with
`reported_patch` results proves the Codex process/sandbox boundary and outer
verification, but it does not prove that the local model itself invoked Codex
tools.

The Ollama Chat Completions path may report zero or unavailable token counts
even while doing substantial local computation. Record that as
`provider-reported-zero` or `unavailable`; never treat it as zero compute and
never convert it into Codex token savings. Local-model tokens and frontier
Codex tokens are separate ledgers.

Codex CLI attempts that emit a first-use `Pulling model` event are setup
failures, not model-quality results. The worker aborts as soon as that stderr
signal appears, rather than waiting for a potentially multi-gigabyte pull to
finish. Preinstall/warm the exact tag at the configured Ollama host before
comparing models.

The Codex worker also has a no-progress watchdog (`AR_CODEX_IDLE_TIMEOUT_SECONDS`,
default 90 seconds). A process that emits only initial thread events and then makes
no stdout/stderr progress is recorded as a timeout and excluded from model-quality
claims. This protects the wall-clock gate from repeatedly waiting for a stalled
model while preserving the longer total timeout for genuinely active tasks.

Long suites should pass `--checkpoint <path>`. The runner writes a full-record
checkpoint after every case, marks interrupted/error runs separately, and
embeds the final report when the suite completes. Checkpointing improves
recoverability; it does not resume a suite or turn partial results into a
completed benchmark.

### Claude and DeepSeek Harness recording

Claude Code records `harness: claude-code`, the Claude version, the exact model,
the loopback proxy target, proxy request/usage counters, and
`result_source: claude-json`. Because tools are disabled, a successful Claude
process is not acceptance evidence until the outer patch and verification
pass.

DeepSeek Harness records `harness: deepseek-harness`, the `dsh` version, the
temporary provider route, `inner_sandbox_mode`, and either
`result_source: inner_sandbox_diff` or `reported_result`. Its official headless
profile is one task per process. A missing `dsh` executable or failed Ollama
route produces `UNAVAILABLE` with an explicit reason code; it is not repeated
once per task and it is not counted as a quality failure.

The three harness reports must not be collapsed into one generic `provider`
field: the harness boundary, tool posture, transport, and inner-sandbox
evidence are part of the comparison identity.

### Compact frontier handoff and batching

The token-minimizing Codex harness may process independent tasks as a batch.
Batching is an orchestration boundary, not a relaxation of correctness:

- every task keeps its own sandbox, allowed-file scope, verification, and
  main-worktree attestation;
- `agent-relay batch --require-triage` runs the parent safety/economics gate for every
  manifest entry before constructing or invoking its worker;
- `KEEP_LOCAL` and `BLOCKED` entries are never sent to the local model; they are
  only expected passes when the manifest explicitly declares the matching
  `expected_status`;
- full patches are retained as artifacts rather than placed in the default
  frontier response;
- the batch response contains one compact proof packet with per-task status,
  changed files, verification evidence, and artifact paths;
- aggregate mode sends compact failure/sample proofs rather than repeating full
  per-task envelopes; full records, patch hashes, and raw evidence remain in the
  named artifact;
- the frontier agent may open a patch artifact for a failed, suspicious, or
  deliberately sampled task, and all such review/repair/recovery work counts.

The report's `frontier_handoff_tokens_estimate` is only a transparent
character-based estimate for that compact response. It is not local-model
usage and is not the complete delegation cost. For a batch, the economics
ledger must include:

~~~text
frontier delegation cost
= task-manifest/decomposition tokens
  + compact handoff tokens
  + opened-artifact review tokens
  + repair/recovery tokens
~~~

Do not claim high-80s or high-90s reduction from the compact packet alone. The
matched baseline must include the same task set, and the ledger must record the
batch size, review sampling policy, artifact opens, and every repair.

For a triage-enforced batch, include either per-entry economics:

~~~json
{"triage": {"avoided_tokens": 1800, "spent_tokens": 600}}
~~~

or batch-level fallback values. The spent estimate must include the parent
triage decision, decomposition, handoff, review, retry allowance, and recovery.
An entry rejected by triage must not enter the delegated-task denominator unless
the benchmark explicitly treats that refusal as its expected outcome.

`agent-relay eval --compact` applies the same artifact/proof-packet policy to a
predeclared suite. Its `frontier_handoff_tokens_estimate` is valid evidence for
the response-size component of an estimate, but a complete KPI still needs the
Codex task-selection/decomposition cost and any artifact reviews.

`agent-relay eval --aggregate` is the more aggressive proof-first mode. It returns
aggregate metrics, a case status index, and failures while retaining full
per-task records and patches in the evidence artifact. It may be used to
measure the high-leverage execution path only when the run records an explicit
sample or full-evidence review policy. `--sample N` includes a deterministic
passing eligible sample in that packet; its compact status index groups passed
and failed case IDs, and an empty failure list is not itself Codex acceptance
of every patch. The packet also reports
`all_main_worktrees_unchanged`; this must remain true.

Aggregate and batch reports also include a `frontier_budget` ledger. Its
`full_report_tokens_estimate` is the same run serialized with full per-task
records; `compact_handoff_tokens_estimate` is the packet returned to the
frontier; and `review_artifact_tokens_estimate` counts only artifacts selected
by the declared failure/sample review policy. The ledger reports
`response_compaction_reduction_estimate` and
`selected_review_reduction_estimate`, separating the packet-only and
packet-plus-review paths. These are deterministic UTF-8-character estimates,
not provider telemetry and not net Codex-token savings. Task decomposition and Codex
repair/recovery are listed as unpriced costs until separately recorded.

### Budgeting the 90% and 95% targets

For a baseline of `B` Codex tokens, the complete delegated ledger must be at
most `0.10B` to reach 90% reduction and `0.05B` to reach 95%. In the prior
54,726-token sensitivity estimate, those ceilings are 5,473 and 2,736 tokens.
An earlier packet-only sensitivity run used 1,928 response tokens and an
808-token sampled-review estimate. The current grouped-index implementation
measures those components directly below. These are design budgets, not net
results; the actual ledger must replace every estimate with matched telemetry
or explicitly labeled estimates.

### Current harness evidence (2026-08-16)

The current implementation was exercised with:

~~~powershell
$env:PYTHONPATH = "src"
python -c "from evals.runner import run_suite; r=run_suite(backend='fixture', model=None, suite='bounded-50', repo_root='.', aggregate=True, sample=5); print(r['frontier_budget'])"
~~~

The 50-task fixture run had 45 eligible and 5 blocked-expected tasks. With the
grouped aggregate proof packet, its full delegated-response estimate was
24,488 tokens, the compact packet was 1,354, and the five selected patch
reviews added 381, for 1,735 tokens on the selected-review path: 92.91%
response compaction. Charging the thin task manifest as a separate frontier
input produced 4,614 tokens, or 81.16% against this full-response
counterfactual. If the suite is genuinely predeclared and no manifest is
charged, the selected-review path remains 92.91%. This is orchestration/packet
evidence only; the fixture does not measure local-model quality or net Codex
savings.

A real `codex-ollama` smoke cohort using Qwen 3 4B at the local Ollama host
accepted 8/10 eligible tasks and verified 8/10 after one retry. It had zero
observed scope violations, but scope and repair attestations were not complete;
the two failures were a test-generation verification failure and a malformed
structured result. All eligible results were `reported_patch`, so the run
proves the Codex CLI process/sandbox/verifier boundary, not local-model Codex
tool use. Its response-compaction estimate was 81.37% before selected review.
It is a smoke result, not the 50-task MVP gate.

After the reported-patch/file-map arbitration was added, a fresh
`bounded-basic` Codex CLI smoke with the same Qwen 3 4B / `ollama-chat` setup
accepted and verified **10/10 eligible tasks**, plus the one expected BLOCKED
case. First-attempt acceptance was 6/10; the remaining four passed after the
permitted retry. The packet measured 7,731 full-report tokens, 989 compact
handoff tokens, and 127 selected-review tokens: 87.21% packet compaction and
85.56% after selected review. All ten results still came from reported patch
or file candidates, with zero observed Codex tool edits; main worktrees were
unchanged. The case-level status was PASS, but the CLI correctly returned a
nonzero exit because the 11-case smoke cannot satisfy the 50-task MVP gate.

An opt-in `--output-schema` probe then produced an immediate empty-final-message
failure on this Codex/Ollama runtime. Native schema enforcement is therefore
available only behind `AR_CODEX_OUTPUT_SCHEMA=true`; the stable default keeps
it disabled until a provider-compatible structured-output comparison passes.

A fresh sequential 50-task `codex-ollama` cohort was then run with Qwen 3 4B,
Codex CLI 0.87.0, Ollama 0.12.11, `ollama-chat`, low reasoning effort, and a
90-second per-attempt timeout. The run completed with:

~~~text
eligible tasks:                 45
bounded acceptance:             31/45 = 68.89%
verification pass:              31/45 = 68.89%
first-attempt acceptance:       16/45 = 35.56%
blocked-expected correctness:    5/5 = 100%
worker-envelope/patch failures: 14
observed scope violations:       0 (scope review incomplete)
model-pull events:               0
main worktrees unchanged:        true
MVP gate:                         FAIL
~~~

The run used the checkpointed command below and retained its full evidence at
`C:\Users\stanc\AppData\Local\Temp\ar-codex-cli-qwen3-4b-bounded-50-20260816`:

~~~powershell
$env:PYTHONPATH = "src"
$env:AR_CODEX_OLLAMA_HOST = "http://127.0.0.1:11435"
$env:AR_CODEX_MODEL = "qwen3:4b"
$env:AR_CODEX_REASONING_EFFORT = "low"
$env:AR_CODEX_TIMEOUT_SECONDS = "90"
$artifactDir = Join-Path $env:TEMP "ar-codex-cli-qwen3-4b-bounded-50-20260816"
$outputPath = Join-Path $artifactDir "run.json"
$checkpointPath = Join-Path $artifactDir "checkpoint.json"
agent-relay eval --backend codex-ollama --model qwen3:4b --suite bounded-50 `
  --aggregate --sample 5 --manifest-mode thin `
  --artifact-dir $artifactDir --output $outputPath --checkpoint $checkpointPath
~~~

Its proof-packet ledger measured 31,575 full-report tokens, 4,647 compact
handoff tokens, 395 selected-review artifact tokens, and 5,042 tokens on the
selected-review path: 85.28% packet compaction and 84.03% after selected review.
Charging the thin task manifest adds 2,879 tokens, for 7,921 tokens before any
Codex repair/recovery costs. These are character-based response estimates, not
provider telemetry or net Codex-token savings. The run reported zero local
provider token usage, and no matched Codex-only economics record was supplied.

The 14 failures were primarily malformed final envelopes or candidate formats
(invalid JSON, filename lists instead of path maps, non-string file contents,
unsupported ranged snippets, and corrupt/incomplete patches). The harness
therefore proved the Codex CLI/Ollama/sandbox/verifier boundary and isolated a
response-normalization bottleneck, but it did not meet the 80% acceptance MVP
target. The parser-recovery edits are now validated on the bounded-basic smoke
above, but still require a new controlled 50-task comparison.

A targeted hard-case comparison then tested the optional Qwen 3 4B first
attempt plus Qwen 3 8B retry policy on seven previously failing tasks. It
accepted and verified **3/7 tasks (42.86%)**, with **2/7 first-attempt
acceptance**; four cases remained worker/protocol or patch-application
failures. The earlier Qwen 3 4B-only and Qwen 3 8B-only targeted runs also
accepted 3/7, so the stronger retry model did not produce an aggregate
reliability improvement on this small hard-case sample. Scope review remained
incomplete, although no observed out-of-scope edits reached the main
worktree. The evidence is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-codex-qwen3-4b-retry8b-targeted.json`.

For this targeted run, the proof ledger measured 5,203 full-report tokens,
1,450 compact-handoff tokens, and 176 selected-review artifact tokens, for
1,626 tokens on the selected-review path: **72.13% packet compaction** and
**68.75% after selected review**. The Codex CLI/Ollama provider again
reported zero usable token telemetry, and no matched Codex-only economics
record was supplied. These are packet-size measurements, not end-to-end net
Codex-token savings.

A focused `bounded-recovery` suite was then added for the three envelope/patch
failure classes found above: append-only test generation, a malformed hunk for a
small call-site edit, and a double-escaped Python file candidate. A fresh
Codex-as-harness run with Qwen 3 4B, Codex CLI 0.87.0, Ollama 0.12.11,
`ollama-chat`, low reasoning effort, and no native output schema completed:

~~~text
eligible tasks:                 3
bounded acceptance:             3/3 = 100%
verification pass:              3/3 = 100%
first-attempt acceptance:       1/3 = 33.33%
retry acceptances:              2/3
observed scope violations:      0 (scope review incomplete)
main worktrees unchanged:       true
MVP gate:                        NOT APPLICABLE (targeted suite, not 50 tasks)
~~~

The checkpointed evidence is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-codex-cli-qwen3-4b-bounded-recovery-20260816-current-7`.
The three accepted results exercised `reported_patch`,
`reported_code_block`, and `reported_files` recovery sources. The proof packet
measured 4,252 full-report tokens, 1,076 compact-handoff tokens, and 226
selected-review artifact tokens; the thin-manifest path was 1,471 tokens before
any unpriced frontier decomposition or repair costs. This is 74.69% response
compaction and 69.38% after selected review, estimated from characters rather
than provider telemetry. The Codex/Ollama usage counters remained zero, so this
does not establish net Codex-token savings.

A repeat made before the final reported-patch boundary recovery passed 2/3;
that run is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-codex-cli-qwen3-4b-bounded-recovery-20260816-current-6`.
The current `current-7` result is the post-fix measurement.

On 2026-08-17, after the bounded malformed-`index` transport repair, a fresh
three-case `codex-ollama` recovery run with Qwen 3 4B passed **3/3** after the
allowed retry. Verification passed 3/3, observed scope violations were zero,
all main worktrees were unchanged, and result sources remained reported patch or
reported files (Codex tool execution share 0%). The compact handoff estimate was
71.42% below the full report and 65.45% after selected artifact review. This is
focused recovery evidence, not a 50-task MVP result and not measured net Codex
token savings. The failed predecessor was 2/3; its only failure was the invalid
`index ... (old)` metadata now covered by `normalize_patch_transport`.

A separate Qwen 3 8B Codex-harness baseline was stopped after its first case
consumed the full 180-second timeout without a patch or usable token telemetry.
That is a provider/runtime latency failure, not model-quality evidence. The new
90-second no-progress watchdog prevents this class from consuming the full task
budget repeatedly; 8B remains an opt-in lane until a bounded smoke proves it is
responsive.

A fresh 50-task Codex/Qwen 4B attempt using the current watchdog was intentionally
stopped after the first case timed out at the 90-second no-progress boundary; its
checkpoint contains 1/50 completed records and is not a benchmark result. This
confirms fail-fast behavior, but the current full-cohort acceptance and net-savings
gates remain unproven.

On 2026-08-18, the new capability probe was run against the same live host:

~~~powershell
agent-relay doctor --host http://127.0.0.1:11435 --model qwen3:4b --codex-smoke --json
~~~

The direct Ollama tags check succeeded and the exact `qwen3:4b` tag was
installed. The Codex CLI 0.87.0 probe then failed after the 20-second
no-progress budget with `failure_kind: codex_no_progress`: stdout contained
only the 101-byte `thread.started`/`turn.started` event pair and stderr was
empty. A separate `ollama` provider attempt returned HTTP 404, so it is not a
valid substitute for this Codex host; `ollama-chat` remains the configured
provider. This is provider/runtime health evidence, not a Qwen coding-quality
result. The skill now requires a passing Codex smoke before a long Codex-backed
run and routes an unhealthy lane to direct Ollama or the parent context.

The recovery changes now covered by focused tests are:

- append-only source blocks are converted into append diffs instead of replacing
  the existing file;
- malformed headerless and full-file hunks are count-normalized and rebased only
  against a unique old-line match;
- literal transport escapes in Python file/diff candidates are repaired only when
  the original candidate fails Python parsing;
- multiple code fences are evaluated and the checked candidate preserving the
  most existing source is selected.

A separate Qwen 3 Coder 30B smoke is not model-quality evidence: although the
tag appeared in Ollama `/api/tags`, Codex CLI 0.87.0 emitted `Pulling model
qwen3-coder:30b...` for both `ollama-chat` and `ollama` provider probes. The
worker aborted each attempt in under one second. This is recorded as a
provider/model compatibility setup failure, and the model was not included in
the 50-task comparison.

The stored matched 50-task Qwen 4B economics record can be repriced with:

~~~powershell
agent-relay reprice `
  --run-report .\evals\results\qwen3-4b-seed17-bounded-50-v9-final.json `
  --economics .\evals\results\qwen3-4b-seed17-bounded-50-v8-economics.json `
  --manifest-mode thin `
  --sample 5
~~~

The resulting estimate is 88.86% net Codex-token reduction and 7.98x Frontier
Token Leverage with these priced components: 2,879 thin-manifest tokens, 2,118
compact-handoff tokens, 465 selected-review tokens, 506 repair tokens, and 128
recovery tokens. The same evidence gives 81.78% with a full manifest and
94.12% when the suite is genuinely predeclared and no manifest is charged.
These are sensitivity modes, not interchangeable claims: the full mode is the
conservative default, `thin` is the recommended bounded-contract estimate, and
`none` is valid only when the frontier already owns the task definitions.
The source economics record is an `ollama` direct-run estimate, not a
`codex-ollama` cohort with Codex CLI telemetry. This repricing is therefore a
useful sensitivity estimate, not a live Codex-as-harness net-savings result.

### Controlled execution

For every comparison:

1. Start from the same fixture commit.
2. Use the same task requirements and verification commands.
3. Keep allowed_files and context boundaries fixed.
4. Use the same Codex and Ollama/model configuration for the cohort.
5. Permit at most one local retry.
6. Record all failures and recovery work.
7. Run the complete diff and sandbox checks.
8. Do not remove a task after seeing its result.
9. Record infrastructure failures separately from model failures.
10. Publish the denominator and number of attempts.

The runner resolves the declared suite and fixtures under `--repo/evals`; it
does not silently evaluate a different checkout when `--repo` points elsewhere.

For model comparisons, record the Ollama model tag, quantization, runtime
version, hardware, context limit, temperature or sampling settings, and
benchmark date. Repeat cases when measuring variance; a single successful
smoke run is not a reliability estimate.

The primary `bounded-50` suite uses target-definition ranges such as
`tasks.py:70-71` and `tests/test_helpers.py:13-14` for its eligible cases. Where
the expected behavior is encoded in a focused test, a case may also provide a
small read-only assertion range from that test file. This keeps the local task
genuinely bounded while supplying the smallest sufficient behavioral evidence;
those files remain outside the write scope. Complete-file context remains
supported when the whole allowed file is intentionally supplied.
The supervisor rejects a whole-file response when only a line range or excerpt
was supplied. For the small-model compatibility path, it may accept one target
definition in the `files` field only when it parses and has the same top-level
syntax shape as the declared range; the supervisor then generates the checked
diff. This compatibility adapter is currently Python-only; non-Python ranged
tasks must return a unified diff. Extra definitions, syntax errors, or path
mismatches remain failures.

## 7. Token accounting

Count Codex usage from actual provider/runtime telemetry when available. If
an estimate is required, label it as an estimate and use the same accounting
rule for both modes.

For each task, record:

~~~yaml
codex_tokens:
  baseline_implementation:
  delegation_or_decomposition:
  review:
  repair:
  recovery:
  total_with_delegation:
~~~

The local model's tokens and compute are separate measurements:

~~~yaml
local_runtime:
  model:
  input_tokens:
  output_tokens:
  wall_clock_seconds:
  cpu_or_gpu_seconds:
~~~

Per task:

~~~text
baseline_codex_tokens
delegated_codex_tokens
net_codex_tokens_saved
net_codex_token_reduction
frontier_token_leverage
~~~

Do not count local-model token savings as Codex savings. Do not claim a token
reduction if the local result required enough Codex repair to erase it.

## 8. Required per-task record

Store a machine-readable record for every attempt and a final task summary. The
runner must retain attempt history, sandbox mode, and an attestation that the
caller's main worktree was unchanged; a final status alone is not evidence.

Suggested schema:

~~~yaml
eval_id:
task_id:
category:
eligibility: eligible | blocked_expected | invalid_fixture
model:
ollama_host:
runtime:
hardware:
attempts:
  - attempt:
    status:
    verification_passed:
    files_changed:
    scope_violation:
    scope_reviewed:
    scope_review_basis:
    scope_review:
    wall_clock_seconds:
    local_input_tokens:
    local_output_tokens:
expected_behavior:
accepted:
first_attempt_accepted:
bounded_acceptance:
substantial_codex_repair:
blocked_result_correct:
codex_tokens:
  triage: 200
  baseline:
  delegation:
  review:
  repair:
  recovery:
  total_with_delegation:
codex_review_minutes:
failure_reason:
notes:
~~~

Keep the raw patch, verification output, and diff evidence linked to the
record. A summary without the underlying evidence is not a benchmark result.

The executable runner accepts an optional `--economics PATH` JSON file. Its
`source` must be either `codex-telemetry` or `estimate`, and its `cohort` must
identify the exact matched run. The runner checks the cohort identity against
the executed suite, fixture digest, repository identity, and model. Its `tasks`
object is keyed by case id and must provide these fields for every eligible task:

~~~json
{
  "source": "codex-telemetry | estimate",
  "cohort": {
    "backend": "codex-ollama",
    "suite": "bounded-50",
    "fixture_digest": "sha256 of cases and fixtures",
    "repository_identity": "git:<commit> or working-tree:<digest>",
    "model": "qwen3:4b"
  },
  "tasks": {
    "case-id": {
      "baseline_codex_tokens": 20000,
      "triage_codex_tokens": 200,
      "delegation_codex_tokens": 2000,
      "review_codex_tokens": 1000,
      "repair_codex_tokens": 0,
      "recovery_codex_tokens": 0,
      "baseline_seconds": 120,
      "triage_seconds": 5,
      "delegation_seconds": 0,
      "review_seconds": 30,
      "repair_seconds": 0,
      "recovery_seconds": 0,
      "scope_reviewed": true,
      "substantial_codex_repair": false
    }
  }
}
~~~

The runner computes net Codex savings, token reduction, Frontier Token
Leverage, and paired wall-clock overhead. When supplied, triage decision tokens
and seconds are included in the delegated total. Delegated wall-clock time
includes the local run plus the supplied triage/delegation/review/repair/recovery time;
it must not omit review work. Missing economics or repair/scope-review
classification are reported as `NOT_AVAILABLE` or `INCOMPLETE`; they are never
replaced with a guessed pass. `agent-relay eval` exits zero only when the complete
`mvp_gate` is `PASS`; case-level success with unevaluated economics is still a
non-passing benchmark result.

For an already recorded full run, the executable repricing path is:

~~~powershell
agent-relay reprice `
  --run-report .\evals\results\run.json `
  --economics .\evals\results\economics.json `
  --manifest-mode thin `
  --sample 5
~~~

The repricer never calls a model. It carries forward the matched baseline and
repair/recovery costs, then replaces the old per-task frontier response with
the compact handoff plus selected artifact reviews. It emits sensitivity for
`full`, `contract`, `thin`, and `none` manifest assumptions. Every result is
`ESTIMATED` unless the source records provider telemetry; this is the
executable bridge from the packet-reduction submetric to the net-savings
estimate, not a substitute for a new matched Codex-only run.
The Ollama report also records model runtime counters when the API returns
them, including prompt/output evaluation counts and retry count. The result's
`attempt_history` is the accounting source: it includes retryable provider or
Codex-harness failures that occur before sandbox creation as well as patch and
verification attempts inside the sandbox. Runtime usage is summed across that
history so a failed first call cannot make delegation savings look better than
they were. Legacy records without this history remain explicitly estimated.

## 14. Authoritative fixed-lane Qwen3.5:4B cohort (2026-08-17)

The authoritative current cohort is the clean run at
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-deterministic2-20260817`.
It used Codex CLI `0.87.0`, Ollama `0.32.13` at
`http://localhost:11434`, and the installed exact model tag `qwen3.5:4b`
with digest
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`.
The Codex compatibility lane used `ollama-chat`, provider `ar-ollama`,
`AR_CODEX_NUM_CTX=8192`, disabled reasoning, stripped tools, compact prompts,
`AR_CODEX_OUTPUT_SCHEMA=false`, same-model retry, `AR_CODEX_TEMPERATURE=0`,
and `AR_CODEX_SEED=17`. No implicit model pull occurred.

Immediately before the cohort, `agent-relay doctor --codex-smoke --model qwen3.5:4b
--json` returned `ok: true` in one attempt (14.8 seconds), with the same host,
model, provider, context bound, temperature, seed, and no-pull policy. The
probe is lane-health evidence only; the cohort below is the quality evidence.

The checked-in fixture preflight passed before model execution: all 45 eligible
oracle patches applied, their paths matched `expected_files`, and every
`insert_after` oracle matched its declared context range. This closes the
earlier benchmark-fixture defect in which eight test-generation patches all
inserted at the final test.

~~~text
eligible tasks:              45
bounded acceptance:          39/45 = 86.67%
verification after max retry:39/45 = 86.67%
first-attempt acceptance:     25/45 = 55.56%
expected BLOCKED correct:      5/5 = 100%
scope violations observed:    0 (full attestation incomplete)
substantial repairs:           not evaluated
main worktrees unchanged:      true
Codex tool execution share:    0%
MVP quality checks:            acceptance and verification PASS
overall MVP gate:              NOT_EVALUATED
~~~

The six failed eligible cases were `benchmark-parse-bool`,
`benchmark-nonnegative-count`, `benchmark-test-dedupe`,
`benchmark-format-labels`, `benchmark-add-headers`, and `benchmark-scale-int`.
They represent malformed/stale patches, a fail-closed syntax-shape rejection,
an invalid insert-after candidate, and semantic verification failures; they were
not counted as successes. The accepted 39/45 result clears the 80% acceptance
and 85% verification thresholds, but it is not an MVP pass because the run has
no complete task-aware scope attestation and no matched Codex-only baseline.

The full compact report is
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-deterministic2-20260817\run.json`;
the retained full per-case evidence is
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-deterministic2-20260817\full-records.json`.
Its proof packet estimate was 51,911 full-report tokens versus 3,147 compact
handoff tokens, or 93.94% response-packet compaction (3,698 tokens including
the selected review artifacts). This is a same-run response-size estimate, not
Codex token savings: it does not include parent decomposition, review, repair,
or a matched Codex-only baseline.
Provider token counters were zero, so this run supplies **no measured token
savings percentage**. The honest current answer remains: quality is above the
acceptance/verification thresholds, while token savings, scope-review
completion, repair rate, and paired wall-clock economics remain unproven.

### 14.1 Evaluator hardening and direct Codex baseline probe (2026-08-17)

The evaluator now performs a conservative task-aware diff review in
`evals/scope_review.py`. Path containment remains mandatory, but a patch is not
marked reviewed merely because it edits an allowed file: ranged tasks must keep
their changed hunks inside the declared context, and `insert_after` tasks must
stay at the declared anchor. Missing ranged context remains incomplete rather
than being guessed safe. The review also records its basis and reasons in each
case record.

This hardening exposed and fixed a real orchestration defect: a valid
`insert_after` unified diff was being sent through append-only recovery and
moved to end-of-file. Recovery now runs only when the original diff is not
applicable. The deterministic control was rerun after the fix:

~~~text
artifact:                  C:\Users\stanc\AppData\Local\Temp\ar-fixture-bounded-50-scope-review-v4-20260817.json
eligible acceptance:       45/45 = 100%
verification:              45/45 = 100%
expected BLOCKED correct:   5/5 = 100%
scope violations:          0 observed
task-aware scope reviewed: 35/45 eligible tasks
scope gate:                NOT_EVALUATED (10 legacy tasks lack ranged context)
economics:                 NOT_AVAILABLE (fixture control)
~~~

This is orchestration/control evidence, not local-model quality or token
savings evidence. The 35 reviewed tasks use path and context/oracle-envelope
evidence; the remaining 10 are intentionally not promoted to a clean scope
denominator until their contracts gain ranged context and the model cohort is
rerun.

A separate direct Codex-only baseline probe was run with Codex CLI
`0.147.0-alpha.1.2`, model `gpt-5.6-luna`, the app-managed executable, an
ephemeral `CODEX_HOME` containing only the authenticated session, ignored user
configuration, and explicit `danger-full-access` inside the disposable
fixture sandbox. Artifact:
`C:\Users\stanc\AppData\Local\Temp\ar-codex-baseline-minimal-final-d00b26d419294eb298753f74400ad4aa`.
It completed the first `negative-timeout` task in one attempt: 1/1 accepted,
1/1 independently verified, 0 path violations, 38.26 seconds end to end, and
provider-reported usage of 137,130 input plus 941 output tokens (138,071
total). Its scope review is incomplete because that legacy task has no ranged
context. The artifact is a baseline probe, not a complete matched economics
cohort; it must not be converted into a token-savings percentage.

The probe also showed why the isolated baseline matters: a prior run inheriting
the full desktop Codex configuration consumed about 190,349 tokens and 77.8
seconds for the same one-case shape. That run is diagnostic and discarded from
the baseline comparison because unrelated MCP/skill startup context polluted
the measurement.

### 14.2 Current Codex CLI Responses-lane smoke after transport hardening (2026-08-17)

The app-managed Codex CLI `0.147.0-alpha.1.2` rejects the historical
`wire_api = "chat"` setting, so the current default is `wire_api = "responses"`.
The first current-CLI probe reached Ollama but passed Responses requests through
unchanged. Ollama then spent 61.26 seconds and 255,899 Codex input tokens across
two attempts while Qwen repeatedly tried malformed shell edits. That diagnostic
is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-codex0147-smoke-20260817\doctor.json`;
it is not cohort quality evidence.

The proxy now applies the Responses-native equivalent of the bounded Chat lane:
`reasoning: {effort: "none"}`, tool-schema removal, the compact no-tools
contract, `max_output_tokens`, and deterministic sampling controls. The fresh
smoke artifact is
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-codex0147-smoke-rewrite-20260817\doctor.json`.
It used the installed exact Qwen3.5:4B digest
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`, Ollama
`0.32.13`, `AR_CODEX_NUM_CTX=8192`, `AR_CODEX_NUM_PREDICT=2048`, temperature
`0`, seed `17`, no model pull, and the disposable inner `danger-full-access`
worktree. It completed one attempt with a verified `reported_files` result:

~~~text
smoke status:                 SUCCESS
attempts:                     1
end-to-end seconds:           4.70
Codex wall-clock seconds:     3.69
Codex input + output tokens:  5,453 + 65 = 5,518
Responses requests rewritten: 1
reasoning disabled:           true
tool schemas stripped:        true
prompt contract compacted:    true
model pull detected:          false
~~~

This is current harness-health and transport-efficiency evidence, not a matched
Codex-only token-savings percentage. The direct comparison above is a useful
regression signal, but only a paired 50-task baseline plus repair/review ledger
can satisfy the MVP economics gate.

## 14.3 Matched current-CLI Qwen3.5:4B cohort and baseline (2026-08-17)

The current matched comparison uses the exact installed `qwen3.5:4b` model,
not a generic Qwen tag:

~~~text
model:              qwen3.5:4b
Ollama:             0.32.13 at http://localhost:11434
model digest:       2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd
Codex CLI:          0.147.0-alpha.1.2
wire API:           Responses
provider ID:        ar-ollama
context/predict:    8192 / 2048
temperature/seed:   0 / 17
reasoning/tools:    disabled / stripped
retry model:        qwen3.5:4b
fixture digest:     1fba5990bd9bd810f1df1c009bfbfdf17c98164ecfaa987f101d321a71be5fc3
cohort identity:    working-tree:2a506d62a54e02bedc48ffc3cbbe9183295814f16afe227ec06f275308d83aed
~~~

The authoritative delegated run is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-codex0147-rewrite-v8-20260817\run.json`,
with full per-attempt records at
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-codex0147-rewrite-v8-20260817\artifacts\full-records.json`:

~~~text
eligible tasks:                 45
first-attempt acceptance:       44/45 = 97.78%
bounded acceptance:             45/45 = 100%
verification pass:              45/45 = 100%
scope violations:               0/45 = 0%
task-aware scope reviewed:      45/45
expected BLOCKED correct:        5/5 = 100%
main worktrees unchanged:       true
model pulls:                    0
case-level status:              PASS
MVP gate:                       NOT_EVALUATED (parent economics unpriced)
~~~

The direct Codex-only baseline uses the same suite, fixture digest, repository
identity, current app-managed Codex binary, and `gpt-5.6-luna`. Its report is
retained at
`C:\Users\stanc\AppData\Local\Temp\ar-codex-baseline-bounded-50-codex0147-v2-20260817\run.json`.
It captured real `turn.completed` usage events:

~~~text
eligible tasks:                 45
bounded acceptance:             36/45 = 80%
verification pass:              45/45 = 100%
range-scope violations:         9/45
Codex input tokens:              5,018,462
Codex output tokens:             40,357
Codex total tokens:              5,058,819
summed end-to-end seconds:       1,439.11
~~~

The nine baseline scope violations and the one semantically incorrect baseline
patch are retained as outcomes, not removed from the denominator. The baseline
is therefore a real cost reference, but not a claim that direct Codex is a
perfect quality oracle.

### Economics status

The matched economics ledger is
`evals/results/qwen35-4b-codex0147-bounded-50-v8-economics-estimate.json`, and
the report-only evaluation is
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-codex0147-rewrite-v8-20260817\run-with-economics-v2.json`.
It is deliberately marked `source: estimate`:

~~~text
real direct-Codex baseline:       5,058,819 tokens
thin task manifest estimate:      2,879 tokens
compact handoff estimate:         2,353 tokens
selected review artifact estimate:  373 tokens
packet-priced delegated total:    5,605 tokens
packet-only reduction estimate:   99.889%
packet-only leverage estimate:    901.55x
conservative retry proxy:          1/45 = 2.22%
MVP economics gate:               NOT_EVALUATED
~~~

The `99.889%` figure is not the project's measured savings claim. It prices
only the compact task manifest, compact handoff, and selected review artifact
packet. The outer Codex supervisor's triage/delegation decisions, review of
every accepted patch, frontier repair/recovery, and review time were not
captured in this evaluator run. The 286,153 tokens reported by the local
Qwen/Codex harness are recorded separately and are explicitly excluded from
Codex savings. A measured MVP economics pass requires those outer-supervisor
costs to be captured as `source: codex-telemetry` on the same cohort.

This packet-only result is retained as a sensitivity estimate. The complete
project criterion is evaluated by the measured supervisor run below.

### 14.4 Measured frontier-supervisor economics (2026-08-17)

To close the economics gate, one real current Codex CLI process performed the
50-task triage pass, one real Codex CLI process performed the batch review of
all 45 delegated candidates, and one real Codex CLI process repaired the one
candidate rejected by review (`exact-logging`). Every process emitted
`turn.completed` usage telemetry. Triage and review ran read-only; the repair
ran in a disposable fixture worktree. The supervisor evidence is retained at
`C:\Users\stanc\AppData\Local\Temp\ar-frontier-supervisor-bounded-50-codex0147-20260817\supervisor.json`,
with the repair evidence at
`C:\Users\stanc\AppData\Local\Temp\ar-frontier-supervisor-bounded-50-codex0147-20260817\repair-exact-logging.json`.

The measured economics ledger is
`evals/results/qwen35-4b-codex0147-bounded-50-v8-economics-measured.json` and
the passing report is
`C:\Users\stanc\AppData\Local\Temp\ar-qwen35-bounded-50-codex0147-rewrite-v8-20260817\run-with-economics-measured-v3.json`:

~~~text
Codex-only baseline:             5,058,819 tokens / 1,439.11 seconds
Codex triage:                       25,158 tokens /    26.97 seconds
Codex batch review:                 34,811 tokens /    33.89 seconds
Codex repair:                       72,066 tokens /    23.89 seconds
Measured delegated Codex total:   132,035 tokens
Net Codex tokens saved:          4,926,784
Net Codex token reduction:       97.390%
Frontier Token Leverage:           37.31x
Delegated end-to-end time:          461.63 seconds
Paired wall-clock overhead:       -67.92%
Frontier repair rate:                1/45 = 2.22%
MVP gate:                           PASS
~~~

The quality result remains 45/45 bounded acceptance after at most one local
retry, 45/45 verification, 0/45 scope violations, 45/45 scope-reviewed, and
5/5 expected BLOCKED decisions. The batch review initially accepted 44/45;
the one rejected candidate received the measured Codex repair above and then
passed verification. The 286,153 Qwen/Codex-harness local tokens remain a
separate local-runtime measurement and are not included in the Codex savings
calculation.

This is the first cohort in this repository that satisfies all GOAL.md MVP
gates with measured frontier telemetry. The result is specific to the exact
Qwen3.5:4B tag, current Codex CLI, Ollama host, prompt/transport settings, and
50-task fixture identity recorded above; it is not a universal savings claim
for every repository or model.

## 9. Aggregate reporting

Every report must show counts as well as percentages.

Recommended summary:

| Metric | Numerator | Denominator | Result | Target | Gate |
| --- | ---: | ---: | ---: | ---: | --- |
| First-attempt acceptance |  | eligible |  | diagnostic |  |
| Bounded acceptance |  | eligible |  | >=80% |  |
| Verification pass after max 1 retry |  | eligible |  | >=85% |  |
| Scope violations |  | all delegated |  | <1% |  |
| Substantial Codex repair |  | eligible |  | <15% |  |
| Net Codex token reduction |  | matched tasks |  | >=50% |  |
| Paired wall-clock overhead |  | matched tasks |  | <=25% |  |
| Correct BLOCKED results |  | blocked_expected |  | report |  |

Also publish per-model results. Do not combine models in a way that hides a
weak model or cherry-picks only successful task categories.

`agent-relay eval` keeps the case-level `status` separate from `mvp_gate`. The initial
10-case `bounded-basic` suite can pass its orchestration/correctness checks,
but `mvp_gate` remains `NOT_EVALUATED` or `FAIL` until the 50-task cohort and
matched economics records exist.

The automated scope check proves changed-path containment. Because unrelated
edits inside an allowed file require task-aware review, the full scope gate
also requires `scope_reviewed: true` for every delegated record. Verification
commands are trusted contract inputs, not an OS-level security sandbox; do not
use untrusted commands as a safety boundary.

For a 50-task suite with 45 eligible implementation tasks:

- bounded acceptance of 80% requires at least 36 accepted eligible tasks
- verification pass rate of 85% requires at least 39 passing eligible tasks
- <1% scope violations requires zero violations across the full delegated
  cohort
- repair below 15% allows at most 6 substantial repairs among 45 eligible
  tasks

The exact integer thresholds must be recalculated when the denominator changes.

## 10. MVP pass condition

Over a representative benchmark of at least 50 tasks, the selected MVP
configuration must demonstrate:

- **>=80% bounded acceptance** on eligible tasks, with no more than one retry
- **>=50% net Codex token reduction** on matched delegatable tasks
- **<1% scope violations**, ideally zero
- **<15% substantial Codex repair**
- **>=85% verification pass rate** after at most one retry
- **wall-clock completion within 25% of the Codex-only baseline**
- a separately reported and reliable BLOCKED result for unsuitable tasks

The claim is valid only when all core gates pass on the same declared cohort.
A local benchmark score, a successful Ollama request, or a plausible patch is
not sufficient evidence.

## 11. Stretch targets

After the MVP gates pass, aim for:

~~~text
Acceptance rate:          >90%
Net Codex token savings:  >70%
Scope violations:         0%
Codex repair rate:        <5%
Local completion share:   >70% of eligible subtasks
~~~

Define local completion share as:

~~~text
eligible subtasks completed and accepted without Codex code changes
-------------------------------------------------------------------
eligible subtasks assigned to local execution
~~~

## 12. Interpretation

The most important comparison is not which model wins a generic coding
benchmark. It is which configuration produces the most verified local
engineering work per Codex token spent.

A smaller model that completes 88% of Codex-defined microtasks, stays in scope,
runs four times faster, and saves 70% of Codex tokens is more valuable to this
project than a larger model with a higher benchmark score but poor latency,
scope discipline, or review economics.

The north-star remains:

> **Maximize verified local engineering work per Codex token spent.**

## 13. Historical Codex/Qwen3:4B compatibility-lane evidence

The latest live validation uses the custom `ar-ollama` provider in Codex CLI
0.87.0, Ollama 0.12.11 at `http://127.0.0.1:11435`, Qwen 3 4B, the temporary
loopback compatibility proxy, and the default one-retry contract. The proxy
strips unused Chat tool schemas and compacts the provider system message before
forwarding it; Codex remains the execution protocol and the outer sandbox and
verifier remain authoritative. This is observable in each worker runtime as
`codex_tools_stripped`, `codex_prompt_compacted`, `bytes_forwarded`, and
`compat_proxy_stats`.

The capability probe now exercises one bounded retry because a small model can
return a correct summary without a candidate on its first text-only turn:

~~~powershell
agent-relay doctor --host http://127.0.0.1:11435 --model qwen3:4b --codex-smoke --json
~~~

Latest result: `SUCCESS`, 2 attempts, verified `value.py`,
`result_source=reported_files`, and unchanged caller worktree. The final
attempt forwarded 4,269 bytes after proxy compaction and recorded 1,072,855
response bytes. This proves lane health and recovery behavior, not a 50-task
acceptance rate or net Codex-token savings.

The latest three-case `bounded-recovery` run is retained at
`evals/results/codex-ollama-qwen3-4b-recovery-v4.json`, with full records in
`evals/artifacts/codex-ollama-qwen3-4b-recovery-v4/full-records.json`:

~~~text
bounded acceptance:          3/3 = 100%
verification pass:           3/3 = 100%
first-attempt acceptance:    0/3 = 0%
retry acceptances:           3/3
observed scope violations:   0 (full scope attestations still incomplete)
main worktrees unchanged:    true
Codex tool execution share:  0%
packet compaction:           82.87% (4,856 -> 832 estimated response tokens)
net Codex savings:            not measured
MVP gate:                    NOT APPLICABLE (3 tasks, no matched economics)
~~~

This result is a useful recovery/transport signal, not permission to claim the
50-task MVP. It also shows the current tradeoff: the compact local lane is
reliable after one retry but slow (roughly 143–186 seconds per case in this
three-case run) and did not exercise Codex tool edits. The next measured work
is to compare this lane against direct Ollama and a tool-capable lane on the
same matched cohort, then price review and retry costs with Codex telemetry.

