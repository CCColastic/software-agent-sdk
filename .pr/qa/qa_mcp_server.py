"""Stdio FastMCP server for the #5743 QA.

Usage: python qa_mcp_server.py TOOL_NAME MARKER_FILE

Serves one tool, TOOL_NAME(text). The reply carries a short SHA-256 of the
MCP_TOKEN the server was started with, so credential delivery is visible
without the token itself ever entering events or logs. Every start appends a
line to MARKER_FILE, so the QA can tell whether anything connected.
"""

import hashlib
import os
import sys
from datetime import UTC, datetime

from fastmcp import FastMCP


tool_name, marker = sys.argv[1], sys.argv[2]
with open(marker, "a") as f:
    f.write(f"{datetime.now(UTC).isoformat()} pid={os.getpid()} {tool_name}\n")

mcp = FastMCP(f"qa-{tool_name}")


@mcp.tool(name=tool_name)
def echo(text: str) -> str:
    """Echo text with a fingerprint of the token this server received."""
    token = os.environ.get("MCP_TOKEN", "")
    digest = hashlib.sha256(token.encode()).hexdigest()[:12] if token else "none"
    return f"QA_MCP:{tool_name}:{text}:token_sha={digest}"


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
