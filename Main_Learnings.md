# Main Learnings — Building a Multi-Agent Blogger with Google ADK

Notes on how the pieces fit together: how tools are defined, how the model actually calls them, and how sub-agents share state.

## 1. The three tool flavors

When ADK hands Gemini the tool list, every entry looks the same to the model: a **name**, a **JSON schema of arguments**, and a **description**. What differs is what happens on your side when the model picks one.

The root agent's tool list is a mix of three flavors:

### `FunctionTool` — the plain one

Write a Python function, wrap it with `FunctionTool(fn)`. The function's docstring becomes the description; type hints become the argument schema. Model calls it → ADK just runs the function → return value goes back to the model. That's it.

### `AgentTool` — wraps another agent so it looks like a tool

```python
planner_tool = agent_tool.AgentTool(agent=robust_blog_planner)
```

When the model calls `planner`, ADK actually runs a whole sub-agent — its own LLM calls, its own tools, its own instruction. The sub-agent's final text becomes the "return value" the outer model sees. This is how you compose agents: one agent invokes another as if it were a function.

### `MCPToolset` — an external tool server

The model doesn't see one tool, it sees **however many tools that MCP server exposes**. ADK talks to the MCP server (via stdio or HTTP), asks it "what tools do you have?", and adds them to the model's list. When the model calls one, ADK forwards the call to the MCP server, which runs it.

A trends MCP might expose `trends`; a BigQuery MCP would expose whatever tools that server defines (`query`, `list-datasets`, etc.).

### The key insight

The model doesn't know or care which flavor. It just sees "here are tools." Your job as agent author is picking the right wrapper for what the tool actually is:

- Local Python function → `FunctionTool`
- Another agent → `AgentTool`
- External server → `MCPToolset`

## 2. How the model actually calls a tool

### Model ↔ ADK uses Gemini's native function-calling API

When ADK sends a request to Gemini, it includes a `tools` field in the JSON body listing every tool's name, description, and JSON schema. Gemini responds with either:

- **plain text** (it's done, or talking to you), or
- **a `functionCall` block** like `{"name": "generate_hero_image", "args": {"prompt": "…"}}`

ADK sees the `functionCall`, looks up the name in its tool registry, runs it, and sends the return value back to Gemini as a `functionResponse` in the next request. Loop.

This mechanism is the same regardless of tool flavor. The model has no idea whether `generate_hero_image` is a Python function, another agent, or a remote MCP tool — it just knows the name.

### Where MCP actually enters the picture

MCP is a protocol for ADK to talk to **external tool servers** — a transport, not a model-facing thing. It only matters *after* the model has picked a tool and ADK has decided "oh, this tool lives in an MCP server, I need to forward the call there."

Two transport modes:

- **stdio** — ADK launches the server as a subprocess and speaks JSON-RPC over stdin/stdout.
- **HTTP** — ADK POSTs JSON-RPC to a URL over the network.

### Two totally separate protocols

- **Model ↔ ADK**: Gemini function-calling API (HTTP to Google's Gemini endpoint).
- **ADK ↔ MCP server**: MCP protocol (stdio or HTTP to your server).

MCP exists so you can ship tool implementations as **standalone processes** that any MCP-aware client (Claude Desktop, Cursor, other agents) can plug into — not just one specific ADK agent.

## 3. LoopAgent + `output_key` — state sharing between sub-agents

A `LoopAgent` wraps a list of sub-agents. It runs them in order, then repeats up to `max_iterations` times if the last one signals "retry."

The mechanism is **shared state**. Each sub-agent has an `output_key`:

- `BlogPlanner(output_key="blog_outline")` → its final text is written to `state["blog_outline"]`.
- `OutlineValidationChecker` reads `blog_outline` from state (via its instruction: *"Check the outline in state `blog_outline`"*), then writes `"ok"` or `"retry"` to `state["validation_result"]`.

LoopAgent loops until `"ok"` or `max_iterations` is hit. When the outer `AgentTool` wrapper returns to the root agent, the root sees the last output. State also persists across the whole turn, so a later `BlogWriter` reads the same `blog_outline` that the planner wrote.

That's how you chain agents without explicitly passing arguments — **they share a dict**.
