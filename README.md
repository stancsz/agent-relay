# Agent Relay

**Agent Relay** is the event-driven gateway and task router for heterogeneous
AI workers. It routes remote or local work to Codex, Claude, local Qwen, or
Antigravity according to capability, cost, and verification needs.

Unlike a conventional LLM router, which forwards prompts or API calls between
models, Agent Relay routes complete **agent harnesses**: tools, permissions,
workspaces, sandboxes, task contracts, execution policy, and verification move
with the selected runtime.

The repository was formerly called **Local Code Delegate**. `lcd` and
`subagent` remain compatibility command aliases; new usage should prefer
`agent-relay`.

## Unified subagent lanes

This repository is now the single home for bounded subagent work. `lcd` and
`subagent` remain available for compatibility; new integrations should use the
`agent-relay` CLI and the canonical lane names:

| Lane | Role | Default | Proof boundary |
| --- | --- | --- | --- |
| `local-qwen` | local/free mechanical worker | Qwen3.5:4B through Ollama and Codex CLI | disposable sandbox, scope gate, parent reruns checks |
| `claude-task` | primary Claude implementation/team worker | authenticated Claude task bridge; optional Agent Teams | task receipt, workspace lock, Git fingerprint |
| `codex-review` | subscription QA verifier | GPT-5.6 Sol, high reasoning | read-only Codex CLI review receipt |
| `agy-antigravity` | Google-stack scout/planner | Gemini 3.1 Pro, high effort | plan receipt; parent verifies locally |

List lanes with `agent-relay lanes --json`. Run the subscription verifier with
`agent-relay review --repo . --model gpt-5.6-sol --reasoning-effort high --uncommitted`.
That command uses the user's existing Codex CLI login; it does not accept an
API key and it fails explicitly when the model or entitlement is unavailable.

The Claude implementation is vendored under
[`lanes/claude-task/`](lanes/claude-task/) from the former Claude Prime project.
Its native-team and authenticated A2A behavior remains intact; the unified
skill is [`skills/agent-relay/SKILL.md`](skills/agent-relay/SKILL.md).

The evidence-backed routing and the latest local readiness results are recorded
in [`docs/SUBAGENT_ROLES.md`](docs/SUBAGENT_ROLES.md).

Consult the Google-stack specialist with
`agent-relay ask --lane agy-antigravity --repo . --prompt "..." --json`. The
default is plan mode; it is intended for Gemini, Firebase, Android, Google Cloud,
browser/UI, and frontend-specific judgment, not unreviewed patch application.

Agent Relay is a small, Windows-first Python prototype that lets a parent agent
route tightly bounded work to local or hosted specialist workers.

The validated MVP path is:

~~~text
frontier Codex -> triage -> second Codex CLI process -> Ollama -> Qwen3.5:4B
-> disposable Git sandbox -> scope review -> tests -> compact proof packet
~~~

The important distinction is:

- Ollama is the inference server.
- Qwen3.5:4B is the local model.
- Codex CLI is the local execution harness.
- Agent Relay is the supervisor, sandbox, verifier, retry gate, and economics
  ledger.

This is not a claim that a 4B model can replace Codex. It is an experiment in
whether Codex can safely spend fewer frontier-model tokens on small,
mechanical tasks.

## Current status

The codex-ollama lane is the current measured MVP. It starts a second Codex
CLI process with a temporary Ollama-backed provider and keeps the caller's
worktree outside the worker's write path.

| Capability | Status |
| --- | --- |
| Structured task contract and bounded write scope | Implemented |
| Parent triage: DELEGATE, KEEP_LOCAL, or BLOCKED | Implemented |
| Direct Ollama worker | Implemented; useful as a diagnostic/reference lane |
| Codex CLI over Ollama (codex-ollama) | Implemented and measured |
| Disposable Git sandbox, patch capture, scope review, verification, retry | Implemented |
| Compact batch handoff and economics ledger | Implemented |
| Agent Relay skill and Qwen worker prompt kit | Included |
| Claude Code task bridge | Integrated under `lanes/claude-task`; run its capability smoke before use |
| Antigravity CLI specialist | Integrated as `agy-antigravity`; local CLI smoke is required before use |
| DeepSeek Harness over Ollama | Planned; no measured result |

Some older files under evals/ contain historical qwen3:4b runs. They are not
the current model result. The authoritative current cohort uses the exact tag
qwen3.5:4b.

## The measured result

On 2026-08-17, the project completed one matched bounded-50 cohort:

- 50 task contracts total
- 45 eligible bounded tasks
- 5 expected BLOCKED safety cases
- exact model tag qwen3.5:4b
- codex-ollama through Ollama 0.32.13
- Codex CLI 0.147.0-alpha.1.2

The headline result is **97.39% measured frontier Codex-token reduction** for
this cohort and accounting method. It is a measured experiment result, not a
general promise about every repository or task.

### Quality and economics

| Measure | Result |
| --- | ---: |
| First-attempt local acceptance | 44/45 = 97.78% |
| Acceptance after at most one local retry | 45/45 = 100% |
| Verification pass rate | 45/45 = 100% |
| Scope violations | 0/45 = 0% |
| Expected blocked decisions | 5/5 correct |
| Frontier Codex repairs after review | 1/45 = 2.22% |
| Direct Codex-only baseline | 5,058,819 tokens |
| Frontier Codex triage | 25,158 tokens |
| Frontier Codex batch review | 34,811 tokens |
| Frontier Codex repair | 72,066 tokens |
| Total delegated frontier Codex usage | 132,035 tokens |
| Net Codex tokens saved | 4,926,784 |
| Net frontier Codex-token reduction | **97.39%** |
| Frontier Token Leverage | **37.31x** |
| Paired wall-clock overhead | -67.92% in this batch |

The calculation is:

~~~text
baseline Codex tokens                 5,058,819
- triage, review, and repair            132,035
= net Codex tokens saved               4,926,784

4,926,784 / 5,058,819 = 97.39% reduction
4,926,784 / 132,035   = 37.31x Frontier Token Leverage
~~~

The local runtime recorded 286,153 Qwen/Codex-harness tokens. Those tokens are
reported separately and are deliberately excluded from the Codex reduction
number: the KPI measures frontier Codex tokens avoided, not total compute,
electricity, latency, or local-model token generation.

The one repair was not hidden. Review rejected exact-logging because the
candidate placed the logger inside process instead of at module scope. Codex
repaired it in the disposable worktree and the declared test then passed.

The direct Codex-only run is a cost baseline, not a gold-standard oracle. It
accepted 36/45 bounded tasks and produced 9 range-scope violations in that
run. This makes the comparison useful for economics, but it also means the
97.39% number should not be read as proof that local Qwen is better than Codex
at general software engineering.

The full evidence and accounting are in [EVALS.md](EVALS.md), especially the
measured frontier-supervisor section, and in
[the checked-in economics ledger](evals/results/qwen35-4b-codex0147-bounded-50-v8-economics-measured.json).
The project-level gates are summarized in [GOAL.md](GOAL.md).

## How delegation works

LCD treats a delegated task as a contract, not as an open-ended chat:

1. Frontier Codex triages the task and estimates whether delegation can save
   enough frontier tokens.
2. LCD sends only the bounded objective, allowed files, read-only context, and
   deterministic verification commands to the local execution lane.
3. The worker runs in a disposable Git worktree or fixture sandbox.
4. LCD captures the candidate patch or changed-file content.
5. The outer verifier checks patch applicability, changed-file scope, and the
   declared tests.
6. A single bounded retry may run when the failure is recoverable.
7. Frontier Codex receives a compact result containing status, changed files,
   verification evidence, patch hashes, and blockers. Full patches and raw
   transcripts remain artifacts for review.

The outer supervisor remains authoritative. A worker's self-report is not
accepted as proof.

### Good delegation candidates

Delegate when the task is:

- mechanical and local, usually one to three files;
- explicit about the required behavior and allowed files;
- covered by a deterministic test, lint, or typecheck command;
- reversible and free of external side effects;
- cheap for the frontier to review relative to doing the edit itself.

Keep work in frontier Codex when it involves architecture, ambiguous product
requirements, authentication or security policy, broad migrations, difficult
debugging, data loss, production side effects, or missing verification. A
small model should not be asked to decide what the system ought to be.

The default triage gate is fail-closed. A task must have a recognized
task_kind, empty risk_flags, a small write scope, deterministic verification,
and a projected delegation leverage of at least 2x. A task that fails the gate
is kept local or reported as blocked; it is not silently sent to Qwen.

## Quick start on Windows

### 1. Install the project

~~~powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\Activate.ps1
~~~

Requirements are Python 3.11+, Git, a working Codex CLI installation, and
Ollama.

### 2. Install and prewarm the exact model

~~~powershell
ollama pull qwen3.5:4b
ollama ps
~~~

Ollama normally listens on http://localhost:11434. If it is elsewhere, set
LCD_CODEX_OLLAMA_HOST below. The worker does not pull a model during a
delegation; an absent model is a setup failure.

### 3. Configure the Codex-over-Ollama lane

The defaults are already conservative, but these settings reproduce the
measured operating profile:

~~~powershell
$env:LCD_CODEX_OLLAMA_HOST = "http://localhost:11434"
$env:LCD_CODEX_MODEL = "qwen3.5:4b"
$env:LCD_CODEX_LOCAL_PROVIDER = "ollama-chat"
$env:LCD_CODEX_PROVIDER_ID = "lcd-ollama"
$env:LCD_CODEX_WIRE_API = "responses"
$env:LCD_CODEX_REASONING_EFFORT = "low"
$env:LCD_CODEX_TIMEOUT_SECONDS = "180"
$env:LCD_CODEX_IDLE_TIMEOUT_SECONDS = "90"
$env:LCD_CODEX_COMPAT_PROXY = "true"
$env:LCD_CODEX_DISABLE_REASONING = "true"
$env:LCD_CODEX_STRIP_TOOLS = "true"
$env:LCD_CODEX_COMPACT_PROMPT = "true"
$env:LCD_CODEX_NUM_CTX = "8192"
$env:LCD_CODEX_NUM_PREDICT = "2048"
$env:LCD_CODEX_TEMPERATURE = "0"
$env:LCD_CODEX_SEED = "17"
$env:LCD_CODEX_OUTPUT_SCHEMA = "false"
$env:LCD_CODEX_RETRY_MODEL = "qwen3.5:4b"
~~~

If codex on PATH is stale, point the worker at the intended executable:

~~~powershell
$env:LCD_CODEX_BIN = "C:\path\to\codex.exe"
~~~

The measured cohort used the current app-managed Codex executable, not a
legacy --oss --local-provider mode.

### 4. Run a capability smoke

~~~powershell
lcd doctor --codex-smoke --model qwen3.5:4b --json
~~~

Do not start a long evaluation until this reports success. The smoke checks
the Codex executable, temporary provider, Ollama route, exact model, inner
sandbox, patch capture, and outer verification.

### 5. Run a bounded task or small evaluation

~~~powershell
lcd triage --task .\task.json --avoided-tokens 1800 --spent-tokens 600 --json
lcd delegate --backend codex-ollama --require-triage --task .\task.json --repo C:\path\to\repo --json
lcd eval --backend codex-ollama --model qwen3.5:4b --suite bounded-basic --aggregate --sample 3 --json
~~~

The small evaluation is a correctness/transport smoke, not the 97.39%
economics result. A complete matched cohort, parent triage, frontier review,
and repair ledger are required before claiming net Codex savings.

## Task contract

The minimum useful contract is explicit about scope and verification:

~~~json
{
  "task_id": "config-017",
  "task_kind": "bounded_bugfix",
  "risk_flags": [],
  "objective": "Add validation for negative timeout values.",
  "allowed_files": [
    "src/config.py",
    "tests/test_config.py"
  ],
  "context": [],
  "requirements": [
    "Negative timeout values raise ValueError.",
    "Zero remains valid."
  ],
  "constraints": [
    "Do not change the public API.",
    "Do not touch files outside allowed_files."
  ],
  "verification": [
    "pytest tests/test_config.py"
  ],
  "success_criteria": [
    "All verification commands exit with code 0.",
    "Only allowed files changed."
  ]
}
~~~

context is read-only evidence. Only allowed_files may be changed.
Verification commands are trusted task inputs; they are not an OS-level
security boundary.

## CLI surfaces

The implemented command surfaces are:

~~~text
agent-relay lanes    List the canonical local-qwen, claude-task, codex-review, agy-antigravity lanes
agent-relay review   Run the read-only Codex subscription QA verifier
agent-relay ask      Consult the AGY Google-stack specialist in plan mode
agent-relay doctor   Check Ollama, the exact model, and optional Codex smoke.
agent-relay triage   Decide DELEGATE, KEEP_LOCAL, or BLOCKED.
agent-relay delegate Run one bounded task through Ollama or codex-ollama.
agent-relay eval     Run a declared suite and produce quality/evidence metrics.
agent-relay baseline Run the matched direct-Codex comparison lane.
agent-relay batch    Run independent tasks and return one compact handoff.
agent-relay reprice  Estimate compact-handoff economics for a recorded run.

`subagent` and `lcd` remain compatibility aliases for these command surfaces.
~~~

lcd reprice is an estimate of frontier response/handoff accounting. It is not
the same as the measured frontier Codex telemetry in the result above. Do not
use compact packet-size savings as a substitute for a complete
triage/review/repair ledger.

For deterministic orchestration checks without a model:

~~~powershell
lcd eval --backend fixture --suite bounded-50 --aggregate --sample 5 --json
~~~

The fixture backend proves task, sandbox, scope, retry, and reporting logic.
It is not evidence that Qwen can solve the tasks or that Codex tokens were
saved.

## Skill and prompt kit

The unified skill is in
[skills/agent-relay/SKILL.md](skills/agent-relay/SKILL.md). It defines the common
lane vocabulary and the worker-versus-verifier authority boundary.

The reusable Agent Relay skill is in
[skills/agent-relay/SKILL.md](skills/agent-relay/SKILL.md). Its Qwen worker
guide and prompt kit are in
[skills/agent-relay/references/qwen-worker.md](skills/agent-relay/references/qwen-worker.md)
and [skills/agent-relay/references/qwen-prompts.md](skills/agent-relay/references/qwen-prompts.md).
The packaged artifact is [agent-relay.skill](agent-relay.skill).

The skill's job is to help frontier Codex decide when delegation is worth
doing, form a bounded contract, invoke the Codex CLI/Qwen lane, and consume
proof before opening large artifacts. It does not make unsafe tasks safe and
does not replace independent verification.

## Safety boundary

The inner Codex CLI currently uses danger-full-access because the installed
non-interactive Codex release rejects shell execution under workspace-write
with approval_policy=never. That setting is used only inside a disposable Git
worktree or fixture sandbox. The outer process still owns patch application,
allowed-file checking, verification, and the caller's real worktree.

This is not an OS-level security sandbox. Do not point it at an untrusted
model, untrusted verification commands, secrets, or a valuable repository
until you have supplied a stronger isolation layer. The benchmark preserved
the main worktree, but that is an observed property of that run, not a
security guarantee for arbitrary host configuration.

## Honest limitations

The current result is promising but narrow:

- It is one 50-contract cohort, dominated by small deterministic tasks.
- The triage, review, and repair costs came from real Codex CLI telemetry, but
  they were captured as batch supervisor calls. Interactive production usage
  may spend more frontier tokens.
- The baseline was a real direct-Codex run, but it was not a perfect quality
  oracle and had its own scope/acceptance failures.
- Local-model tokens, compute cost, power, and model-download/storage cost are
  excluded from the Codex-token KPI.
- Passing tests do not prove semantic correctness; the measured review caught
  a structural issue that tests alone did not express.
- No claim has been measured for Claude task quality, larger repositories,
  broad refactors, or ambiguous work. The Codex review lane is a verifier
  adapter, not a claim that every installed account has GPT-5.6 Sol access.
- A cold model, different Codex release, different prompt settings, or
  different task mix can materially change the numbers.

The practical claim is therefore modest: **bounded local delegation can be
useful and highly token-efficient when frontier Codex chooses narrow tasks and
still reviews verified evidence.** The project has not demonstrated that
delegation is the right choice for arbitrary coding work.

## Repository layout

~~~text
src/agent_relay/       Compatibility Python module containing the
                                worker, Codex harness, proxy, sandbox, verifier,
                               task contract, triage, batch, and CLI.
lanes/claude-task/             Vendored Claude native-team/A2A worker lane.
skills/agent-relay/              Unified lane skill and routing contract.
skills/agent-relay/            Unified skill and worker prompt references.
evals/cases/                   Bounded benchmark contracts and patch fixtures.
evals/runner.py                Evaluation execution and gate accounting.
evals/results/                 Checked-in result ledgers.
tests/                         Unit and integration tests for the harness.
GOAL.md                        Product objective and success criteria.
EVALS.md                       Evaluation protocol and evidence report.
~~~

## Development

Run the repository tests from the project root:

~~~powershell
.\.venv\Scripts\python.exe -m pytest
~~~

When changing the delegation protocol, update the task contract, verifier,
tests, skill prompt kit, and EVALS.md together. A passing worker test is not
enough to establish an economics result; rerun a matched cohort and retain
the full evidence artifacts.

## Bottom line

Agent Relay is a working, measured MVP for one specific question:

> Can frontier Codex delegate bounded low-level coding work to Qwen3.5:4B
> through Ollama, receive a compact verified result, and materially reduce
> frontier Codex token usage?

For the recorded cohort, the answer is **yes**: 97.39% net frontier Codex
token reduction, 100% bounded acceptance after one retry, 0 scope violations,
and 2.22% frontier repair. The answer is **not yet** “yes for coding in
general.”
