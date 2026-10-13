"""Summarize the provider requests an llm-stub received.

Usage: python summarize_requests.py STUB_JSONL [FIRST_CALL [LAST_CALL]]

Prints one JSON line per chat completion in the call range: the stub call
number, who sent it (the parent conversation, whose first user message is the
"QA <scenario>" prompt, or a sub-agent, identified by its task prompt), the
scripted step it answered with, the tool names offered to the provider, QA MCP
tool results already in its messages, and the latest delegation tool result.
"""

import json
import sys


DELEGATION = {"task", "workflow", "workflow_tool_set"}


def texts(content):
    if isinstance(content, str):
        return [content]
    return [c.get("text", "") for c in content or []]


path = sys.argv[1]
first = int(sys.argv[2]) if len(sys.argv) > 2 else 0
last = int(sys.argv[3]) if len(sys.argv) > 3 else 1 << 30

for line in open(path):
    record = json.loads(line)
    call = record["call"]
    if record.get("path") != "/v1/chat/completions" or not first <= call <= last:
        continue
    messages = record["body"]["messages"]
    prompt = next(" ".join(texts(m["content"])) for m in messages if m["role"] == "user")
    names = {
        tc["id"]: tc["function"]["name"]
        for m in messages
        for tc in m.get("tool_calls") or []
    }
    mcp_results, delegation = [], None
    for m in messages:
        if m.get("role") != "tool":
            continue
        body = " ".join(texts(m.get("content")))
        if "QA_MCP:" in body:
            mcp_results.append(body)
        if names.get(m.get("tool_call_id")) in DELEGATION:
            delegation = body[:240]
    out = {
        "call": call,
        "sender": "parent" if prompt.startswith("QA ") else f"sub-agent ({prompt[:40]!r})",
        "answered_with": record["step"],
        "tools": sorted(t["function"]["name"] for t in record["body"].get("tools") or []),
        "qa_mcp_results": mcp_results,
    }
    if delegation:
        out["last_delegation_result"] = delegation
    print(json.dumps(out))
