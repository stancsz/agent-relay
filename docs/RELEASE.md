# Agent Relay release readiness

This is the release gate for the repository. A green unit-test run alone is not
production readiness because the four lanes use different credentials,
permissions, tools, and runtimes.

## Required gates

1. `py -3 -m pytest` passes from a clean checkout.
2. `skills/agent-relay/SKILL.md` passes the Codex skill validator.
3. `agent-relay --help` and `agent-relay lanes --json` work after editable or
   wheel installation.
4. The exact local-Qwen model is present and passes
   `agent-relay doctor --codex-smoke` without an implicit model pull.
5. Claude authentication and the selected A2A/native-team mode are probed
   separately; a health response is not proof of a completed task.
6. Codex review is run read-only with the logged-in Codex CLI and the requested
   model; no model downgrade or provider fallback is permitted.
7. AGY is run in plan mode by default; permission-denied or unavailable-model
   receipts remain explicit failures.
8. The complete diff, package contents, and release notes are inspected before
   staging.

## Compatibility policy

`lcd`, `subagent`, `LCD_*`, and the historical evaluation artifact names are
compatibility surfaces. New code, documentation, and release commands use
Agent Relay, `agent-relay`, and `agent_relay`.
