import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-claude", "version": "test"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "Agent",
                "description": "fake agent",
                "inputSchema": {"type": "object", "properties": {"description": {"type": "string"}, "prompt": {"type": "string"}}},
            }]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "FAKE_MCP_AGENT_OK"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()
