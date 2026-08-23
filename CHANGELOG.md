# Changelog

## 0.1.0 — 2026-08-20

- Renamed the project identity to **Agent Relay**.
- Renamed the canonical Python module from `local_code_delegate` to
  `agent_relay`.
- Added the canonical `agent-relay` console command while retaining the
  `subagent` compatibility alias.
- Standardized live configuration, provider IDs, and runtime artifact prefixes
  on the Agent Relay `AR_*`/`ar-*` namespace; historical evaluation payloads
  remain unchanged for provenance.
- Consolidated the unified agent-harness skill under `skills/agent-relay/`.
- Documented the distinction between LLM-level model routing and
  agent-harness-level relay/routing.
