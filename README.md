# Multi-Agent Blog Writer

A small multi-agent system built with [Google's Agent Development Kit (ADK)](https://google.github.io/adk-docs/) that turns a topic prompt into a full technical blog post. 

## What it does

Give it a topic and it will:
1. Query **Google Trends** (via a local MCP server) for related rising queries and pick the best angle.
2. Draft a Markdown outline (title, intro, 4–6 sections, conclusion).
3. Validate the outline; retry up to 3 times if it's missing pieces.
4. Write a full Markdown article from the outline.
5. Validate the article; retry up to 3 times if it falls short.
6. Return the post plus 3 alternate titles and 2 tweet-length hooks.

## Architecture

```
root_agent (Blogger)
├── trends_mcp    → MCP server (server.py) exposing the `trends` tool
│                     └── pytrends → Google Trends (with fallbacks)
├── planner_tool  → RobustBlogPlanner (LoopAgent, max 3 tries)
│                     ├── BlogPlanner              (produces outline)
│                     └── OutlineValidationChecker (says "ok" or "retry")
└── writer_tool   → RobustBlogWriter  (LoopAgent, max 3 tries)
                      ├── BlogWriter                 (produces article)
                      └── BlogPostValidationChecker  (says "ok" or "retry")
```

The root agent invokes the trends tool once (via MCP over stdio), then hands off to the planner and writer, which are exposed as `AgentTool`s. Each sub-agent writes its output to shared state (`blog_outline`, `blog_post`) that downstream agents read. The MCP server is spawned automatically by ADK when the tool is called — no separate process to launch.

## Setup

Requires Python 3.10+ (tested with 3.12).

```bash
git clone https://github.com/crick262004/multi-agent-blog-writer.git
cd multi-agent-blog-writer

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and paste in your GOOGLE_API_KEY
```

Get a free API key at [Google AI Studio](https://aistudio.google.com/apikey).

## Running it

ADK discovers agents by importing the containing package, so run `adk web` from the **parent** directory:

```bash
cd ..                    # step out of the project folder
adk web                  # opens a local web UI
```

Pick `multi-agent-blog-writer` (or whatever you named the folder) from the agent dropdown and start chatting. Try something like:

> Write a blog post about how Python's GIL affects async performance.

Alternative: `adk run multi-agent-blog-writer` for a terminal-only session.

## Project layout

```
.
├── agent.py           # all agent definitions + root_agent
├── server.py          # MCP server exposing the `trends` tool
├── __init__.py        # exposes `agent` to ADK
├── requirements.txt
├── .env.example       # template — copy to .env
└── .gitignore
```
