# Remote Claude/A2A QA report

## Scope

This report validates the feature described as:

> An orchestrator submits bounded work to a worker on another machine, the worker lets its Claude/MCP runtime execute continuously, and the orchestrator can observe, recover, cancel, verify, and receive a durable result.

The current checkout proves the coordinator/worker protocol and Claude bridge on
one Windows host using real HTTP processes and deterministic fixtures. It also
proves direct remote MCP transport to a reachable neighboring service, but does
not claim the full physical two-PC Agent Relay coordinator/worker scenario; that
final gate is documented in [`lan-acceptance.md`](lan-acceptance.md).

## Results

| Check | Result | What it proves |
| --- | --- | --- |
| `py -3 -B lanes/claude-task/scripts/test_claude_a2a.py` | PASS | Claude A2A protocol, auth, durable jobs, profiles, schedules, fake MCP modern/legacy paths, fallback, and worktree gates. |
| `py -3 -m pytest -q tests/test_claude_task.py tests/test_lanes.py tests/test_cli.py` | PASS | Agent Relay Claude adapter, lane registration, and CLI integration. |
| `py -3 -m pytest -q tests/test_worker_plane_e2e.py` | PASS | Real HTTP coordinator + enrolled worker + lease + Claude backend boundary + artifact + receipt + SSE replay. |
| `py -3 -m pytest -q tests/test_claude_task.py tests/test_claude_a2a_regressions.py` | PASS | Local Claude adapter, remote-daemon dispatch settings, returned patch evidence, and async task-ID digest replay protection. |
| `py -3 -m pytest -q tests/test_claude_task.py::test_claude_task_executes_through_a_real_remote_a2a_daemon` | PASS | Agent Relay submits to a real in-process HTTP Claude A2A daemon, polls the durable job, authenticates it, and maps the returned receipt. |
| `py -3 -m pytest -q tests/test_store.py tests/test_worker_plane.py` | PASS | Cancellation/lease-expiry safety and backend/capability-aware worker claiming. |
| `py -3 -m pytest -q tests/test_store.py tests/test_control.py tests/test_protocol.py` | PASS | Restart-safe chain creation, predecessor terminal gating, bounded parent inputs, and idempotent HTTP child submission. |
| `py -3 -m pytest -q tests/test_store.py tests/test_control.py tests/test_worker_plane.py` | PASS | Deferred chain auto-materialization, request-hash idempotency, scoped worker access, receipt artifact validation, and declared parent-input fetch. |
| `py -3 -m pytest -q tests/test_store.py tests/test_control.py` | PASS | Coordinator-owned priority/deadline-compatible claim selection, expiry handling, and explicit no-work response. |
| `py -3 -m pytest -q tests/test_mcp.py` | PASS | MCP initialization, tool discovery, durable submit/inspect/cancel, bounded dispatch, chain submission, optional local-worker execution snapshot, session validation, and non-loopback auth guard. |
| `py -3 -m pytest -q tests/test_claude_mcp.py` | PASS | Existing-Claude-MCP session lifecycle, prompt translation, remote output mapping, truthful authority metadata, and failure propagation. |
| Live `claude-mcp` readiness plus read-only durable task through `10.0.0.207:8000/mcp` | PASS | Agent Relay discovered the real `mcp-runner` service, claimed a queued task, received `READY`, and persisted a `succeeded` remote-MCP receipt. |
| `py -3 -m pytest -q --ignore=tests/test_eval_suite.py` | PASS | Full non-evaluation repository regression suite. |
| `py -3 -m pytest -q` | PASS | Full repository regression suite, including the aggregate evaluation suite. |
| `py -3 scripts/acceptance_control_plane.py` | PASS | Separate-process coordinator idempotency, expired lease reassignment, stale-worker rejection, artifact/receipt persistence, restart, scoped credential survival, and revocation. |
| `py -3 scripts/acceptance_tls.py` | PASS | TLS health, authenticated request, and unauthenticated rejection. |
| `py -3 scripts/probe_claude_lane.py --timeout 40` | PASS | Live Claude MCP capability discovery. The installed runtime exposes `Agent` but not the native team task tools. |
| `py -3 scripts/smoke_claude_task.py` | PASS | Real bounded Claude edit, parent-owned verification, scope gate, and unchanged caller worktree. Transport was explicitly labeled `cli-fallback`. |
| `py -3 scripts/smoke_claude_cancellation.py` | PASS | Running → cancel requested → worker-confirmed cancelled with `execution_stopped: true`. |
| `py -3 scripts/smoke_claude_interruption.py` | PASS | Separate worker interruption → lease expiry → reassignment → fresh verified Claude success receipt. |
| `py -3 -m compileall -q src tests evals lanes/claude-task/scripts` | PASS | Repository Python sources compile cleanly. |
| `py -3 scripts/validate_skill_package.py ...` (both archives) | PASS | Source skill and wheel-embedded skill archives match exactly. |
| `py -3 scripts/smoke_claude_interruption.py` | PASS | Separate worker process is killed after `running`; lease expiry, reassignment, fresh Claude execution, verification, and unchanged caller worktree are observed. |
| `py -3 -B -m py_compile ...` | PASS | Claude bridge modules compile cleanly. |

## What is now validated

- The orchestrator can create one durable logical task and avoid duplicate submission.
- A worker can register with a scoped credential, acquire a lease, renew ownership, execute, upload an artifact, and submit a terminal receipt.
- The coordinator persists events and artifacts and serves them after reconnect/restart.
- The client can observe progress through replayable JSON events or bounded SSE.
- Claude execution is bounded by workspace/task policy and parent-owned verification.
- Cancellation does not become false success: the final receipt distinguishes a confirmed stop from an unproven stop.
- Lease-renewal loss now signals cancellation-capable adapters and fences the stale worker from reporting success; coordinator expiry/reassignment remains authoritative.
- Native Claude team capability is probed rather than assumed. When unavailable, the fallback is explicit and not reported as a native team run.
- An existing remote Claude A2A daemon can now be selected for actual execution through `AR_CLAUDE_A2A_SERVER_URL`, with bearer authentication, HTTPS CA support, bounded async polling, cancellation, and returned patch evidence.
- The remote-daemon adapter path has a real HTTP integration test; it is not only a mocked transport boundary. The test uses the vendored daemon and deterministic MCP fixture, so it proves protocol wiring and receipt mapping rather than external Claude quality.
- Scoped workers cannot claim a task whose explicit backend/capability policy is incompatible with their enrolled Agent Card; the coordinator enforces this before lease grant rather than relying only on worker-side filtering.
- The coordinator can now select the highest-priority compatible task through `POST /tasks/claim`, using earliest deadline and FIFO creation order as tie-breakers; overdue queued work receives a durable expiry receipt, while live leases and incompatible policies are skipped safely.
- Durable follow-up chains are locally validated: a child step is created exactly once after an allowed terminal predecessor, survives store restart, and carries only explicitly declared parent artifact references and bounded messages.
- Deferred chain recipes are locally validated: terminal completion materializes the child exactly once, malformed activation cannot roll back the parent terminal state, restart reconciliation is available, and the worker fetches only hash-verified declared parent inputs.
- The CLI exposes `watch-chain` for bounded operator polling of ordered step state and pending blockers.
- The MCP façade exposes the same durable lifecycle through `submit`/`run`/`Agent`, bounded `dispatch`, `inspect`, `watch`, `cancel`, and `chain_submit` tools; the façade delegates to the authenticated coordinator rather than creating a second state store.
- The explicit `claude-mcp` worker backend can route a durable read-only task to an existing streamable-HTTP Claude MCP service; receipts include the endpoint, transport, and remote-only verification authority rather than claiming a local sandbox.

## What is not yet proven

- A real PC-A to PC-B run across a physical LAN.
- Real certificate/CA provisioning, firewall rules, and network interruption behavior.
- A physical PC-A to PC-B Claude worker interruption, allowing lease expiry, restarting it, and proving safe reassignment over the LAN.
- A native Claude Agent Team run in the current installed Claude version; the live probe reports `Agent` but not `TaskCreate`/`TaskUpdate`/native team tools, so the successful smoke is a bounded CLI fallback.
- Automatic multi-step orchestrator chaining where the result of one job causes the next job to be submitted is not implemented yet.
- Side-effect-aware retry for arbitrary non-Git external actions.

## Release conclusion

The single-job feature is implemented and locally/separately end-to-end validated, including execution through an existing remote Claude A2A daemon. It is not yet physically LAN-accepted. The correct release status is **LAN-ready candidate, external two-PC acceptance pending**, not “fully proven multi-PC production release.”
