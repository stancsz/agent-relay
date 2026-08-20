import json
import os
import sys
from pathlib import Path

team_file = Path(os.environ["FAKE_TEAM_FILE_PATH"])
inbox = team_file.parent / "inboxes" / "team-lead.json"

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-claude-modern", "version": "test"}}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "description": "fake modern native team tool", "inputSchema": {"type": "object"}} for name in ("Agent", "TaskCreate", "TaskUpdate", "TaskList", "SendMessage")]}
    elif method == "tools/call":
        name = request.get("params", {}).get("name")
        arguments = request.get("params", {}).get("arguments", {})
        if name == "Agent":
            team_file.parent.mkdir(parents=True, exist_ok=True)
            inbox.parent.mkdir(parents=True, exist_ok=True)
            if not team_file.exists():
                team_file.write_text(json.dumps({"name": "session-modern-team", "members": [{"name": "team-lead"}]}), encoding="utf-8")
                inbox.write_text("[]", encoding="utf-8")
            messages = json.loads(inbox.read_text(encoding="utf-8"))
            member = arguments.get("name", "unknown")
            messages.append({"from": member, "text": f"A2A_RESULT FAKE_MODERN_TEAM_{member}", "summary": "fake modern team result", "read": False})
            inbox.write_text(json.dumps(messages), encoding="utf-8")
            value = {"status": "teammate_spawned", "name": member, "team_name": team_file.parent.name, "teammate_id": f"{member}@session-modern-team", "agent_type": "general-purpose"}
        elif name == "TaskCreate":
            value = {"task": {"id": "modern-fake-task"}}
        elif name in {"TaskUpdate", "TaskList", "SendMessage"}:
            value = {"ok": True}
        else:
            value = {"error": f"unknown fake tool: {name}"}
        result = {"content": [{"type": "text", "text": json.dumps(value)}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()
