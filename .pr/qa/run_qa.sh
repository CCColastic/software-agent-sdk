#!/usr/bin/env bash
# QA for #5743: delegated sub-agents inherit exactly the parent's MCP tools.
#
# Usage: run_qa.sh CHECKOUT LABEL
#
# Launches an isolated, authenticated Agent Server from CHECKOUT with
# control-agent-server, stores an MCP server (with a secret env credential)
# and parent agent profiles through the REST API, and drives every delegation
# entry point with a scripted OpenAI-compatible provider (llm-stub). The real
# MCP discovery, delegation and tool execution paths run unchanged. Results
# (per-request tool lists, MCP tool results, server-start markers, credential
# leak scans) land in $RUN/qa5743/results.
set -u

CHECKOUT=$(cd "$1" && pwd)
LABEL=$2
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
CAS="$REPO/.agents/skills/verify-agent-server/scripts/control-agent-server"
PY="$REPO/.venv/bin/python"
SERVER="$HERE/qa_mcp_server.py"

export AGENT_SERVER_VERIFY_RUN
AGENT_SERVER_VERIFY_RUN=$($CAS launch --new --print-run --checkout "$CHECKOUT" --name "qa5743-$LABEL" </dev/null)
RUN=$AGENT_SERVER_VERIFY_RUN
Q=$RUN/qa5743
M=$Q/markers
R=$Q/results
WS=$Q/ws
PLUGIN=$Q/plugin-qa
mkdir -p "$M" "$R" "$WS/.agents/agents" "$PLUGIN/.plugin" "$PLUGIN/agents"
echo "run: $RUN (checkout $CHECKOUT, HEAD $(git -C "$CHECKOUT" rev-parse --short HEAD))" | tee "$R/00-run.txt"

TOKEN="qa5743-mcp-$(openssl rand -hex 12)"
PLUGIN_TOKEN="qa5743-plugin-$(openssl rand -hex 12)"
sha() { printf %s "$1" | sha256sum | cut -c1-12; }
echo "expected token_sha: settings=$(sha "$TOKEN") plugin=$(sha "$PLUGIN_TOKEN")" | tee -a "$R/00-run.txt"

server_json() { # TOOL MARKER [ENV_JSON]
  printf '{"command":"%s","args":["%s","%s","%s"]%s}' "$PY" "$SERVER" "$1" "$M/$2.log" "${3:+,\"env\":$3}"
}

# --- Fixtures ---------------------------------------------------------------
STUB=$($CAS fixture llm-stub --name qa-stub --step 'reply:unused' --print-path </dev/null)
$CAS llm set --profile qa-stub --model openai/qa-stub --base-url "$STUB/v1" --no-api-key --num-retries 0 --no-validate </dev/null >/dev/null

# The parent's MCP server, stored in settings with a secret credential.
$CAS api POST /api/settings/mcp/inherited-mcp --expect 201 --quiet \
  --json "$(server_json qa_echo inherited "{\"MCP_TOKEN\":\"$TOKEN\"}")" </dev/null >/dev/null

# File-based sub-agents. `declarer` declares an extra server and a same-named
# server with another command; `nester` delegates again (grandchild).
"$PY" - "$WS/.agents/agents" "$(server_json rogue_extra rogue_extra)" "$(server_json rogue_same rogue_same)" <<'EOF'
import json, sys
from pathlib import Path
d, extra, same = Path(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3])
mcp = json.dumps({"rogue": extra, "inherited-mcp": same})
(d / "declarer.md").write_text(
    f"---\nname: declarer\ndescription: QA agent declaring its own MCP servers\n"
    f"tools: [terminal]\nmcp_config: {mcp}\n---\nYou are the declarer.\n"
)
(d / "nester.md").write_text(
    f"---\nname: nester\ndescription: QA agent that delegates again\n"
    f"tools: [task_tool_set]\nmcp_config: {mcp}\n---\nYou delegate.\n"
)
(d / "leaf.md").write_text(
    f"---\nname: leaf\ndescription: QA grandchild with no built-in tools\n"
    f"tools: []\nmcp_config: {mcp}\n---\nYou are the leaf.\n"
)
EOF

# A plugin that adds an MCP server whose credential is a ${VAR} secret, and a
# plugin agent declaring its own rogue server.
"$PY" - "$PLUGIN" "$(server_json plugin_echo plugin '{"MCP_TOKEN":"${QA_PLUGIN_TOKEN}"}')" "$(server_json rogue_plugin rogue_plugin)" <<'EOF'
import json, sys
from pathlib import Path
p, plugin_server, rogue = Path(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3])
(p / ".plugin" / "plugin.json").write_text(json.dumps({"name": "qa-plugin", "version": "1.0.0", "description": "QA"}))
(p / ".mcp.json").write_text(json.dumps({"mcpServers": {"plugin-mcp": plugin_server}}))
(p / "agents" / "plugged.md").write_text(
    "---\nname: plugged\ndescription: QA plugin agent\ntools: [terminal]\n"
    f"mcp_config: {json.dumps({'rogue-plugin': rogue})}\n---\nYou are plugged.\n"
)
EOF

profile() { # NAME TOOLS_CSV REFS_JSON
  local tools
  tools=$(printf '%s' "$2" | "$PY" -c 'import json,sys; print(json.dumps([{"name": t} for t in sys.stdin.read().split(",")]))')
  $CAS api POST "/api/agent-profiles/$1" --expect 201 --quiet \
    --json "{\"llm_profile_ref\":\"qa-stub\",\"tools\":$tools,\"mcp_server_refs\":$3}" </dev/null >/dev/null
}
profile qa-task terminal,file_editor,task_tool_set '["inherited-mcp"]'
profile qa-wfset terminal,file_editor,workflow_tool_set '["inherited-mcp"]'
profile qa-wf terminal,file_editor,workflow '["inherited-mcp"]'
profile qa-empty terminal,file_editor,task_tool_set '[]'

# --- Scenarios --------------------------------------------------------------
CALLS=0
scenario() { # NAME PROFILE BODY_JSON STEP...
  local name=$1 prof=$2 body=$3
  shift 3
  # The stub indexes its script by a global call counter: pad past used calls.
  local steps=()
  for ((i = 0; i < CALLS; i++)); do steps+=(--step 'reply:pad'); done
  for s in "$@"; do steps+=(--step "$s"); done
  $CAS fixture llm-stub --name qa-stub "${steps[@]}" </dev/null >/dev/null
  for f in "$M"/*.log; do [ -f "$f" ] && wc -l <"$f" >"$f.before"; done
  local cid
  cid=$($CAS conversation start --agent-profile "$prof" --workspace "$WS" --no-autotitle \
    --prompt "QA $name" ${body:+--body-json "$body"} --secret "QA_PLUGIN_TOKEN=$PLUGIN_TOKEN" \
    --wait --until finished,error,stuck --timeout 240 --print-id </dev/null)
  local status
  status=$($CAS api GET "/api/conversations/$cid" --field execution_status </dev/null)
  {
    echo "## $name (profile $prof, conversation $cid, status $status)"
    "$PY" "$HERE/summarize_requests.py" "$RUN/fixtures/qa-stub.jsonl" "$CALLS"
    echo "MCP server starts during this scenario:"
    for f in "$M"/*.log; do
      [ -f "$f" ] || continue
      local before=0
      [ -f "$f.before" ] && before=$(cat "$f.before")
      echo "  $(basename "$f" .log): $(($(wc -l <"$f") - before))"
    done
    [ -f "$M/rogue_extra.log" ] || echo "  rogue_extra: 0"
    [ -f "$M/rogue_same.log" ] || echo "  rogue_same: 0"
    [ -f "$M/rogue_plugin.log" ] || echo "  rogue_plugin: 0"
  } | tee "$R/$name.txt"
  CALLS=$(wc -l <"$RUN/fixtures/qa-stub.jsonl")
}

FIN_CHILD='tool:finish:{"message":"CHILD_DONE"}'
FIN_PARENT='tool:finish:{"message":"PARENT_DONE"}'
wf() { printf 'tool:workflow:{"name":"qa","script":"async def main(wf):\\n    return await wf.run_agent(\\"call qa_echo\\", \\"%s\\")"}' "$1"; }

scenario S1-task-builtin qa-task "" \
  'tool:task:{"description":"qa","prompt":"call qa_echo","subagent_type":"general-purpose"}' \
  'tool:qa_echo:{"text":"S1"}' "$FIN_CHILD" "$FIN_PARENT"

scenario S2-task-file-agent-declaring-rogue-servers qa-task "" \
  'tool:task:{"description":"qa","prompt":"call qa_echo","subagent_type":"declarer"}' \
  'tool:qa_echo:{"text":"S2"}' "$FIN_CHILD" "$FIN_PARENT"

scenario S3-workflow_tool_set qa-wfset "" \
  "$(wf declarer)" 'tool:qa_echo:{"text":"S3"}' "$FIN_CHILD" "$FIN_PARENT"

scenario S4-workflow qa-wf "" \
  "$(wf declarer)" 'tool:qa_echo:{"text":"S4"}' "$FIN_CHILD" "$FIN_PARENT"

scenario S5-nested-grandchild qa-task "" \
  'tool:task:{"description":"qa","prompt":"delegate","subagent_type":"nester"}' \
  'tool:task:{"description":"qa","prompt":"call qa_echo","subagent_type":"leaf"}' \
  'tool:qa_echo:{"text":"S5"}' "$FIN_CHILD" "$FIN_CHILD" "$FIN_PARENT"

scenario S6-empty-parent-scope qa-empty "" \
  'tool:task:{"description":"qa","prompt":"look around","subagent_type":"declarer"}' \
  "$FIN_CHILD" "$FIN_PARENT"

scenario S7-resume qa-task "" \
  'tool:task:{"description":"qa","prompt":"first","subagent_type":"general-purpose"}' \
  "$FIN_CHILD" \
  'tool:task:{"description":"qa","prompt":"call qa_echo","subagent_type":"general-purpose","resume":"task_00000001"}' \
  'tool:qa_echo:{"text":"S7-resumed"}' "$FIN_CHILD" "$FIN_PARENT"

scenario S8-plugin-mcp-and-plugin-agent qa-task "{\"plugins\":[{\"source\":\"$PLUGIN\"}]}" \
  'tool:task:{"description":"qa","prompt":"call both","subagent_type":"plugged"}' \
  'tool:plugin-mcp_plugin_echo:{"text":"S8-plugin"}' \
  'tool:inherited-mcp_qa_echo:{"text":"S8-settings"}' "$FIN_CHILD" "$FIN_PARENT"

# --- Credential exposure ----------------------------------------------------
{
  echo "## Credential exposure scan"
  for name in TOKEN PLUGIN_TOKEN; do
    value=${!name}
    hits=$(grep -rlF -- "$value" "$RUN" 2>/dev/null | grep -v '/qa5743/results/' || true)
    echo "$name plaintext in run directory (server state, conversations, logs, stub requests): ${hits:-none}"
  done
  $CAS state grep "$TOKEN" --glob 'server/**/*' --expect-none </dev/null >/dev/null && echo "state grep settings token: none"
  $CAS state grep "$PLUGIN_TOKEN" --glob 'server/**/*' --expect-none </dev/null >/dev/null && echo "state grep plugin token: none"
} | tee "$R/S9-credentials.txt"

$CAS stop </dev/null >/dev/null
echo "results: $R"
