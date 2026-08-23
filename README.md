# Agent Relay

**Agent Relay** is the event-driven gateway and task router for heterogeneous
AI workers. It routes remote or local work to Codex, Claude, local Qwen, or
Antigravity according to capability, cost, and verification needs.

Unlike a conventional LLM router, which forwards prompts or API calls between
models, Agent Relay routes complete **agent harnesses**: tools, permissions,
workspaces, sandboxes, task contracts, execution policy, and verification move
with the selected runtime.

`agent-relay` is the canonical command. The `subagent` alias remains available
for existing integrations; new usage should prefer `agent-relay`.

## Unified subagent lanes

This repository is the single home for bounded subagent work. New integrations
should use the `agent-relay` CLI and the canonical lane names:

| Lane | Role | Default | Proof boundary |
| --- | --- | --- | --- |
| `local-qwen` | local/free mechanical worker | Qwen3.5:4B through Ollama and Codex CLI | disposable sandbox, scope gate, parent reruns checks |
| `claude-task` | Claude implementation worker | authenticated Claude Orchestrator; optional Agent Teams | disposable Agent Relay sandbox, task receipt, workspace lock, Git fingerprint |
| `claude-mcp` | remote Claude MCP convenience worker | existing streamable-HTTP Claude MCP server (JSON or SSE) | remote output/transport receipt; no local sandbox claim |
| `sol-reviewer` | Sol high independent read-only reviewer | GPT-5.6 Sol, high reasoning | read-only Codex CLI review receipt |
| `agy-antigravity` | Google-stack scout/planner | Gemini 3.1 Pro, high effort | plan receipt; parent verifies locally |

List configured lanes with `agent-relay lanes --json`. Check local executables,
Ollama health, and Claude's ephemeral bridge prerequisites with
`agent-relay lanes --check --json` or `agent-relay doctor --all`. If an
`AR_CLAUDE_A2A_SERVER_URL` is configured, its `/health` endpoint is also
probed. Read readiness as `ready`, `degraded`, `blocked`, or `unknown`; an
executable-only check is `unknown`, not proof of entitlement. Run the
subscription verifier with
`agent-relay review --repo . --model gpt-5.6-sol --reasoning-effort high --uncommitted`.
That command uses the user's existing Codex CLI login; it does not accept an
API key and it fails explicitly when the model or entitlement is unavailable.

The Claude implementation is vendored under
[`lanes/claude-task/`](lanes/claude-task/) as the `claude-task` backend. Its
native-team and authenticated A2A behavior remains intact; the unified skill is
[`skills/agent-relay/SKILL.md`](skills/agent-relay/SKILL.md), and the dedicated
orchestration skill is `claude-orchestrator`.

The evidence-backed routing and the latest local readiness results are recorded
in [`docs/SUBAGENT_ROLES.md`](docs/SUBAGENT_ROLES.md).

The durable coordinator is the next layer above the local adapters. It stores
task envelopes, lifecycle events, leases, Agent Cards, and terminal receipts in
SQLite. The default server binds to loopback; use `AR_RELAY_AUTH_TOKEN` and an
explicit host when exposing it to another machine.

Consult the Google-stack specialist with
`agent-relay ask --lane agy-antigravity --repo . --prompt "..." --json`. The
default is plan mode; it is intended for Gemini, Firebase, Android, Google Cloud,
browser/UI, and frontend-specific judgment, not unreviewed patch application.

The AGY lane uses the standalone Antigravity CLI, not the Antigravity IDE
launcher. Install it with the official installer, then verify it from a fresh
login shell:

~~~text
curl -fsSL https://antigravity.google/cli/install.sh | bash
zsh -lic 'command -v agy; agy --version'
agy -p 'Reply with exactly AGY_CLI_OK.' --output-format json --print-timeout 30s
~~~

The JSON result must contain `status: SUCCESS` and a nonempty `response`. Do
not use `agy chat` or treat an Electron launch with exit code 0 as a delegated
response.

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
| Claude Code task bridge | Integrated as the sandboxed `claude-task` backend; the vendored bridge must be available |
| Existing Claude MCP server | Integrated as the explicit `claude-mcp` remote-output backend; endpoint readiness must be probed |
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

agent-relay treats a delegated task as a contract, not as an open-ended chat:

1. Frontier Codex triages the task and estimates whether delegation can save
   enough frontier tokens.
2. agent-relay sends only the bounded objective, allowed files, read-only context, and
   deterministic verification commands to the local execution lane.
3. The worker runs in a disposable Git worktree or fixture sandbox.
4. agent-relay captures the candidate patch or changed-file content.
5. The outer verifier checks patch applicability, changed-file scope, and the
   declared tests.
6. A single bounded retry may run when the failure is recoverable.
7. Frontier Codex receives a compact result containing status, changed files,
   verification evidence, patch hashes, and blockers. Full patches and raw
   transcripts remain artifacts for review.

The outer supervisor remains authoritative. A worker's self-report is not
accepted as proof.

### Configurable intelligence escalation

Agent Relay does not send every task to a frontier model. It evaluates explicit
policy gates—`plan_end`, `execute`, `review_end`, `recovery`, and `release`—using
operational signals such as risk flags, ambiguity, scope, failed attempts,
missing evidence, and verification state. A matched rule returns one of
`continue`, `consult`, `require_review`, or `block`.

`continue` keeps the task on the bulk worker path. By default, the policy
summons Sol high for a second opinion at `plan_end`, then requires a read-only
high verifier at `review_end`; both profiles are configurable. `consult`
summons a configured high-intelligence planning or recovery profile;
`require_review` requires a read-only high verifier before acceptance; and `block` fails closed
when authority or evidence is insufficient. The policy, profiles, model names,
and reasoning effort are configurable in a versioned JSON file; a high-model
receipt never replaces deterministic tests, scope checks, or workspace proof.
See [`docs/pm/escalation-policy.md`](docs/pm/escalation-policy.md) and the
example at [`config/escalation.example.json`](config/escalation.example.json).
If Sol finds an actionable issue, the consultation receipt returns a bounded
`REVISE_BULK_WORKER_AND_RECHECK` next step. The selected worker—often
`claude-task` for repository-aware work—revises, reruns deterministic checks,
and is consulted again once. Exhaustion yields `HUMAN_REVIEW` or `BLOCKED`, not
an infinite retry loop.

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

## Install on another PC

Agent Relay has two installable pieces:

- The Python package provides the `agent-relay` CLI and has no Python runtime
  dependencies.
- The Codex skill teaches Codex how to use the bounded routing workflow. The
  package embeds the same skill archive, so it can be installed without this
  repository checkout.

From GitHub on Windows:

~~~powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "git+https://github.com/stancsz/agent-relay.git"
.\.venv\Scripts\agent-relay.exe skill install
.\.venv\Scripts\agent-relay.exe lanes --json
~~~

On macOS or Linux, use the equivalent `python3` and `.venv/bin/agent-relay`
paths. To install into a non-default Codex directory, set `CODEX_HOME` or pass
`--destination`. Existing skill files are never overwritten unless `--force`
is explicit:

~~~text
agent-relay skill install --force --json
~~~

The core CLI is portable after Python 3.11+ is installed. Individual lanes have
their own external prerequisites: the local-Qwen lane needs Ollama and Codex
CLI, the Claude lane needs Claude Code/authentication, and the AGY lane needs
the `agy` CLI. Installation does not claim those tools or credentials are
present.

### 2. Install and prewarm the exact model

~~~powershell
ollama pull qwen3.5:4b
ollama ps
~~~

Ollama normally listens on http://localhost:11434. If it is elsewhere, set
AR_CODEX_OLLAMA_HOST below. The worker does not pull a model during a
delegation; an absent model is a setup failure.

### 3. Configure the Codex-over-Ollama lane

The defaults are already conservative, but these settings reproduce the
measured operating profile:

~~~powershell
$env:AR_CODEX_OLLAMA_HOST = "http://localhost:11434"
$env:AR_CODEX_MODEL = "qwen3.5:4b"
$env:AR_CODEX_LOCAL_PROVIDER = "ollama-chat"
$env:AR_CODEX_PROVIDER_ID = "ar-ollama"
$env:AR_CODEX_WIRE_API = "responses"
$env:AR_CODEX_REASONING_EFFORT = "low"
$env:AR_CODEX_TIMEOUT_SECONDS = "180"
$env:AR_CODEX_IDLE_TIMEOUT_SECONDS = "90"
$env:AR_CODEX_COMPAT_PROXY = "true"
$env:AR_CODEX_DISABLE_REASONING = "true"
$env:AR_CODEX_STRIP_TOOLS = "true"
$env:AR_CODEX_COMPACT_PROMPT = "true"
$env:AR_CODEX_NUM_CTX = "8192"
$env:AR_CODEX_NUM_PREDICT = "2048"
$env:AR_CODEX_TEMPERATURE = "0"
$env:AR_CODEX_SEED = "17"
$env:AR_CODEX_OUTPUT_SCHEMA = "false"
$env:AR_CODEX_RETRY_MODEL = "qwen3.5:4b"
~~~

If codex on PATH is stale, point the worker at the intended executable:

~~~powershell
$env:AR_CODEX_BIN = "C:\path\to\codex.exe"
~~~

The measured cohort used the current app-managed Codex executable, not a
legacy --oss --local-provider mode.

### 4. Run a capability smoke

~~~powershell
agent-relay doctor --codex-smoke --model qwen3.5:4b --json
~~~

Do not start a long evaluation until this reports success. The smoke checks
the Codex executable, temporary provider, Ollama route, exact model, inner
sandbox, patch capture, and outer verification.

When Claude Code is available, run the separate Claude gates as well:

~~~powershell
py -3 scripts\probe_claude_lane.py --timeout 40
py -3 scripts\smoke_claude_task.py
py -3 scripts\smoke_claude_cancellation.py
py -3 scripts\smoke_claude_interruption.py
~~~

The first command proves authenticated MCP capability discovery. The second
proves a real bounded Claude edit, parent-owned verification, target-file scope,
and preservation of the caller worktree. A green bridge health check alone is
not task-completion evidence. The cancellation smoke exercises the full worker
plane and requires `cancel_requested` to become a worker-confirmed `cancelled`
receipt with `execution_stopped: true`.
The interruption smoke additionally kills a separate worker after `running`,
waits for lease expiry, and requires a fresh worker to return a verified
success receipt.

### 5. Run a bounded task or small evaluation

~~~powershell
agent-relay triage --task .\task.json --avoided-tokens 1800 --spent-tokens 600 --json
agent-relay delegate --backend codex-ollama --require-triage --task .\task.json --repo C:\path\to\repo --json
agent-relay eval --backend codex-ollama --model qwen3.5:4b --suite bounded-basic --aggregate --sample 3 --json
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
agent-relay lanes    List the canonical local-qwen, claude-task, claude-mcp, sol-reviewer, agy-antigravity lanes
agent-relay serve    Run the durable SQLite-backed coordinator
agent-relay mcp      Expose the durable coordinator through MCP tools
agent-relay agents   List or register worker Agent Cards
agent-relay submit   Submit one idempotent bounded task
agent-relay chain-submit Submit one durable, predecessor-gated follow-up step
agent-relay watch    Poll or stream a task until a terminal state
agent-relay inspect  Read one durable task envelope and event history
agent-relay inspect-chain Read one durable chain and its ordered steps
agent-relay watch-chain Poll a durable chain until its materialized steps finish
agent-relay cancel   Request cancellation without claiming execution stopped
agent-relay resume   Requeue a waiting task after interruption
agent-relay worker   Run the reference local-Qwen or Claude worker loop
agent-relay review   Run the read-only Codex subscription QA verifier
agent-relay escalate Evaluate the configurable intelligence gate
agent-relay consult  Summon the configured high Codex profile when required
agent-relay ask      Consult the AGY Google-stack specialist in plan mode
agent-relay doctor   Check Ollama, the exact model, and optional Codex smoke.
agent-relay triage   Decide DELEGATE, KEEP_LOCAL, or BLOCKED.
agent-relay delegate Run one bounded task through Claude/claude-task by default; alternatives are explicit.
agent-relay eval     Run a declared suite and produce quality/evidence metrics.
agent-relay baseline Run the matched direct-Codex comparison lane.
agent-relay batch    Run independent tasks and return one compact handoff.
agent-relay reprice  Estimate compact-handoff economics for a recorded run.

The `agent-relay` command is canonical for these surfaces; `subagent` remains a
compatibility alias for existing integrations.
~~~

At the end of ordinary planning, request the default Sol-high second opinion:

~~~powershell
agent-relay escalate --task .\task.json --stage plan_end --json
agent-relay consult --task .\task.json --repo . --stage plan_end --json
~~~

After the ordinary worker has reviewed its candidate, use the acceptance gate:

~~~powershell
agent-relay escalate --task .\task.json --stage review_end --json
agent-relay consult --task .\task.json --repo . --stage review_end --json
~~~

Pass `--policy .\config\escalation.example.json` or set
`AR_ESCALATION_POLICY` to replace the defaults. A required consultation that
cannot run is a failed gate; it is never silently downgraded to the bulk lane.

## Durable coordinator quick start

Run the coordinator locally:

~~~powershell
agent-relay serve --db .\relay.sqlite3 --port 8788
~~~

Submit the same task repeatedly with the same idempotency key; the coordinator
returns one logical task rather than creating duplicate execution:

~~~powershell
agent-relay submit --url http://127.0.0.1:8788 --task .\task.json `
  --idempotency-key task-001 --json
agent-relay watch task-001 --url http://127.0.0.1:8788
agent-relay watch task-001 --url http://127.0.0.1:8788 --stream --json
~~~

For a bounded follow-up, submit a child after the predecessor reaches an
allowed terminal state, or register it early with `--defer-until-ready`. The
coordinator persists the linear chain, atomically materializes deferred steps,
and makes the request idempotent across orchestrator restarts:

~~~powershell
agent-relay chain-submit --chain-id feature-1 --step-id review --step-index 1 `
  --task .\review-task.json --predecessor-task-id task-001 `
  --parent-artifact-id artifact_<patch-id> `
  --parent-message "Review only the declared patch." --priority 5 `
  --deadline-at 2027-08-23T00:00:00Z --json
agent-relay inspect-chain feature-1 --url http://127.0.0.1:8788 --json
agent-relay watch-chain feature-1 --url http://127.0.0.1:8788 --json

agent-relay chain-submit --chain-id feature-1 --step-id test --step-index 2 `
  --task .\test-task.json --predecessor-task-id review `
  --defer-until-ready --json
~~~

Only explicitly named predecessor artifacts and bounded messages enter the
child envelope; transcripts and undeclared parent files are never forwarded.
`--priority` accepts integers from -1000 to 1000; higher values claim first,
then earlier deadlines. A deadline expires unleased queued work before
execution; it does not interrupt an already-running worker.
When a child is leased, the reference worker fetches only those declared
artifacts, verifies their hash and size, and records the fetch in its receipt.
Use `POST /chains/{chain_id}/reconcile` after operational repair; the
coordinator also runs reconciliation at startup.

### MCP façade

Claude MCP is convenient because an MCP client can discover a small tool set
without learning the underlying HTTP API. Agent Relay provides the same entry
point while retaining durable task state and worker receipts:

~~~powershell
agent-relay mcp --coordinator-url http://127.0.0.1:8788 `
  --coordinator-token $env:AR_RELAY_AUTH_TOKEN `
  --token $env:AR_RELAY_AUTH_TOKEN --port 8789
~~~

The `/mcp` endpoint exposes `submit` (plus `run`/`Agent` Claude-MCP-compatible
aliases), bounded `dispatch`, `inspect`, `watch`, `cancel`, and `chain_submit`.
Calls may provide a complete bounded task contract or a natural-language
`prompt`. Prompt calls become durable tasks and are read-only by default;
callers must explicitly provide `allowed_files` before edits are authorized.
`dispatch` submits durable tasks with bounded concurrency and can optionally
wait for terminal snapshots.
Keep the façade on loopback unless a bearer token and HTTPS-protected
coordinator path are configured.

For a single-machine convenience mode, add an optional local worker. `run` and
`Agent` then wait for a terminal receipt by default; `submit` remains an
asynchronous durable enqueue:

~~~powershell
agent-relay mcp --coordinator-url http://127.0.0.1:8788 `
  --coordinator-token $env:AR_RELAY_AUTH_TOKEN `
  --token $env:AR_RELAY_AUTH_TOKEN `
  --local-worker-backend claude-task --local-worker-repo C:\work\repo
~~~

The local worker still uses the normal lease, capability, sandbox, and receipt
path. Prompt calls may also provide `workdir`: for `local-qwen` and
`claude-task` it must be an existing directory inside `--local-worker-repo`; for
`claude-mcp` it is passed as a path on the remote MCP machine. The worker
records the effective remote directory in the receipt. Omit the local-worker
flags when the worker should run on another machine; remote workers should
submit explicit task contracts because their filesystem roots are not visible
to the MCP façade.

To route durable tasks into an existing Claude MCP server, use the explicit
`claude-mcp` worker backend. Configure the endpoint and the path as seen by the
remote MCP server:

~~~powershell
$env:AR_CLAUDE_MCP_URL = "https://pc-b.example.test:8000/mcp"
$env:AR_CLAUDE_MCP_WORKDIR = "."
$env:AR_CLAUDE_MCP_AUTH_TOKEN = "<optional-token>"
agent-relay worker --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --worker-id pc-a-claude-mcp --backend claude-mcp --repo C:\work\repo --claim-next
~~~

Plain HTTP to a non-loopback MCP endpoint is rejected by default. For a
trusted private-LAN development service only, explicitly set
`AR_CLAUDE_MCP_ALLOW_INSECURE_LAN=1`. The remote MCP server owns the process
and filesystem; Agent Relay records its bounded output and transport identity,
but does not claim local patch or verification authority for this backend.

On the worker machine, point the reference worker at its local checkout:

~~~powershell
agent-relay worker --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --agent-token $env:AR_RELAY_AGENT_TOKEN `
  --worker-id pc-b-claude --backend claude-task --repo C:\work\repo `
  --claim-next
~~~

`--token` is the coordinator/admin credential; `--agent-token` is the scoped
credential enrolled for that worker. Rotate or revoke a worker credential with
the coordinator token:

~~~powershell
agent-relay agents --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --revoke pc-b-claude --json
~~~

`--claim-next` asks the coordinator for one highest-priority compatible task per poll.
The coordinator evaluates the enrolled Agent Card and task workspace policy,
skips incompatible or already leased work, and grants the lease atomically.
Without this flag, the reference worker retains the backwards-compatible
list-and-claim loop.

For LAN use, set a bearer token on both server and client. Prefer HTTPS with a
certificate issued by a private CA or trusted public CA:

~~~powershell
$env:AR_RELAY_CA_CERT = "C:\agent-relay\lan-ca.pem"
agent-relay serve --host 0.0.0.0 --port 8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --tls-cert C:\agent-relay\server-chain.pem `
  --tls-key C:\agent-relay\server-key.pem
~~~

Clients and workers use `https://...` URLs and verify the server certificate;
`AR_RELAY_CA_CERT` adds a private CA without an insecure bypass. The current
coordinator persists lifecycle state, leases, Agent Cards, and hash-checked
patch artifacts. Workers acquire a lease and report transitions through the
protocol endpoints; the reference worker loop provides the local-Qwen and
Claude adapter implementations.

Use `workspace_policy` to make routing explicit and enforceable. For example,
`--workspace-policy-json '{"backend":"claude-task","required_capabilities":["claude-a2a"]}'`
prevents a scoped local-Qwen worker from claiming the task; the coordinator
checks the enrolled Agent Card before granting its lease.

To use a Claude A2A daemon that is already running on another machine, set
the remote endpoint on the process that executes the `claude-task` lane:

~~~powershell
$env:AR_CLAUDE_A2A_SERVER_URL = "https://pc-b.example.test:8787"
$env:AR_CLAUDE_A2A_AUTH_TOKEN = "<claude-daemon-token>"
$env:AR_CLAUDE_A2A_CA_CERT = "C:\agent-relay\lan-ca.pem" # private CA only
$env:AR_CLAUDE_A2A_WORKSPACE_PATH = "."
agent-relay delegate --backend claude-task --task .\task.json --repo . --json
~~~

With these settings Agent Relay submits the bounded task to the existing
daemon's durable `/a2a/jobs` API, polls the same job, propagates cancellation,
and returns the daemon's bounded patch and receipt. Without the remote URL, the
worker starts the vendored ephemeral bridge locally. Non-loopback Claude A2A
servers require an auth token and TLS; use a loopback listener or a secure
tunnel for development-only HTTP.

Cancellation is deliberately evidence-bound. Claude tasks use the bridge's
durable async job cancellation endpoint; adapters that cannot prove a stopped
execution return an explicit `blocked` receipt with
`execution_stopped: false`, never a false `succeeded` result.

Transient Claude bridge transport failures are also evidence-bound: when the
adapter can prove the caller worktree remains untouched and the failure is a
retryable liveness/connection boundary, the worker records the failure, returns
the task to `waiting`, releases its lease, and consumes the task's bounded
`retry_limit`. It does not turn a recoverable adapter outage into a false
terminal failure or retry indefinitely.

Filter the machine-readable Agent Card registry by task kind, capability, or
truthful readiness:

~~~powershell
agent-relay agents --url http://127.0.0.1:8788 --task-kind mechanical `
  --capability claude-a2a --readiness ready --json
~~~

The coordinator also exposes a bounded Server-Sent Events replay stream for
clients that need push-style updates. Reconnect with the last numeric `id` as
the `after` value; the JSON `/events?after=N` endpoint remains the canonical
missed-event replay path:

~~~powershell
curl.exe -H "Authorization: Bearer $env:AR_RELAY_AUTH_TOKEN" `
  "http://127.0.0.1:8788/tasks/task-001/events/stream?after=0&timeout=30"
~~~

agent-relay reprice is an estimate of frontier response/handoff accounting. It is not
the same as the measured frontier Codex telemetry in the result above. Do not
use compact packet-size savings as a substitute for a complete
triage/review/repair ledger.

For deterministic orchestration checks without a model:

~~~powershell
agent-relay eval --backend fixture --suite bounded-50 --aggregate --sample 5 --json
~~~

Run the authenticated control-plane acceptance harness. It launches the
coordinator as a separate local process and exercises the protocol over real
HTTP, including idempotency, expired-lease reassignment, stale-worker
rejection, artifact/receipt persistence, coordinator restart, SSE reconnect,
and credential revocation:

~~~powershell
py -3 scripts\acceptance_control_plane.py
~~~

This is a loopback protocol/fault-injection harness; the physical two-PC LAN
scenario remains a separate release gate.

The fixture backend proves task, sandbox, scope, retry, and reporting logic.
It is not evidence that Qwen can solve the tasks or that Codex tokens were
saved.

## Codex CLI subagents and review

The standard implementation worker is Claude through the `claude-task` backend.
Sol high is the independent read-only acceptance reviewer. The parent Codex
Desktop session remains responsible for final integration, UI work, and ship
decisions.

For a direct non-interactive implementation run, use `codex exec` with explicit
model and sandbox settings:

~~~powershell
codex exec --cd . --model gpt-5.6-sol --sandbox workspace-write `
  -c model_reasoning_effort=high `
  "Implement the bounded task, run the focused tests, and report changed files and evidence."
~~~

Project custom agents are selected when the parent Codex session spawns a
subagent. Start an interactive session in this repository and request:

~~~text
Use Claude/claude-task to implement this bounded task. After it finishes, use
sol-reviewer to inspect the candidate and deterministic verification evidence,
then have the parent Codex Desktop session make the final acceptance decision.
Do not let sol-reviewer edit files.
~~~

For a standalone CLI review of all staged, unstaged, and untracked changes:

~~~powershell
codex review --uncommitted
~~~

`codex exec` is the scriptable worker interface; add `--json` for JSONL events
or `--output-last-message` when another tool needs the final message. `codex
review` is the dedicated non-interactive review interface and does not modify
the worktree. The project `.codex/config.toml` only caps concurrent subagents;
it does not alter global Codex configuration.

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

The archive is generated from the skill source with
`py -3 scripts/build_skill_package.py`; CI validates both the checked-in
archive and the copy embedded in the Python wheel.

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
src/agent_relay/       Canonical Python module containing the worker, Codex
                                harness, proxy, sandbox, verifier,
                               task contract, triage, batch, and CLI.
lanes/claude-task/             Vendored Claude native-team/A2A worker lane.
skills/agent-relay/              Unified skill, lane routing, and worker prompt references.
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
