# Agent Relay MCP

Agent Relay exposes a streamable HTTP MCP endpoint at `/mcp`. It has two
related surfaces:

1. The durable coordinator tools (`submit`, `run`, `dispatch`, `inspect`,
   `watch`, `cancel`, and `chain_submit`) enqueue bounded tasks and return
   durable receipts.
2. The direct-agent tools (`agent_status` and `invoke_agent`) run one local
   Gemini, Codex, or Claude Code CLI process and return a bounded response.

The direct surface is for short consultations, planning, and reviews. Use the
durable surface for repository edits, retries, cancellation, parent inputs, and
independent acceptance gates.

## Start it

The MCP server still needs a coordinator, unless a caller only needs the
direct tools. Start the coordinator and MCP façade in separate terminals:

```powershell
agent-relay serve --db .\relay.sqlite3 --port 8788 --token $env:AR_RELAY_AUTH_TOKEN

agent-relay mcp `
  --coordinator-url http://127.0.0.1:8788 `
  --coordinator-token $env:AR_RELAY_AUTH_TOKEN `
  --token $env:AR_RELAY_AUTH_TOKEN `
  --agent-repo C:\work\repo `
  --port 8789
```

The MCP endpoint requires the bearer token when `--token` is set. Non-loopback
binds also require a token. Keep the endpoint on loopback unless HTTPS and an
appropriate network policy are in place.

The direct-agent configuration can also be supplied through environment
variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AR_MCP_AGENT_REPO` | Direct-agent workspace root | current directory |
| `AR_MCP_AGENT_TIMEOUT_SECONDS` | Default and maximum direct-call timeout | `300` |
| `AR_MCP_AGENT_MAX_OUTPUT_CHARS` | Response cap | `12000` |
| `AR_MCP_AGENT_CONCURRENCY` | Simultaneous direct calls | `2` |
| `AR_MCP_ALLOW_AGENT_WRITES` | Permit explicit `workspace-write` calls | `false` |
| `AR_GEMINI_TRANSPORT` | `auto`, `gemini`, or `agy` | `auto` |
| `AR_GEMINI_BIN` | Direct Gemini CLI override | discovered `gemini.cmd`/`gemini` |
| `AR_GEMINI_MODEL` | Direct Gemini or agy model override | `gemini-3.1-pro-high` |
| `AR_CODEX_BIN` | Codex CLI override | discovered `codex.cmd`/`codex` |
| `AR_CLAUDE_BIN` | Claude Code CLI override | discovered `claude.cmd`/`claude` |
| `AR_MCP_CODEX_MODEL` | Optional direct Codex model | CLI default |
| `AR_MCP_CLAUDE_MODEL` | Optional direct Claude model | CLI default |

## Direct-agent tools

### `agent_status`

Returns executable discovery for all three logical lanes. A `ready` result
means an executable was found; it does not prove that the account is logged in
or that a request will succeed. It never starts an agent.

### `invoke_agent`

Required arguments:

```json
{
  "agent": "gemini",
  "prompt": "Review the repository's test strategy and report concrete gaps."
}
```

Optional arguments:

```json
{
  "agent": "codex",
  "prompt": "Inspect the bounded implementation and report regressions.",
  "workdir": "src",
  "mode": "read-only",
  "model": "gpt-5.6-sol",
  "timeout_seconds": 120
}
```

`agent` is one of `gemini`, `codex`, or `claude`. `workdir` must be an existing
directory beneath `--agent-repo` (or `AR_MCP_AGENT_REPO`). The prompt is capped
at 24,000 characters, the response is capped by `AR_MCP_AGENT_MAX_OUTPUT_CHARS`,
and each process is terminated at its timeout.

`mode` defaults to `read-only` and is enforced with the backend's CLI controls:

| Logical agent | Default transport | Read-only controls |
| --- | --- | --- |
| `gemini` | direct Gemini CLI or usable `agy` fallback | Gemini plan mode, or agy plan mode |
| `codex` | Codex CLI | `exec --sandbox read-only --ephemeral` |
| `claude` | Claude Code CLI | `--permission-mode plan --allowed-tools Read --no-session-persistence` |

`workspace-write` is rejected unless the server is explicitly started with
`--allow-agent-writes` (or `AR_MCP_ALLOW_AGENT_WRITES=true`). Even then, writes
start from the configured workspace root and are subject to the selected CLI's
own permission/sandbox behavior plus the prompt policy. Direct writes do not
receive Agent Relay's durable patch, scope, sandbox, or parent-verification
guarantees; prefer `submit` with a proper task contract for implementation work.

Every invocation returns a normalized receipt with `agent`, `transport`,
`status`, `summary`, `response`, `return_code`, `duration_seconds`, and bounded
`runtime` metadata. `PASS` means the process exited successfully with a nonempty
response. It is not independent verification of the response or of any file
changes.

## Gemini readiness on the development machine

There are two different Google paths and they should not be conflated:

- The installed direct `@google/gemini-cli` was version `0.26.0` at the time of
  this check. Its CLI and cached credentials were present, but a live one-shot
  failed because the account requires `GOOGLE_CLOUD_PROJECT` or
  `GOOGLE_CLOUD_PROJECT_ID`.
- The installed `agy.exe` path completed a bounded one-shot in plan mode with
  `gemini-3.1-pro-high` and returned `AGY_RELAY_PROBE_OK`. Therefore `agy` is
  the currently usable Gemini-backed transport on this machine.

The direct Gemini read-only transport requests Gemini CLI
`--approval-mode plan`; that mode must be enabled in the local Gemini CLI
configuration. `auto` therefore selects `agy` when the direct CLI is present
but no Google Cloud project is configured. This is a transport fallback, not
proof that every Gemini account or model is available.

With `AR_GEMINI_TRANSPORT=auto`, the logical `gemini` lane prefers the direct
CLI only when it is explicitly configured or a Google Cloud project is
present. Otherwise it uses the discovered `agy` transport. Set
`AR_GEMINI_TRANSPORT=gemini` to require the direct CLI, or `agy` to require the
working Gemini-backed path explicitly. The `agent_status` result reports which
transport was selected, while only an actual `invoke_agent` receipt proves a
particular call completed.

## Examples

After connecting an MCP client, ask a read-only Gemini-backed specialist:

```json
{
  "name": "invoke_agent",
  "arguments": {
    "agent": "gemini",
    "prompt": "Inspect only the relevant files. Explain the smallest safe change and list the checks the parent should run.",
    "mode": "read-only"
  }
}
```

Ask Codex for a bounded review:

```json
{
  "name": "invoke_agent",
  "arguments": {
    "agent": "codex",
    "prompt": "Review the current diff for correctness, security, and missing regression tests. Do not edit.",
    "mode": "read-only"
  }
}
```

Ask Claude Code for a read-only second opinion:

```json
{
  "name": "invoke_agent",
  "arguments": {
    "agent": "claude",
    "prompt": "Review the MCP adapter contract and identify edge cases. Do not edit files.",
    "mode": "read-only"
  }
}
```

For a complete MCP initialize flow, use `initialize`, retain the returned
`MCP-Session-Id`, send `notifications/initialized`, then call `tools/list` and
`tools/call`. The existing protocol tests exercise this session behavior.
