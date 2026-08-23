---
name: agent-relay
description: "Route bounded work through heterogeneous agent harnesses for local Qwen, Claude, Codex review, or Antigravity. Use explicit contracts, fail-closed permissions, and independent proof review."
---

# Agent Relay

Agent Relay provides five deliberately different agent-harness lanes behind one
workflow and one vocabulary:

| Lane | Role | Authority | Default model | Worktree policy |
| --- | --- | --- | --- | --- |
| `local-qwen` | mechanical worker | free local inference through Ollama and Codex CLI | `qwen3.5:4b` | disposable sandbox; parent reruns proof |
| `claude-task` | Claude implementation worker | authenticated Claude Code orchestrator, optional Agent Teams | host policy | bounded receipt with Git/workspace gates |
| `claude-mcp` | remote convenience worker | existing Claude streamable-HTTP MCP server (JSON or SSE) | host policy | remote output/transport receipt; no local sandbox claim |
| `sol-reviewer` | Sol high independent read-only reviewer | logged-in Codex CLI subscription | `gpt-5.6-sol`, high effort | read-only review; must not edit |
| `agy-antigravity` | Google-stack scout/planner | local Antigravity CLI session | `gemini-3.1-pro-high`, high effort | plan mode; parent owns edits and proof |

## Routing rules

The parent Codex remains the orchestrator. It owns decomposition, allowed files,
acceptance criteria, integration, and final validation. A worker report is a
candidate receipt, not proof.

- Use `local-qwen` only for low-risk, finite, mechanically verifiable tasks.
- Use `claude-task` for repository-aware implementation or parallel independent
  work; use Agent Teams only when direct teammate coordination earns its cost.
- Use `claude-mcp` only when an existing remote MCP service is the intended
  execution authority; it does not provide local patch or sandbox proof.
- Use `sol-reviewer` after Claude implementation for an independent, read-only QA pass.
- Use the configurable escalation policy at `plan_end`, `execute`, `review_end`,
  `recovery`, and `release` gates. Let ordinary workers do bulk work; summon a
  configured Sol-high planner after ordinary planning and Sol reviewer after
  ordinary review by default; additional rules can summon them on recovery,
  ambiguity, risk, or missing proof.
- Use `agy-antigravity` for Google-specific ecosystem questions, Firebase,
  Android, browser/UI, and Gemini/Google Cloud integration judgment. Treat it as
  a plan/research specialist, not a general patch worker.
- Never ask a verifier to edit, fix, commit, push, merge, deploy, or approve its
  own changes.
- Run the repository's own tests and inspect the complete diff after every lane.

The escalation policy is operational rather than confidence-based. It records
the matched rule, signals, selected profile/model, and evidence requirements.
Malformed policy, unavailable required high-lane capability, missing evidence,
or ambiguous safety signals fail closed; do not silently fall back to the bulk
worker and call a required consultation complete.

The default acceptance path is `claude-task` implementation, deterministic
verification, then `sol-reviewer`. A result is not accepted when either the
declared tests fail or the Sol reviewer is unavailable, rejects the candidate,
or cannot produce a read-only receipt.

Treat a high-tier rejection as feedback: return it to the selected bulk worker
(`claude-task` for repository-aware implementation, or another configured
worker), require a deterministic recheck, and invoke the high gate again. Keep
the revision count bounded; after exhaustion stop at human review or blocked.

## Commands

List the canonical registry:

```powershell
agent-relay lanes --json
```

Check whether the configured lanes have usable prerequisites on this machine.
Readiness is `ready`, `degraded`, `blocked`, or `unknown`: a live health probe
can be `ready`, while an executable-only check remains `unknown` until
invocation and entitlement are exercised:

```powershell
agent-relay lanes --check --json
agent-relay doctor --all
```

Run the durable coordinator and use its idempotent task lifecycle:

```powershell
agent-relay serve --db .\relay.sqlite3 --port 8788
agent-relay mcp --coordinator-url http://127.0.0.1:8788 --port 8789
agent-relay submit --url http://127.0.0.1:8788 --task .\task.json --idempotency-key task-001 --json
agent-relay watch task-001 --url http://127.0.0.1:8788
agent-relay watch task-001 --url http://127.0.0.1:8788 --stream --json
agent-relay inspect task-001 --url http://127.0.0.1:8788 --json
agent-relay cancel task-001 --url http://127.0.0.1:8788 --json
agent-relay resume task-001 --url http://127.0.0.1:8788 --json
agent-relay chain-submit --chain-id feature-1 --step-id review --step-index 1 `
  --task .\review-task.json --predecessor-task-id task-001 `
  --parent-message "Review only the declared patch." --json
agent-relay worker --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --agent-token $env:AR_RELAY_AGENT_TOKEN `
  --worker-id pc-b-claude --backend claude-task --repo C:\work\repo `
  --claim-next
```

The coordinator is loopback-only by default. For LAN use, set the same
`AR_RELAY_AUTH_TOKEN` on the server and client. Prefer HTTPS for LAN use:

```powershell
$env:AR_RELAY_CA_CERT = "C:\agent-relay\lan-ca.pem"
agent-relay serve --host 0.0.0.0 --port 8788 `
  --token $env:AR_RELAY_AUTH_TOKEN `
  --tls-cert C:\agent-relay\server-chain.pem `
  --tls-key C:\agent-relay\server-key.pem
```

Use `https://...` coordinator URLs on clients and workers; `AR_RELAY_CA_CERT`
adds a private CA while retaining certificate verification. Cancellation is deliberately
two-phase: `cancel_requested` is not `cancelled` until the worker confirms that
execution stopped. The current slice persists jobs, events, leases, and Agent
Cards and hash-checked patch artifacts. The reference worker loop can execute
local-Qwen or Claude tasks against its explicitly configured local checkout.

Claude workers use the bridge's durable asynchronous job cancellation path.
If an adapter cannot prove that execution stopped, Agent Relay records
`blocked` with `execution_stopped: false`; it never turns a post-cancel result
into false `succeeded` state.

Retryable Claude bridge liveness or connection failures are returned to
`waiting`, their lease is released, and the task's `retry_limit` bounds the
fresh-sandbox retry. Treat a terminal failure as evidence that the bounded
retry policy was exhausted or the error was not classified as transport-safe.

The coordinator bearer is the admin/client credential. The worker's
`--agent-token` is a separately enrolled, scoped credential. Revoke it with:

```powershell
agent-relay agents --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --revoke pc-b-claude --json
```

For coordinator-owned routing, add `--claim-next` to the worker command. Each
poll asks for one highest-priority compatible task; the coordinator applies the worker's
Agent Card and task workspace policy before granting the durable lease. The
default list-and-claim loop remains available for compatibility.

Filter the machine-readable Agent Card registry by task kind, capability, or
truthful readiness:

```powershell
agent-relay agents --task-kind mechanical --capability bounded-edit `
  --readiness ready --json
```

Workers refresh their Agent Card heartbeat after execution. A successful
bounded result marks that worker `ready`; an adapter failure marks it
`degraded`. Registration-only discovery remains `unknown`, so an executable's
presence is never treated as proof of model access.

For push-style clients, use the bounded SSE replay endpoint and reconnect with
the last numeric event id:

```text
/tasks/{task_id}/events/stream?after=0&timeout=30
```

The JSON `/tasks/{task_id}/events?after=N` endpoint remains available for
deterministic replay and inspection.

Follow-up chains are linear and durable. Submit a child through
`POST /chains/{chain_id}/steps` (or `chain-submit`) with an explicit
predecessor terminal-state policy. Repeating the same step is idempotent; a
live, failed, blocked, or cancelled predecessor cannot unlock a child unless
that state is explicitly listed. Add `defer_until_ready: true` (or
`--defer-until-ready`) to register a live predecessor's next step; terminal
completion materializes it exactly once, and `/chains/{chain_id}/reconcile`
replays pending activation after repair or restart. Pass only declared
artifact IDs and bounded messages—never transcripts or implicit repository
context. The reference worker fetches declared parent artifacts through the
coordinator, verifies their hashes, and includes bounded parent-input evidence
in the terminal receipt. Use `watch-chain` to poll the ordered materialized
steps until the chain reaches a terminal or blocked state.

Queue submissions may include `priority` (-1000 through 1000) and an
ISO-8601 `deadline_at` with timezone. Claiming is coordinator-owned: higher
priority wins, then the earliest deadline, then FIFO creation order. The
coordinator expires overdue work only while it is unleased and queued; it does
not forcibly interrupt a running worker.

The `mcp` command exposes the durable coordinator through a small MCP surface:
`submit`/`run`/`Agent`, bounded `dispatch`, `inspect`, `watch`, `cancel`, and
`chain_submit`. It is a translation layer over the authenticated coordinator,
not a second execution authority. Calls may provide a complete task contract or
a natural-language `prompt`; prompt calls become durable tasks and default to
read-only until `allowed_files` is explicitly declared.
For a single-machine convenience path, add `--local-worker-backend` and
`--local-worker-repo`; then `run`/`Agent` wait for a terminal receipt while
`submit` remains asynchronous. Prompt calls may provide a `workdir` under the
configured local-worker repository for `local-qwen` and `claude-task`; for
`claude-mcp`, `workdir` is passed as a path on the remote MCP machine. The
effective directory is recorded in the receipt. The local worker uses the same
lease path as a separately launched worker, while remote MCP verification is
explicitly output-only.

To route durable tasks into an existing Claude MCP service, configure the
explicit `claude-mcp` backend:

```powershell
$env:AR_CLAUDE_MCP_URL = "https://pc-b.example.test:8000/mcp"
$env:AR_CLAUDE_MCP_WORKDIR = "."
$env:AR_CLAUDE_MCP_AUTH_TOKEN = "<optional-token>"
agent-relay worker --url http://127.0.0.1:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --worker-id pc-a-claude-mcp --backend claude-mcp --repo C:\work\repo --claim-next
```

Non-loopback plain HTTP is rejected unless
`AR_CLAUDE_MCP_ALLOW_INSECURE_LAN=1` is explicitly set for a trusted private
LAN development service. This backend records remote MCP output and transport
identity; it does not claim local patch, sandbox, or verification authority.

The repository also includes a loopback protocol/fault-injection acceptance
harness:

```powershell
py -3 scripts\acceptance_control_plane.py
py -3 scripts\acceptance_tls.py
```

When Claude credentials are available, the reproducible lane gates are:

```powershell
py -3 scripts\probe_claude_lane.py --timeout 40
py -3 scripts\smoke_claude_task.py
py -3 scripts\smoke_claude_cancellation.py
py -3 scripts\smoke_claude_interruption.py
```

These prove capability discovery, a bounded parent-verified edit, and a real
worker-confirmed cancellation boundary. They do not substitute for the
physical two-PC interruption/recovery gate.

The interruption smoke additionally kills a separate worker after `running`
and requires lease-expiry reassignment plus a fresh verified Claude receipt.

Run the local worker through the existing bounded contract:

```powershell
agent-relay delegate --backend local-qwen --task .\task.json --repo . --require-triage --json
```

Run Claude through an ephemeral allowlisted A2A bridge and disposable Git
sandbox. The bridge script is vendored with the source checkout; installed
packages should set `AR_CLAUDE_BRIDGE_SCRIPT` to an equivalent server script.

```powershell
agent-relay delegate --backend claude-task --task .\task.json --repo . --json
```

The low-level bridge can still be started directly for bridge-specific jobs or
native-team experiments:

```powershell
powershell -NoProfile -File .\lanes\claude-task\scripts\start_claude_a2a_server.ps1 -WorkspaceRoot (Get-Location).Path
```

An already-running Claude A2A daemon can be used instead of starting a local
ephemeral bridge. Configure the worker process with:

```powershell
$env:AR_CLAUDE_A2A_SERVER_URL = "https://pc-b.example.test:8787"
$env:AR_CLAUDE_A2A_AUTH_TOKEN = "<claude-daemon-token>"
$env:AR_CLAUDE_A2A_CA_CERT = "C:\agent-relay\lan-ca.pem" # private CA only
$env:AR_CLAUDE_A2A_WORKSPACE_PATH = "."
agent-relay delegate --backend claude-task --task .\task.json --repo . --json
```

The adapter submits and polls the daemon's durable job endpoint, propagates a
cancel event, and preserves the remote patch/receipt as the remote worker's
verification evidence. Non-loopback daemon binding requires both bearer auth
and TLS; plain HTTP is limited to loopback or a secure tunnel.

Submit a Claude A2A packet with:

```powershell
powershell -NoProfile -File .\lanes\claude-task\scripts\claude_a2a_delegate.ps1 -TaskFile .\claude-task.json
```

Run the independent Codex subscription review against the current checkout:

```powershell
agent-relay review --repo . --model gpt-5.6-sol --reasoning-effort high --uncommitted --json
```

Ask the Google-stack specialist in plan mode:

```powershell
agent-relay ask --lane agy-antigravity --repo . --prompt "Review Firebase and Android integration risks." --json
```

The AGY adapter uses the standalone `agy` CLI headless print protocol, not the
Antigravity IDE launcher. Install the CLI with
`curl -fsSL https://antigravity.google/cli/install.sh | bash`, then validate it
with `agy -p "Reply with exactly AGY_CLI_OK." --output-format json` from a fresh
login shell. The adapter fails explicitly when the CLI, model, account, JSON
status, or response is unavailable; a zero exit with empty output is never a
pass. `accept-edits` is exposed for bounded experiments, but the canonical lane
defaults to plan mode and does not accept AGY edits as proof.

The Codex lane deliberately does not accept an API key. It invokes the installed
`codex exec review` command, so the user's existing Codex login/session is the
credential boundary. If the model or entitlement is unavailable, the command
returns a failed receipt; it never silently falls back to Qwen or Claude.

The evidence-backed role matrix and current local readiness receipts are in
[`docs/SUBAGENT_ROLES.md`](../../docs/SUBAGENT_ROLES.md).

## Compatibility names

`agent-relay`, `subagent`, `ollama`, and `codex-ollama` remain supported for existing
scripts. New documentation and new integrations should use `agent-relay`,
`local-qwen`, `claude-task`, `claude-mcp`, `sol-reviewer`, and `agy-antigravity`.
