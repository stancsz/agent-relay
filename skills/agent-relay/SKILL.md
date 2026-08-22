---
name: agent-relay
description: "Route bounded work through heterogeneous agent harnesses for local Qwen, Claude, Codex review, or Antigravity. Use explicit contracts, fail-closed permissions, and independent proof review."
---

# Agent Relay

Agent Relay provides four deliberately different agent-harness lanes behind one
workflow and one vocabulary:

| Lane | Role | Authority | Default model | Worktree policy |
| --- | --- | --- | --- | --- |
| `local-qwen` | mechanical worker | free local inference through Ollama and Codex CLI | `qwen3.5:4b` | disposable sandbox; parent reruns proof |
| `claude-task` | primary implementation/team worker | authenticated Claude Code task bridge, optional Agent Teams | host policy | bounded receipt with Git/workspace gates |
| `codex-review` | independent verifier | logged-in Codex CLI subscription | `gpt-5.6-sol`, high effort | read-only review; must not edit |
| `agy-antigravity` | Google-stack scout/planner | local Antigravity CLI session | `gemini-3.1-pro-high`, high effort | plan mode; parent owns edits and proof |

## Routing rules

The parent Codex remains the orchestrator. It owns decomposition, allowed files,
acceptance criteria, integration, and final validation. A worker report is a
candidate receipt, not proof.

- Use `local-qwen` only for low-risk, finite, mechanically verifiable tasks.
- Use `claude-task` for repository-aware implementation or parallel independent
  work; use Agent Teams only when direct teammate coordination earns its cost.
- Use `codex-review` after implementation for an independent, read-only QA pass.
- Use `agy-antigravity` for Google-specific ecosystem questions, Firebase,
  Android, browser/UI, and Gemini/Google Cloud integration judgment. Treat it as
  a plan/research specialist, not a general patch worker.
- Never ask a verifier to edit, fix, commit, push, merge, deploy, or approve its
  own changes.
- Run the repository's own tests and inspect the complete diff after every lane.

## Commands

List the canonical registry:

```powershell
agent-relay lanes --json
```

Run the local worker through the existing bounded contract:

```powershell
agent-relay delegate --backend local-qwen --task .\task.json --repo . --require-triage --json
```

Start the Claude task bridge for an allowlisted workspace:

```powershell
powershell -NoProfile -File .\lanes\claude-task\scripts\start_claude_a2a_server.ps1 -WorkspaceRoot (Get-Location).Path
```

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

`lcd`, `subagent`, `ollama`, and `codex-ollama` remain supported for existing
scripts. New documentation and new integrations should use `agent-relay`,
`local-qwen`, `claude-task`, `codex-review`, and `agy-antigravity`.
