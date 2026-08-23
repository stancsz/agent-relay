# Claude Orchestrator

Claude Orchestrator is a durable operating layer for Claude Code workers and
optional native Agent Teams.

It keeps Claude as the implementation engine and adds the pieces needed for reliable
long-running work: native team coordination, background jobs, heartbeats,
recovery, bounded memory, reusable skills, schedules, independent evidence,
and authenticated A2A/LAN access.

It uses Claude Code as the execution runtime and does not claim to replace
Claude Code.

## Re-throning Claude / 让 Claude 重回王座

The central idea is simple: Claude should remain the implementation worker.
Claude understands the repository, calls tools, edits code, and can coordinate
bounded workers. Claude Orchestrator turns that work into a bounded, durable,
and verifiable system.

核心目标不是再造一个“像 Claude 的 Agent”，而是让 Claude 原生的 Agent
Teams 重新成为工作流的执行核心：Claude 负责理解代码、调用工具、修改项目
和协调队友；Claude Orchestrator 负责任务边界、断线恢复、跨设备交接和证据验证。

| Capability | Claude Orchestrator implementation |
| --- | --- |
| Durable execution | Durable jobs, heartbeats, recovery, cancellation, resume, goals, and schedules |
| Persistent organization | Isolated profiles, explicit memory, reusable skills, and gateway access |
| Claude worker coordination | Independent worker contexts, shared tasks, direct messaging, and version-aware team lifecycle |
| Evidence-driven engineering | Bounded packets, context digests, workspace locks, Git fingerprints, and independent verifier gates |

借鉴的是设计方式，不是复制运行时，也不是把对话历史偷偷塞回上下文。记忆
和 skill 都是显式、有限、可审阅的；Claude 的报告是收据，不是正确性的证明。

## How it works

```text
local or remote orchestrator
        | bounded task JSON + context digest
        v
authenticated Claude Orchestrator daemon
        | queue / heartbeat / profile / memory / schedule
        | fresh `claude mcp serve` session per task
        v
Claude Code native Agent Teams
        | modern implicit teams or legacy create/delete teams
        v
bounded result + Git/worktree evidence
```

The installed skill is named `claude-orchestrator`. The backend contract remains
`claude-task` so existing Agent Relay task packets and CLI commands stay
compatible.

## What it provides

- Native `team`, `worker`, and `verifier` task modes.
- Capability detection for modern implicit Agent Teams and older
  `TeamCreate`/`TeamDelete` MCP surfaces.
- Fresh `claude mcp serve` sessions with the project directory as `cwd`.
- Loopback by default; authenticated bearer-token access for LAN use.
- Durable asynchronous jobs with polling, heartbeats, cancellation, resume,
  and interrupted-job recovery.
- Isolated profiles, bounded searchable memory, reviewable skill snippets,
  one-shot schedules, and interval schedules.
- Repository-relative paths, bounded excerpts, SHA-256 context digests,
  duplicate-task protection, workspace serialization, and Git fingerprints.
- Independent verifier no-change gates and expected-change gates.
- Idle notifications treated as soft signals, with a bounded result reminder;
  explicit teammate failures remain fatal.
- No client-controlled model, base URL, credential, permission mode, budget,
  transcript, or repository dump.

## Install and start a host

The source implementation is vendored under the Agent Relay repository's
`lanes/claude-task/` backend. The installed Codex skill is normally located at:

```text
C:\Users\stanc\.codex\skills\claude-orchestrator
```

Start a loopback daemon for one allowlisted workspace:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\start_claude_a2a_server.ps1" `
  -WorkspaceRoot "C:\path\to\repo" `
  -StateDir "C:\Users\stanc\.claude-orchestrator"
```

For LAN access, bind deliberately and provide a long random token:

```powershell
$env:CLAUDE_A2A_AUTH_TOKEN = '<random-long-token>'
powershell -NoProfile -File `
  "<skill-root>\scripts\start_claude_a2a_server.ps1" `
  -ListenHost "0.0.0.0" `
  -Port 8787 `
  -WorkspaceRoot "C:\path\to\repo" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN `
  -StateDir "C:\Users\stanc\.claude-orchestrator"
```

Use a firewall rule and keep the workspace root narrow. Never put the token in
task JSON or source control. State is stored outside the worktree and contains
bounded jobs, receipts, profiles, memory, skills, and schedules—not transcripts.

For long-running CLI fallback work, set `CLAUDE_A2A_TIMEOUT_SECONDS` before
launching the host, or pass `-TimeoutSeconds` explicitly. The setting applies
only to that relay process; `2400` is a suitable example for a bounded
multi-chapter editorial pass.

## Health and capability checks

Check the daemon without starting Claude:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_health.ps1" `
  -ServerUrl "http://127.0.0.1:8787"
```

Probe the actual Claude MCP server and native team surface:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_health.ps1" `
  -ServerUrl "http://127.0.0.1:8787" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN `
  -ProbeCapabilities
```

The capability probe reports the MCP protocol/server version, available tools,
`Agent` availability, modern native-team availability, and legacy team-tool
availability. It does not override the model, endpoint, credentials, or budget.
On a runtime where `Agent` is exposed but no usable native agent type is
registered, a task may be retried through the explicitly labeled `cli-fallback`
adapter after a no-worktree-change gate. This preserves bounded execution but
does not claim that a native team ran.
The local Windows Claude Code 2.1.233 smoke additionally reports a missing
`TaskCreate`/`TaskList`/`TaskUpdate` team surface; this is treated as the same
native-capability boundary and follows the same labeled fallback path.
Trust the probe for the installed Claude Code version instead of assuming that
the latest documentation matches the local CLI. See the official
[Agent Teams documentation](https://code.claude.com/docs/en/agent-teams) and
[MCP documentation](https://code.claude.com/docs/en/mcp).

## Submit bounded work

Build packets mechanically so the context digest is reproducible:

```python
import json
import sys

sys.path.insert(0, r"<skill-root>\scripts")
from a2a_protocol import build_task

task = build_task(
    task_id="feature-team-001",
    target_role="team",
    operation="team",
    target_paths=["src/feature.ts", "tests/feature.test.ts"],
    objective="Implement the feature and independently review the result.",
    acceptance_criteria=["Focused checks pass.", "No unrequested worktree changes."],
    constraints=["Do not commit, push, merge, deploy, reset, clean, or switch branches."],
    inputs=[],
    profile="coder",
    skill_refs=["focused-review"],
    memory_query="similar feature work",
    remember=True,
    team={"name": "feature-team", "members": [
        {"name": "builder", "role": "worker", "objective": "Implement the feature and run focused checks."},
        {"name": "claude-verifier", "role": "verifier", "objective": "Review evidence read-only and report risks."},
    ]},
    expected_change=True,
)
print(json.dumps(task, ensure_ascii=False))
```

Submit synchronously:

```powershell
powershell -NoProfile -File `
  "<skill-root>\scripts\claude_a2a_delegate.ps1" `
  -TaskFile ".\task.json" `
  -ServerUrl "http://127.0.0.1:8787" `
  -AuthToken $env:CLAUDE_A2A_AUTH_TOKEN
```

For work that should survive a disconnected client, add `-Async`. The returned
`job_id` can be watched, cancelled, or resumed with `claude_a2a_client.py`.

## Evidence and boundaries

A Claude report is a receipt, not proof. The orchestrator should still run the
relevant build, lint, typecheck, tests, runtime checks, and complete-diff review.
A verifier-containing task fails if the content-level worktree fingerprint
changes unexpectedly.

Native teammates use independent contexts and consume additional tokens. They
do not inherit the lead's conversation history. Cross-session messaging is a
separate Claude feature with its own version and platform limits; authenticated
HTTP A2A is the explicit Windows LAN path. See the official
[cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging).

Claude Orchestrator is not a general autonomous model, does not silently modify its
own permissions, and does not replace human responsibility for review, merge,
or release decisions. Use `-NoCliFallback` when native-only execution is a hard
requirement; use `-CliFallback` when an explicitly CLI-based bounded route is
acceptable. Inspect the receipt's `transport` field in either case.

## Validation

```powershell
py -3 -B -m py_compile `
  scripts\bridge_state.py `
  scripts\a2a_protocol.py `
  scripts\claude_a2a_server.py `
  scripts\claude_a2a_client.py `
  scripts\claude_mcp_delegate.py `
  scripts\test_claude_a2a.py

$env:PYTHONPATH = (Resolve-Path scripts).Path
py -3 -B scripts\test_claude_a2a.py
Remove-Item Env:PYTHONPATH

powershell -NoProfile -File scripts\test_claude_delegate.ps1
py -3 -X utf8 `
  "C:\Users\stanc\.codex\skills\.system\skill-creator\scripts\quick_validate.py" `
  .
```

The deterministic suite covers modern and legacy fake-MCP surfaces,
authenticated LAN requests, durable memory/skills/jobs/schedules, async
polling, context boundaries, concurrency locking, and worktree gates.
