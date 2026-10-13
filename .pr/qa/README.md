# QA for #5743: sub-agents inherit exactly the parent's MCP tools

Every run uses a real Agent Server, a real stdio FastMCP server
(`qa_mcp_server.py`) and a scripted OpenAI-compatible provider. The provider
records each request, so we can see which tools each agent was offered. The
MCP discovery, delegation and tool-execution paths run unchanged. Each run
was done twice: once on unmodified `main` (`4011cb5`, the baseline) and once
on this branch.

- `run_qa.sh CHECKOUT LABEL` launches an isolated, authenticated local Agent
  Server from CHECKOUT with `control-agent-server`. Through the REST API it
  stores an MCP server with a secret env credential
  (`POST /api/settings/mcp/inherited-mcp`) and parent agent profiles that
  select it through `mcp_server_refs`. Then it drives scenarios S1–S8.
- `docker_qa.py IMAGE` starts IMAGE through the SDK's `DockerWorkspace` and
  drives a `RemoteConversation`. IMAGE is an agent-server image built with
  `docker/build.py --target source-minimal` from the checkout under test.
- `summarize_requests.py` reduces the recorded provider requests to one line
  per request. Each line shows who sent the request (`parent`, or a sub-agent
  named by its task prompt), the tools offered, the QA MCP tool results seen
  so far, and the last delegation result.

## How to read the evidence

- The MCP tool returns `token_sha=<first 12 hex chars of sha256(MCP_TOKEN)>`.
  The credential's arrival can be checked against the `expected token_sha`
  line without the token itself appearing anywhere.
- Each MCP server start appends a line to a marker file. `rogue_extra`,
  `rogue_same` and `rogue_plugin` are servers that only sub-agent definitions
  declare. A non-zero count means the child opened a connection the parent
  never had.
- The credential scan greps the whole run directory (server state,
  conversations, logs, provider requests) for both tokens in plaintext. For
  Docker it greps the container's `/workspace`, `/qa` and `/tmp` and its logs.

## Results

Files:
- `results/baseline-main-4011cb5.txt` and `results/fix.txt`: local Agent
  Server.
- `results/docker-baseline-main-4011cb5.txt` and `results/docker-fix.txt`:
  Docker.

| Scenario | Baseline (`main`) | This branch |
|---|---|---|
| S1 `task` → built-in `general-purpose` (exact repro from the issue) | Parent offers `qa_echo`, child does not, so the child's call gets no result | Child offers `qa_echo`, call succeeds, token_sha matches |
| S2 `task` → file agent declaring an extra server and a same-named `inherited-mcp` with another command | Rejected: "uses MCP server 'inherited-mcp', MCP server 'rogue', which this agent does not have" | Runs with only the parent's `qa_echo`, call succeeds, 0 rogue starts |
| S3 `workflow_tool_set` → same file agent | Rejected, as in S2 | Child offers `qa_echo`, call succeeds, 0 rogue starts |
| S4 `workflow` → same file agent | Rejected, as in S2 | Child offers `qa_echo`, call succeeds, 0 rogue starts |
| S5 nested: parent → `nester` (task) → `leaf` (no built-in tools); both declare rogue servers | `nester` rejected | Child and grandchild both offer `qa_echo`, grandchild's call succeeds, 0 rogue starts |
| S6 parent profile with `mcp_server_refs: []` → declaring file agent | Rejected, as in S2 | No MCP tools, 0 MCP server starts (parent or rogue) |
| S7 `task` resume of an existing sub-agent | Resumed child lacks `qa_echo` | Resumed child offers `qa_echo`, call succeeds |
| S8 conversation plugin adds `plugin-mcp` with `${QA_PLUGIN_TOKEN}`; plugin agent declares `rogue-plugin` | Plugin agent rejected | Child offers `inherited-mcp_qa_echo` and `plugin-mcp_plugin_echo`, both calls succeed with the right token_sha (plugin secret expanded once, by the parent), 0 rogue starts |
| Credential scan (local) | none | none |
| Docker: file agent declaring no servers | Child lacks `qa_echo` | Child offers `qa_echo`, call succeeds |
| Docker: file agent declaring an extra server and a same-named one (no profile scope) | Child gets `rogue_rogue_extra` and `inherited-mcp_rogue_same` (the parent's server is overridden); both rogue servers start | Child gets only the parent's `qa_echo`, call succeeds, 0 rogue starts |
| Credential scan (Docker container and logs) | none | none |

## Not exercised live

- **Cloud launch.** OpenHands Cloud runs released agent-server images, so this
  branch cannot run there before release. Delegation happens entirely inside
  the agent-server (`TaskManager` / child `LocalConversation`), and that is
  the same code exercised above through the local and Docker launches.
- **ACP sub-agents.** Covered by unit tests in
  `tests/tools/task/test_sub_agent_scope.py`. Scoped delegation still refuses
  an ACP sub-agent. Unscoped delegation hands the ACP agent exactly the
  parent's `mcp_config`. MCP servers that the external ACP CLI configures
  itself (for example in its own user config) are outside the SDK's control.
