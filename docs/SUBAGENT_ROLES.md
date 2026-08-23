# Subagent roles and local readiness

This is the evidence-backed routing contract for the four unified lanes. The
parent Codex remains responsible for decomposition, permissions, integration,
and final proof. A lane's response is evidence, not acceptance.

## What is being relayed

This project is not primarily an LLM/model router. A model router forwards
prompts or API calls; Agent Relay routes an agent harness with its tools,
permissions, workspace, sandbox, task contract, execution policy, and proof
requirements. The selected model is only one component of the runtime.

## Role matrix

| Lane | Use it for | Do not use it as | Default proof |
| --- | --- | --- | --- |
| `local-qwen` | Cheap, offline-capable mechanical work with a finite diff and deterministic checks | An architect, broad refactorer, or final reviewer | Candidate diff, scope gate, declared checks, and parent rerun |
| `claude-task` | Repository-aware implementation, parallel independent work, and tasks that benefit from a stronger general coding worker | An unreviewed merger or a substitute for the parent’s acceptance decision | Authenticated task receipt, workspace/Git gates, diff inspection, and parent tests |
| `codex-review` | Independent post-change QA, regression hunting, and adversarial review | A patch author, fixer, or self-approving release gate | Read-only review receipt plus parent reproduction of material findings |
| `agy-antigravity` | Google-stack reconnaissance: Gemini, Firebase, Android, Google Cloud, browser/UI, and Google-specific integration risks | A general implementation worker or source of unverified Google product claims | Plan-mode specialist receipt, links/evidence where applicable, and parent-owned implementation/tests |

Claude Agent Teams are most valuable when workers can operate independently and
need a shared task list or direct messaging; sequential or tightly coupled work
should use one Claude task instead. Antigravity is deliberately narrower: the
CLI supports concurrent background agents, but this adapter keeps the default
specialist lane in plan mode so permissions and Google-specific advice remain
explicit.

## Research basis

- [Claude Agent Teams](https://code.claude.com/docs/en/agent-teams): independent
  contexts, shared tasks, and direct teammate messaging; experimental and more
  expensive than focused subagents.
- [Antigravity CLI features](https://antigravity.google/docs/cli-features):
  asynchronous subagents can perform background research, builds, and tests,
  with an interactive permission surface.
- [Qwen3.5 release](https://qwen.ai/blog?id=qwen3.5): open-weight Qwen3.5 is
  available for local deployment; this repository still constrains the 4B lane
  to bounded work because local capacity and verification are the limiting
  factors.
- [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model):
  GPT-5.6 Sol is the frontier model tier and supports high reasoning effort;
  the lane uses the logged-in Codex CLI session rather than an API key.

## Local confirmation — 2026-08-20

These are fresh local probes, not claims inferred from documentation:

| Lane | Result | Boundary observed |
| --- | --- | --- |
| `local-qwen` | PASS | Ollama was started locally; exact `qwen3.5:4b` was already installed; Codex-over-Ollama completed the bounded smoke in 11.14s, one attempt, with no model pull. |
| `claude-task` | CLI/auth PASS; native-team MCP BLOCKED | Claude OAuth login and a live probe succeeded. MCP exposed the Agent tool, but the configured `general-purpose` agent type was unavailable. Use the explicit CLI fallback until the native agent type is configured. |
| `codex-review` | BLOCKED in this environment | The read-only adapter reached `codex exec review`, but Codex CLI 0.87.0 rejected `gpt-5.6-luna` as requiring a newer CLI; it also reported the configured `127.0.0.1:9000/mcp` endpoint refused the connection. No fallback or model downgrade is allowed. |
| `agy-antigravity` | BLOCKED by permission prompt | `agy` was installed and listed the configured Gemini model. The plan-mode probe reached the CLI, but its permission check for `agy --help` was denied. The adapter must remain fail-closed; grant the CLI permission interactively before treating the lane as live. |

The local result changes routing confidence, not safety boundaries: Qwen is the
only lane currently confirmed end-to-end for this checkout; Claude is the best
authenticated implementation candidate; Codex remains the independent QA
target once its CLI is upgraded; and AGY remains a Google-specialist planner
until its permission-gated smoke passes.
