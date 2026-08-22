---
name: claude-team-bridge
description: "Claude Prime: native Claude Code Agent Teams with a durable, bounded, authenticated A2A/LAN gateway. Use for native teams, background jobs, profiles, memory, reusable skills, schedules, and cross-device delegation."
---

# Claude Prime

Use this skill when Claude Code should do bounded work as a native Agent Team, or when a remote orchestrator needs to reach a Claude host over authenticated LAN A2A. The bridge is a durable control plane around Claude's native runtime; it is not a second fake team implementation.

## Architecture

```text
local or remote orchestrator
    | bounded JSON task packet + digest
    v
authenticated Claude Prime daemon (one host, one allowlisted workspace)
    | queue / heartbeat / profile / memory / schedule
    | fresh stdio MCP session; CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
    v
Claude Code native Agent Teams
    | modern: Agent implicitly creates team -> tasks -> SendMessage
    | legacy: TeamCreate -> Agent teammates -> TaskCreate/TaskUpdate -> TeamDelete
    v
bounded receipt + Git/worktree evidence
```

Claude owns the team lifecycle and teammate contexts. The bridge owns the network boundary, packet validation, workspace allowlist, role policy, fresh-session rule, bounded result collection, durable jobs, profiles, explicit memory/skills, schedules, cleanup, and independent Git-state gates.

Native Agent Teams are not themselves a cross-machine LAN protocol. A team lives in the Claude host's local `~/.claude/teams` and `~/.claude/tasks` state. To use another computer, run a bridge on that computer and send a bounded A2A packet to it. Do not forward chat history, transcripts, context windows, credentials, repository dumps, or prior task results.

## Prime Agent and Hermes-inspired operating layer

This skill keeps Claude as the native coding engine and adds the operational strengths users value in [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) and [Hermes Agent](https://github.com/NousResearch/hermes-agent):

- Prime-style durable jobs, heartbeats, reattachment, cancellation/resume, schedules, and programmatic teammate calls.
- Prime-style reviewable harness state without rewriting the base skill or silently changing the task contract.
- Hermes-style isolated profiles, explicit searchable memory, reusable skill snippets, and a gateway that can be reached from another device.

Memory and skill writes are explicit and bounded. The bridge never turns a transcript into hidden prompt state.

## Modes

- `team`: one native Claude team with 1–8 named worker/verifier teammates. This is the preferred mode when parallel or independent work is useful. If the installed runtime rejects its native agent type before changing the worktree, the server may use the explicitly recorded bounded CLI fallback.
- `worker`: one native MCP `Agent` call for a focused task.
- `verifier`: one independent read-only MCP `Agent` call plus a no-worktree-change gate.
- `LAN`: the same modes exposed through an authenticated HTTP relay. LAN binding is rejected without a token.
- `daemon`: submit asynchronously, disconnect, then reattach by job ID; the daemon records heartbeats, attempts, failures, and bounded receipts.
- `profile`: isolate durable memory and reusable skill snippets for different agents or workflows.

The server chooses the native agent type from its local policy (`-WorkerAgentType` / `-VerifierAgentType`). An unset policy is passed through as unset so Claude can apply its own default; the bridge no longer invents `general-purpose`. A task packet cannot choose a model, base URL, credential, budget, permission mode, or arbitrary Claude setting.

## Role and evidence rules

- `orchestrator` is the only submitter and owns scope, acceptance criteria, and ship decisions.
- `worker` may edit only the requested bounded task. It must not commit, push, merge, deploy, reset, clean, or switch branches.
- `verifier` independently inspects evidence and must not edit. Any content-level worktree change rejects a verifier-containing run.
- A Claude report is evidence to inspect, not proof of correctness. The orchestrator still runs the relevant build, lint, typecheck, tests, runtime checks, and complete-diff review.

Packets are versioned and digest-linked. Paths are repository-relative; inputs are explicit SHA-256 hashes plus bounded excerpts. Unknown fields, context-bloat keys, path traversal, oversized content, invalid role transitions, duplicate member names, and digest mismatches are rejected.

## Start a host relay

Loopback is the default:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\start_claude_a2a_server.ps1" `
  -WorkspaceRoot "C:\path\to\repo"
```

For LAN use, bind deliberately and require a long random token. Keep the workspace root narrow and apply a firewall rule for the chosen port:

```powershell
$env:CLAUDE_A2A_AUTH_TOKEN = '<random-long-token>'
powershell -NoProfile -File `
  "<skill-root>\scripts\start_claude_a2a_server.ps1" `
  -ListenHost "0.0.0.0" `
  -Port 8787 `
  -WorkspaceRoot "C:\path\to\repo" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN `
  -StateDir "C:\Users\stanc\.claude-team-bridge"
```

The daemon starts no Claude process at launch. Every execution gets a fresh `claude.cmd mcp serve` session with the selected workspace as its working directory. Asynchronous jobs continue after the submitting client disconnects; state stays outside the repository.

## Health and capability checks

Relay health is cheap and does not start Claude:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_health.ps1" `
  -ServerUrl "http://127.0.0.1:8787"
```

Probe the actual Claude MCP capability, including native Agent Teams:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_health.ps1" `
  -ServerUrl "http://127.0.0.1:8787" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN `
  -ProbeCapabilities
```

The probe starts a fresh session with only the Agent Teams feature flag enabled. It reports the MCP protocol/server version, tool names, `Agent` availability, whether the modern implicit team surface is visible, and whether legacy `TeamCreate`/`TeamDelete` are visible. It does not claim that a teammate can execute successfully; the no-edit native team smoke is the stronger test.

Claude Code version differences matter. Current documentation describes modern implicit teams from v2.1.178 onward; the local compatibility validation also covers Claude Code `2.1.146`, where the legacy create/delete tools are exposed. Capability-probe the installed CLI instead of assuming a documentation version matches the machine.

## Build a native team task

```python
from pathlib import Path
import hashlib, json, sys
sys.path.insert(0, r"<skill-root>\scripts")
from a2a_protocol import build_task

task = build_task(
    task_id="feature-team-001",
    target_role="team",
    operation="team",
    target_paths=["src/feature.ts", "tests/feature.test.ts"],
    objective="Implement the bounded feature and independently review the result.",
    acceptance_criteria=["The focused test passes.", "The verifier reports no unrequested worktree changes."],
    constraints=["Do not commit, push, merge, deploy, reset, clean, or switch branches."],
    inputs=[],
    profile="coder",
    skill_refs=["focused-review"],
    memory_query="similar feature work",
    remember=True,
    team={"name": "feature-team", "members": [
        {"name": "builder", "role": "worker", "objective": "Implement the requested feature and run focused checks."},
        {"name": "reviewer", "role": "verifier", "objective": "Independently inspect the diff and test evidence without editing."},
    ]},
    expected_change=True,
)
Path("task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
```

Submit it through the authenticated client:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_delegate.ps1" `
  -TaskFile ".\task.json" `
  -ServerUrl "http://127.0.0.1:8787" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN
```

The client exits zero only for a validated `done` result. A missing native team tool, failed teammate result, malformed receipt, unexpected verifier change, or unsatisfied change expectation remains a failure. A `cli-fallback` receipt is a bounded CLI role execution (all declared workers in sequence, followed by a read-only verifier), not proof that a native team ran. Worker members may declare `target_paths`; each fallback session receives only its member-scoped paths.

For long-running work, submit the same task with `-Async`. Poll `/a2a/jobs/{job_id}`, cancel with `/a2a/jobs/{job_id}/cancel`, or resume an interrupted/failed job with `/a2a/jobs/{job_id}/resume`. Profile memory and skill snippets are opt-in and bounded; no transcript or full context is persisted.

## Native limitations and recovery

Native teams use independent teammate contexts; teammates do not inherit the lead's conversation history. Keep the task packet self-contained and small. One Claude session has one lead team, native teams do not nest, and permissions are governed by the lead session. In-process teammates are the Windows-compatible mode; split-pane display is not required for the bridge. Modern Claude creates the team on first `Agent` spawn and cleans its config on session exit; legacy Claude requires explicit create/delete calls.

Teammate `idle_notification` messages are not treated as failure because Claude can emit one before the required result message is flushed. The bridge sends one bounded reminder and continues waiting; explicit `teammate_failed` events remain fatal. Legacy cleanup waits briefly for shutdown acknowledgements and removes only the exact `a2a-*` team/task paths under the current Claude home.

Cross-session messaging is a separate Claude feature and should not be confused with this LAN relay. It is not the local HTTP A2A protocol and has platform/version limitations. The bridge remains the explicit option when one computer must delegate bounded work to another computer on the same network.

If the capability probe succeeds but a team smoke fails, inspect the returned MCP receipt and exact Claude team/task artifacts. The server may retry only for a recognized native-capability failure (for example, an unavailable agent type or missing Agent Teams tools) when the native snapshot proves that the worktree did not change. That retry is bounded by the same target paths and Git expectation and is labeled `transport: cli-fallback`; use `-NoCliFallback` to disable it or `-CliFallback` to request it explicitly. Never silently override model, endpoint, credentials, or budget. If work may have started, inspect the complete worktree before retrying.

### Current Windows capability boundary

On the observed Claude Code 2.1.239 installation, a team-mode MCP session exposes
`Agent` and the native team tools, but spawning a configured worker can still fail with
`Agent type '…' not found. Available agents: none`. Capability probes therefore are not
proof that a native teammate can execute. The fallback keeps work moving through the
explicit `claude --print` adapter, but its receipt remains distinguishable from
`native-mcp`; sequential CLI role execution cannot be used as evidence that a native
team actually ran.

## Files

- `scripts/a2a_protocol.py`: bounded task/result envelopes, team member validation (including optional member-scoped target paths), profiles, skill refs, memory queries, and context digests.
- `scripts/bridge_state.py`: atomic durable profiles, memory, reusable skills, jobs, and schedules.
- `scripts/claude_a2a_server.py`: loopback/LAN relay, native-team manifest construction, Git gates, and capability endpoint.
- `scripts/claude_a2a_client.py`: validated HTTP client.
- `scripts/claude_mcp_delegate.py`: stdlib MCP client for modern implicit and legacy native-team lifecycles.
- `scripts/claude_a2a_delegate.ps1`: Windows task client wrapper.
- `scripts/start_claude_a2a_server.ps1`: Windows relay launcher.
- `scripts/claude_a2a_health.ps1`: relay and optional live capability health check.
- `scripts/test_claude_a2a.py`: deterministic protocol, auth, modern/legacy fake-MCP, durable-state, native-team, and worktree-gate test.
- `scripts/claude_delegate.ps1`: explicit legacy compatibility adapter; not the team bridge.

## Validation

```powershell
py -3 -B -m py_compile scripts\bridge_state.py scripts\a2a_protocol.py scripts\claude_a2a_server.py scripts\claude_a2a_client.py scripts\claude_mcp_delegate.py scripts\test_claude_a2a.py
$env:PYTHONPATH = (Resolve-Path scripts).Path
py -3 -B scripts\test_claude_a2a.py
Remove-Item Env:PYTHONPATH
powershell -NoProfile -File scripts\test_claude_delegate.ps1
py -3 -X utf8 "C:\Users\stanc\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .
```
