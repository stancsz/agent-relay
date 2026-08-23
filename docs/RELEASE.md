# Agent Relay release readiness

This is the release gate for the repository. A green unit-test run alone is not
production readiness because the four lanes use different credentials,
permissions, tools, and runtimes.

## Required gates

1. `py -3 -m pytest` passes from a clean checkout.
2. `skills/agent-relay/SKILL.md` passes the Codex skill validator.
3. `agent-relay --help`, `agent-relay lanes --json`, and
   `agent-relay lanes --check --json` work after editable or wheel installation.
4. `agent-relay serve`, `submit`, `inspect`, `watch`, `cancel`, `resume`, and
   the reference `worker --once` path pass the durable coordinator integration
   suite, including restart, idempotency, lease, artifact, and receipt checks.
   The `chain-submit` and `inspect-chain` surfaces must also pass the durable
   predecessor-gating and idempotency checks before claiming follow-up-chain
   readiness.
   `py -3 scripts\acceptance_control_plane.py` must also report `PASS`, proving
   scoped worker enrollment/revocation and stale-lease recovery over HTTP.
   `py -3 scripts\acceptance_tls.py` must report `PASS` before claiming an
   HTTPS LAN coordinator path.
   `py -3 scripts\smoke_claude_interruption.py` must report `PASS` for the
   separate-process Claude worker interruption/reassignment gate before
   claiming Claude adapter recovery locally.
   When Claude credentials are available,
   `py -3 scripts\smoke_claude_cancellation.py` must also prove a real
   worker-confirmed cancellation boundary.
   Retryable Claude bridge transport failures must return the task to `waiting`
   with a released lease and remain bounded by `retry_limit`; a transport error
   must not be reported as a terminal success.
5. `agent-relay skill install --destination <empty-temp-dir>` installs the
   bundled skill and the resulting archive matches `skills/agent-relay/`.
6. The exact local-Qwen model is present and passes
   `agent-relay doctor --codex-smoke` without an implicit model pull.
7. Claude authentication and the selected A2A/native-team mode are probed
   separately; a health response is not proof of a completed task. The
   sandboxed `claude-task` adapter must also pass its parent-owned verification
   test and preserve the caller worktree. Run
   `py -3 scripts\probe_claude_lane.py --timeout 40` and
   `py -3 scripts\smoke_claude_task.py` when the real Claude credentials are
   available; otherwise record the lane as conditional.
8. Codex review is run read-only with the logged-in Codex CLI and the requested
   model; no model downgrade or provider fallback is permitted.
9. AGY is run in plan mode by default; permission-denied or unavailable-model
   receipts remain explicit failures.
10. The complete diff, package contents, and release notes are inspected before
   staging.

The acceptance harness is a loopback protocol and fault-injection gate. It
does not substitute for the physical two-PC LAN gate, real adapter
interruption, or external authentication/model availability checks.

The current checkout has passed the full repository suite, the focused
chain/control-plane tests, and the separate-process acceptance harnesses.

## Compatibility policy

`agent-relay`, `subagent`, and `AR_*` are the supported compatibility surfaces.
New code, documentation, and release commands use Agent Relay, `agent-relay`,
and `agent_relay`.
