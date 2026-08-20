# Handoff contract

The relay emits one bounded JSON result. `accepted_by_transport` means only that the MCP/A2A handoff completed mechanically; it is not a claim that the code is correct.

Required result evidence includes:

- `protocol`, `task_id`, `target_role`, `status`, `output`, `changed_paths`, `context_digest`
- `evidence[]` with transport and worktree summaries
- `server_receipt.transport`, MCP protocol/server/tool data, and `accepted_by_transport`
- `server_receipt.team_mode`, `team_name`, `team_complete`, and `native_team_tools_available` for team runs
- `server_receipt.native_team_mode` (`implicit-agent` or `legacy-create-delete`) and `legacy_team_tools_available`
- `server_receipt.before_head`, `after_head`, `worktree_changed`, `verifier_clean`, and `change_expectation_satisfied`

Interpretation:

- `done` means the protocol, transport, requested member receipts, and mechanical Git gates passed. It is still not release approval.
- `blocked` or `failed` means inspect the complete worktree before retrying. A failed handoff can leave a real draft.
- `native_team_tools_available: true` means the installed MCP server exposed the expected tools. The stronger proof is `team_complete: true` with bounded teammate receipts.
- Any verifier-containing run with a content-level worktree fingerprint change is rejected.
- `branch`/`HEAD` changes, unexpected paths, unrequested side effects, or missing evidence require human review before staging.
- The bridge deliberately does not report or override model, base URL, credentials, budgets, or conversation history. Those remain outside the task packet.
- Durable job records may include the bounded task packet, heartbeat, attempt count, and bounded result receipt. They never include a transcript or repository dump.
