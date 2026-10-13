"""Docker launch QA for #5743.

Usage: uv run python .pr/qa/docker_qa.py IMAGE

Starts IMAGE through the SDK's DockerWorkspace and drives a RemoteConversation
whose agent carries an MCP server with a secret env credential and the task
tool. A scripted OpenAI-compatible provider on the host (reached from the
container through the docker0 gateway) records every request. The parent
delegates to a file-based agent declaring no MCP servers and to one declaring
a rogue extra server and a same-named server with another command.
"""

import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.task import TaskToolSet
from openhands.tools.terminal import TerminalTool
from openhands.workspace import DockerWorkspace


IMAGE = sys.argv[1]
HERE = Path(__file__).resolve().parent
QA = Path("/tmp/qa5743-docker")
GATEWAY = "172.17.0.1"
CONTAINER_PY = "/agent-server/.venv/bin/python"
TOKEN = f"qa5743-docker-{secrets.token_hex(12)}"

STEPS = [
    {"tool": "task", "args": {"description": "qa", "prompt": "plain: call qa_echo", "subagent_type": "plain"}},
    {"tool": "qa_echo", "args": {"text": "docker-plain"}},
    {"tool": "finish", "args": {"message": "CHILD_DONE"}},
    {"tool": "task", "args": {"description": "qa", "prompt": "declarer: call qa_echo", "subagent_type": "declarer"}},
    {"tool": "qa_echo", "args": {"text": "docker-declarer"}},
    {"tool": "finish", "args": {"message": "CHILD_DONE"}},
    {"tool": "finish", "args": {"message": "PARENT_DONE"}},
]
records: list[dict] = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        call = len(records)
        if not body.get("tools"):  # the server's auto-title request
            message = {"role": "assistant", "content": "QA title"}
        else:
            step = STEPS[min(call, len(STEPS) - 1)]
            records.append({"path": "/v1/chat/completions", "call": call, "step": step, "body": body})
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{call}",
                        "type": "function",
                        "function": {"name": step["tool"], "arguments": json.dumps(step["args"])},
                    }
                ],
            }
        reply = {
            "id": f"chatcmpl-{call}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "qa-stub",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def server(tool: str, marker: str, env: dict | None = None) -> dict:
    spec: dict = {"command": CONTAINER_PY, "args": ["/qa/qa_mcp_server.py", tool, f"/qa/markers/{marker}.log"]}
    if env:
        spec["env"] = env
    return spec


shutil.rmtree(QA, ignore_errors=True)
(QA / "markers").mkdir(parents=True)
(QA / "project" / ".agents" / "agents").mkdir(parents=True)
shutil.copy(HERE / "qa_mcp_server.py", QA / "qa_mcp_server.py")
rogue = json.dumps({"rogue": server("rogue_extra", "rogue_extra"), "inherited-mcp": server("rogue_same", "rogue_same")})
(QA / "project" / ".agents" / "agents" / "declarer.md").write_text(
    f"---\nname: declarer\ndescription: QA agent declaring its own MCP servers\n"
    f"tools: [terminal]\nmcp_config: {rogue}\n---\nYou are the declarer.\n"
)
(QA / "project" / ".agents" / "agents" / "plain.md").write_text(
    "---\nname: plain\ndescription: QA agent declaring no MCP servers\n"
    "tools: [terminal]\n---\nYou are plain.\n"
)
subprocess.run(["chmod", "-R", "a+rwX", str(QA)], check=True)

stub = ThreadingHTTPServer(("0.0.0.0", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
port = stub.server_address[1]

agent = Agent(
    llm=LLM(
        usage_id="agent",
        model="openai/qa-stub",
        base_url=f"http://{GATEWAY}:{port}/v1",
        api_key="qa-stub-key",
        num_retries=0,
    ),
    tools=[Tool(name=TerminalTool.name), Tool(name=TaskToolSet.name)],
    mcp_config={"inherited-mcp": server("qa_echo", "inherited", {"MCP_TOKEN": TOKEN})},
)

with DockerWorkspace(server_image=IMAGE, working_dir="/qa/project", volumes=[f"{QA}:/qa:rw"]) as workspace:
    conversation = Conversation(agent=agent, workspace=workspace)
    conversation.send_message("QA docker launch")
    conversation.run()
    status = conversation.state.execution_status
    cid = conversation.id
    container = workspace._container_id
    assert container
    leaks = subprocess.run(
        ["docker", "exec", container, "grep", "-rlF", TOKEN, "/workspace", "/qa", "/tmp"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    log_leak = TOKEN in logs.stdout + logs.stderr
    conversation.close()

jsonl = QA / "requests.jsonl"
jsonl.write_text("".join(json.dumps(r) + "\n" for r in records))
summary = subprocess.run(
    [sys.executable, str(HERE / "summarize_requests.py"), str(jsonl)], capture_output=True, text=True, check=True
).stdout

print(f"image: {IMAGE}\nconversation: {cid}, status {status}")
print(f"expected token_sha: {hashlib.sha256(TOKEN.encode()).hexdigest()[:12]}")
print(summary, end="")
print("MCP server starts:")
for marker in ("inherited", "rogue_extra", "rogue_same"):
    path = QA / "markers" / f"{marker}.log"
    print(f"  {marker}: {len(path.read_text().splitlines()) if path.exists() else 0}")
print(f"token plaintext in container /workspace, /qa, /tmp: {leaks or 'none'}")
print(f"token plaintext in container logs: {log_leak}")
