# Multi-Agent Blog Writer

A small multi-agent system built with [Google's Agent Development Kit (ADK)](https://google.github.io/adk-docs/) that turns a topic prompt into a full technical blog post. 

## What it does

Give it a topic and it will:
1. Query **Google Trends** (local MCP server) for rising queries and pick the best angle.
2. *(Optional)* Query **BigQuery** (hosted MCP) for a data point that grounds the angle.
3. *(Optional)* Query **Google Maps** (hosted MCP) for real-world references when the topic mentions places.
4. Draft a Markdown outline (title, intro, 4–6 sections, conclusion) and validate it.
5. Write the full Markdown article from the outline and validate it.
6. Generate a **hero image** with a Gemini image model and prepend it to the post.
7. Persist the post + metadata (JSON + Markdown copy) to `./out/posts/YYYY-MM-DD/`.
8. Return the post plus 3 alternate titles and 2 tweet-length hooks.

Steps 2 and 3 activate only when their env vars are set. Steps 6 and 7 always run — with `GCS_BUCKET` set they upload to Cloud Storage; without it they write to `./out/` locally. The agent runs end-to-end with just `GOOGLE_API_KEY`.

## Architecture

```
root_agent (Blogger)
├── trends_mcp     → local MCP server (server.py) → pytrends → Google Trends
├── bigquery_mcp   → hosted MCP at bigquery.googleapis.com/mcp   (optional, ADC-auth)
├── maps_mcp       → hosted MCP at mapstools.googleapis.com/mcp  (optional, MAPS_API_KEY)
├── planner_tool   → RobustBlogPlanner (LoopAgent)
│                     ├── BlogPlanner              (produces outline)
│                     └── OutlineValidationChecker (says "ok" or "retry")
├── writer_tool    → RobustBlogWriter  (LoopAgent)
│                     ├── BlogWriter                 (produces article)
│                     └── BlogPostValidationChecker  (says "ok" or "retry")
├── generate_hero_image → Gemini image model → GCS signed URL if bucket set, else ./out/hero-images/
└── save_blog_post      → JSON + Markdown → GCS if bucket set, else ./out/posts/YYYY-MM-DD/
```

The root agent invokes the trends tool first, then any hosted MCPs it has creds for, then the planner and writer (exposed as `AgentTool`s), then illustrates and archives. Sub-agents write to shared state (`blog_outline`, `blog_post`) that downstream agents read. The local MCP server is spawned automatically by ADK — no separate process to launch.

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

### Optional: enable the hosted MCPs and Cloud Storage features

To turn on BigQuery, Maps, image generation, and persistence you need a Google Cloud project.

```bash
# 1. Set your project
gcloud config set project YOUR_PROJECT_ID

# 2. Enable the APIs
gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com \
  storage.googleapis.com

# 3. Application Default Credentials for BigQuery + GCS
gcloud auth application-default login --project YOUR_PROJECT_ID

# 4. Create a bucket for hero images + saved posts
gcloud storage buckets create gs://YOUR_BUCKET_NAME --location=US

# 5. Create a Maps API key in Cloud Console → APIs & Services → Credentials
```

Then set in `.env`:

```
MAPS_API_KEY=...
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GCS_BUCKET=YOUR_BUCKET_NAME
IMAGE_MODEL=gemini-2.5-flash-image-preview
```

Any of these left blank simply disables that feature — the agent still works.

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
├── agent.py           # agent + tool definitions (root_agent, MCP toolsets, image + save tools)
├── server.py          # local MCP server exposing the `trends` tool
├── __init__.py        # exposes `agent` to ADK
├── requirements.txt
├── .env.example       # template — copy to .env
└── .gitignore
```
