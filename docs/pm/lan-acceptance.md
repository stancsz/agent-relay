# Physical two-PC LAN acceptance

This runbook is the remaining release gate after
`py -3 scripts\acceptance_control_plane.py` passes. It proves that the same
protocol works across an actual network boundary and that a worker interruption
does not become a false success.

## PC A — coordinator and client

Use a long random value for both the coordinator bearer and the worker's
scoped credential. Keep them out of task JSON and shell transcripts where
possible.

```powershell
$env:AR_RELAY_AUTH_TOKEN = '<admin-token>'
$env:AR_RELAY_CA_CERT = 'C:\agent-relay\lan-ca.pem'
agent-relay serve --host 0.0.0.0 --port 8788 `
  --db C:\agent-relay\relay.sqlite3 `
  --tls-cert C:\agent-relay\server-chain.pem `
  --tls-key C:\agent-relay\server-key.pem
```

The certificate must contain a name/IP that PC B uses to reach PC A, and
`lan-ca.pem` must be the issuing CA (or a trusted CA bundle). Do not disable
certificate verification. Allow inbound TCP 8788 only from PC B's trusted LAN
address. From the client shell on PC A:

```powershell
agent-relay agents --url https://<pc-a-ip>:8788 --token $env:AR_RELAY_AUTH_TOKEN --json
agent-relay submit --url https://<pc-a-ip>:8788 --token $env:AR_RELAY_AUTH_TOKEN `
  --task .\task.json --idempotency-key lan-acceptance-001 --json
agent-relay watch lan-acceptance-001 --url https://<pc-a-ip>:8788 `
  --token $env:AR_RELAY_AUTH_TOKEN --stream --json
```

Submit the same command a second time. The second response must report
`created: false` and the same task ID.

## PC B — enrolled worker

Use a clean checkout and a worker-scoped credential. The admin token is needed
for first enrollment; subsequent worker mutations are actor-bound to the
scoped credential.

```powershell
$env:AR_RELAY_AUTH_TOKEN = '<admin-token>'
$env:AR_RELAY_AGENT_TOKEN = '<worker-b-token>'
$env:AR_RELAY_CA_CERT = 'C:\agent-relay\lan-ca.pem'
agent-relay worker --url https://<pc-a-ip>:8788 `
  --token $env:AR_RELAY_AUTH_TOKEN `
  --agent-token $env:AR_RELAY_AGENT_TOKEN `
  --worker-id pc-b-worker --backend claude-task `
  --repo C:\work\repo --poll-seconds 1
```

For an existing Claude MCP service on PC B, use the explicit remote-output
lane instead. The MCP workdir is interpreted by that service, not by the
coordinator or the worker's local checkout:

```powershell
$env:AR_CLAUDE_MCP_URL = 'http://127.0.0.1:8000/mcp'
$env:AR_CLAUDE_MCP_WORKDIR = '/Users/<user>/work/repo'
$env:AR_CLAUDE_MCP_AUTH_TOKEN = '<optional-mcp-token>'
# Only for a trusted private-LAN HTTP service; prefer HTTPS otherwise.
$env:AR_CLAUDE_MCP_ALLOW_INSECURE_LAN = '1'
agent-relay worker --url https://<pc-a-ip>:8788 `
  --token $env:AR_RELAY_AUTH_TOKEN `
  --agent-token $env:AR_RELAY_AGENT_TOKEN `
  --worker-id pc-b-claude-mcp --backend claude-mcp `
  --repo C:\work\repo --poll-seconds 1 --claim-next
```

This lane records the MCP endpoint, transport, remote host/workdir, and
remote-output verification authority in the receipt. It intentionally does
not claim that PC B's filesystem was sandboxed or that Agent Relay verified a
local patch.

PC A should show the Agent Card as `unknown` until a bounded task completes;
then it should become `ready` on success or `degraded` on adapter failure.

## Required interruption checks

1. Submit a task with a lease long enough to observe execution.
2. Confirm PC B has acquired it and PC A sees `running` plus a worker owner.
3. Disconnect the client or stop its terminal. Reconnect with `watch` or
   `inspect`; do not resubmit. The task ID and event history must be unchanged.
4. Stop the worker process during execution. Wait for the lease to expire.
5. Restart the same worker identity. The task must be reassigned or become
   explicitly `waiting`/`blocked`; it must never report `succeeded` without a
   receipt from the restarted worker.
6. Inspect the terminal receipt and artifact from PC A after restarting the
   coordinator process against the same SQLite database.
7. Revoke the worker from PC A and confirm the worker's next heartbeat/list or
   mutation receives HTTP 401.

For a cancellation test, issue `agent-relay cancel` while the task is running.
Claude should return a worker-confirmed `cancelled` receipt when its bridge
stops the job. A non-stoppable adapter must return `blocked` with
`execution_stopped: false`; neither path may claim `succeeded` after the
cancellation request.

Record the exact OS versions, repository revision, adapter/backend, model,
lease duration, interruption timestamps, task ID, event history, artifact
hash, and receipt. A successful loopback harness is necessary but is not
sufficient evidence for this physical gate.
