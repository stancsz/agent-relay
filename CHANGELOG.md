# Changelog

## Unreleased — 2026-08-21

- Hardened the Windows CLI fallback for restrictive execution policies, direct
  `claude.exe` resolution, JSON target-path forwarding, and minimal PowerShell
  hosts without `Get-FileHash`.
- Preserved disjoint team semantics in fallback mode by running every declared
  worker with member-scoped target paths before the read-only verifier.
- Relaxed verifier status checks to distinguish pre-existing dirty-worktree
  entries from new changes introduced during verification.
- Added the `CLAUDE_A2A_TIMEOUT_SECONDS` launcher override so long-running CLI
  fallback lanes can select a bounded per-process timeout without editing the
  launcher or relying on a hidden hard-coded value.

## 0.1.0 — 2026-08-20

- Renamed the project identity to **Agent Relay**.
- Renamed the canonical Python module from `local_code_delegate` to
  `agent_relay`.
- Added the canonical `agent-relay` console command while retaining `lcd` and
  `subagent` compatibility aliases.
- Consolidated the unified agent-harness skill under `skills/agent-relay/`.
- Documented the distinction between LLM-level model routing and
  agent-harness-level relay/routing.
