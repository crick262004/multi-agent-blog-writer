import os
import sys
import time
import json
import uuid
import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.adk.agents import Agent, LoopAgent
from google.adk.tools import agent_tool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from mcp import StdioServerParameters

# env config
load_dotenv()

MODEL = os.getenv("MODEL", "gemini-flash-latest")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-2.5-flash-image-preview")

# Pace LLM calls to stay under free-tier RPM. 13s ≈ 4.6 calls/min, safely under the 5 RPM cap.
PACE_SECS = float(os.getenv("PACE_SECS", "13"))

def _pace_before_model(callback_context, llm_request):
   time.sleep(PACE_SECS)
   return None

def _log(msg: str):
   print(f"[agent] {msg}", file=sys.stderr)

# Sub-Agent: Planner
blog_planner = Agent(
   name="BlogPlanner",
   model=MODEL,
   description="Creates a practical, skimmable outline in Markdown.",
   instruction="""
You are a technical content strategist. Produce a clear Markdown outline with:
- Title
- Short intro
- 4–6 main sections (each with 2–3 bullets)
- Conclusion

If `codebase_context` exists in state, weave in specific sections/snippets.
Return only the outline in Markdown.
""",
   output_key="blog_outline",
   before_model_callback=_pace_before_model,
)

class OutlineValidationChecker(Agent):
   def __init__(self):
       super().__init__(
           name="OutlineValidationChecker",
           model=MODEL,
           description="Validates that the outline is usable.",
           instruction="""
Check the outline in state `blog_outline`. If it has a title, intro, 4–6 sections, and a conclusion, respond exactly "ok".
Otherwise respond exactly "retry" and list missing pieces.
""",
           output_key="validation_result",
           before_model_callback=_pace_before_model,
       )

robust_blog_planner = LoopAgent(
   name="RobustBlogPlanner",
   description="Retries planning if validation fails.",
   sub_agents=[blog_planner, OutlineValidationChecker()],
   max_iterations=1,
)

# Sub-Agent: Writer
blog_writer = Agent(
   name="BlogWriter",
   model=MODEL,
   description="Writes a technical blog post from the outline.",
   instruction="""
Write a complete Markdown article from the outline in `blog_outline`.

Guidelines:
- Audience: software engineers; skip basics and focus on practical insight.
- Explain both the 'how' and 'why'.
- Include concise code snippets when helpful.
- Follow the outline's structure (H2/H3).
- Output only the final article in Markdown (no fence around the whole post).
""",
   output_key="blog_post",
   before_model_callback=_pace_before_model,
)

class BlogPostValidationChecker(Agent):
   def __init__(self):
       super().__init__(
           name="BlogPostValidationChecker",
           model=MODEL,
           description="Validates the final post.",
           instruction="""
Check `blog_post` for: intro, clear sections matching the outline, conclusion, and technical clarity.
If passes, respond "ok". Else respond "retry" with the specific fixes.
""",
           output_key="validation_result",
           before_model_callback=_pace_before_model,
       )

robust_blog_writer = LoopAgent(
   name="RobustBlogWriter",
   description="Retries writing if validation fails.",
   sub_agents=[blog_writer, BlogPostValidationChecker()],
   max_iterations=1,
)

# Expose planner/writer as tools so the root agent can call them explicitly
planner_tool = agent_tool.AgentTool(agent=robust_blog_planner)
writer_tool  = agent_tool.AgentTool(agent=robust_blog_writer)

# ------------------------------------------------------------
# Local MCP Toolset: Google Trends (via server.py)
# ------------------------------------------------------------
trends_mcp_server = StdioServerParameters(
   command="python3",
   args=[str(Path(__file__).parent / "server.py")],
)
trends_mcp = MCPToolset(
   connection_params=StdioConnectionParams(server_params=trends_mcp_server)
)

# ------------------------------------------------------------
# Hosted MCP Toolset: Google Maps (optional)
# ------------------------------------------------------------
MAPS_MCP_URL = "https://mapstools.googleapis.com/mcp"

def _maybe_maps_mcp() -> Optional[MCPToolset]:
   key = os.getenv("MAPS_API_KEY", "").strip()
   if not key:
       _log("MAPS_API_KEY not set → skipping Maps MCP")
       return None
   _log("Maps MCP enabled")
   return MCPToolset(
       connection_params=StreamableHTTPConnectionParams(
           url=MAPS_MCP_URL,
           headers={"X-Goog-Api-Key": key},
           timeout=30.0,
           sse_read_timeout=300.0,
       )
   )

# ------------------------------------------------------------
# Hosted MCP Toolset: BigQuery (optional)
# ------------------------------------------------------------
BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"

def _maybe_bigquery_mcp() -> Optional[MCPToolset]:
   if not os.getenv("GOOGLE_CLOUD_PROJECT"):
       _log("GOOGLE_CLOUD_PROJECT not set → skipping BigQuery MCP")
       return None
   try:
       import google.auth
       import google.auth.transport.requests
       creds, project_id = google.auth.default(
           scopes=["https://www.googleapis.com/auth/bigquery"]
       )
       creds.refresh(google.auth.transport.requests.Request())
       token = creds.token
   except Exception as e:
       _log(f"BigQuery ADC lookup failed ({e}) → skipping BigQuery MCP")
       return None
   _log(f"BigQuery MCP enabled (project={project_id})")
   return MCPToolset(
       connection_params=StreamableHTTPConnectionParams(
           url=BIGQUERY_MCP_URL,
           headers={
               "Authorization": f"Bearer {token}",
               "x-goog-user-project": project_id,
           },
           timeout=30.0,
           sse_read_timeout=300.0,
       )
   )

# ------------------------------------------------------------
# Local tool: Gemini image generation → upload to GCS → signed URL
# ------------------------------------------------------------
LOCAL_OUT_DIR = Path(os.getenv("LOCAL_OUT_DIR", str(Path(__file__).parent / "out")))

def generate_hero_image(prompt: str) -> dict:
   """
   Generate a hero image for a blog post using Gemini image generation.
   If GCS_BUCKET is set → upload and return a signed URL.
   Otherwise → save locally under out/hero-images/ and return a file:// URL.

   Args:
     prompt: A short description of the desired image (e.g. blog title + style).

   Returns:
     {"status": "ok", "url": "..."} or {"status": "error", "message": "..."}
   """
   try:
       from google import genai
   except Exception as e:
       return {"status": "error", "message": f"genai dep missing: {e}"}

   try:
       client = genai.Client()
       resp = client.models.generate_content(
           model=IMAGE_MODEL,
           contents=[prompt],
       )
       fname = f"hero_{uuid.uuid4().hex}.png"
       tmp_path = f"/tmp/{fname}"
       saved = False
       for part in getattr(resp, "parts", []) or []:
           if getattr(part, "inline_data", None) is not None:
               part.as_image().save(tmp_path)
               saved = True
               break
       if not saved:
           return {"status": "error", "message": "model returned no image part"}

       bucket_name = os.getenv("GCS_BUCKET", "").strip()
       if bucket_name:
           try:
               from google.cloud import storage
               storage_client = storage.Client()
               bucket = storage_client.bucket(bucket_name)
               blob = bucket.blob(f"hero-images/{fname}")
               blob.upload_from_filename(tmp_path)
               url = blob.generate_signed_url(
                   version="v4",
                   expiration=datetime.timedelta(hours=24),
                   method="GET",
               )
               return {"status": "ok", "url": url, "storage": "gcs"}
           except Exception as e:
               _log(f"GCS upload failed, falling back to local: {e}")

       local_dir = LOCAL_OUT_DIR / "hero-images"
       local_dir.mkdir(parents=True, exist_ok=True)
       local_path = local_dir / fname
       Path(tmp_path).replace(local_path)
       return {"status": "ok", "url": local_path.resolve().as_uri(), "storage": "local"}
   except Exception as e:
       _log(f"generate_hero_image failed: {e}")
       return {"status": "error", "message": str(e)}

# ------------------------------------------------------------
# Local tool: persist blog post + metadata to GCS
# ------------------------------------------------------------
def save_blog_post(topic: str, blog_post: str, image_url: str = "") -> dict:
   """
   Save the final blog post and metadata.
   If GCS_BUCKET is set → upload JSON to gs://{bucket}/posts/{YYYY-MM-DD}/{slug}.json.
   Otherwise → write to ./out/posts/{YYYY-MM-DD}/{slug}.json AND
               a plain Markdown copy at ./out/posts/{YYYY-MM-DD}/{slug}.md.

   Args:
     topic: The topic the user requested.
     blog_post: The final Markdown article.
     image_url: Optional URL for the hero image.

   Returns:
     {"status": "ok", "location": "..."} or {"status": "error", "message": "..."}
   """
   try:
       now = datetime.datetime.now(datetime.timezone.utc)
       today = now.strftime("%Y-%m-%d")
       slug = "".join(c.lower() if c.isalnum() else "-" for c in topic).strip("-")[:60] or "post"
       fname = f"{slug}-{uuid.uuid4().hex[:6]}"
       key = f"posts/{today}/{fname}.json"
       payload = {
           "topic": topic,
           "created_utc": now.isoformat(),
           "image_url": image_url,
           "blog_post": blog_post,
       }

       bucket_name = os.getenv("GCS_BUCKET", "").strip()
       if bucket_name:
           try:
               from google.cloud import storage
               storage_client = storage.Client()
               bucket = storage_client.bucket(bucket_name)
               blob = bucket.blob(key)
               blob.upload_from_string(json.dumps(payload, indent=2), content_type="application/json")
               return {"status": "ok", "location": f"gs://{bucket_name}/{key}", "storage": "gcs"}
           except Exception as e:
               _log(f"GCS upload failed, falling back to local: {e}")

       local_dir = LOCAL_OUT_DIR / "posts" / today
       local_dir.mkdir(parents=True, exist_ok=True)
       json_path = local_dir / f"{fname}.json"
       md_path = local_dir / f"{fname}.md"
       json_path.write_text(json.dumps(payload, indent=2))
       md_prefix = f"![hero]({image_url})\n\n" if image_url else ""
       md_path.write_text(md_prefix + blog_post)
       return {"status": "ok", "location": str(md_path.resolve()), "storage": "local"}
   except Exception as e:
       _log(f"save_blog_post failed: {e}")
       return {"status": "error", "message": str(e)}

hero_image_tool = FunctionTool(generate_hero_image)
save_post_tool  = FunctionTool(save_blog_post)

# ------------------------------------------------------------
# Assemble tool list (skip anything that isn't configured)
# ------------------------------------------------------------
_tools = [trends_mcp, planner_tool, writer_tool, hero_image_tool, save_post_tool]
_maps = _maybe_maps_mcp()
if _maps: _tools.insert(1, _maps)
_bq = _maybe_bigquery_mcp()
if _bq: _tools.insert(1, _bq)

# Root Agent: Trends → (BigQuery) → Plan → Write → Image → Save
root_agent = Agent(
   name="Blogger",
   model=MODEL,
   description="Multi-agent blogger that grounds with MCPs, writes, illustrates, and archives.",
   instruction=f"""
When the user gives a topic:
1) Call the MCP tool `trends` (geo="US", timeframe="now 7-d", quick=true).
   Summarize rising queries in 3–5 bullets and pick one angle.
2) If a `bigquery` tool is available, run ONE small query that adds a data point
   relevant to the angle (e.g. GitHub Archive event counts, StackOverflow tag trends).
   Skip silently if the tool is unavailable or the query errors.
3) If a `maps` tool is available and the topic mentions real-world places,
   look up 1–2 supporting references. Skip silently if unavailable.
4) Call the planner tool to generate the outline.
5) Call the writer tool to produce the full draft.
6) Call `generate_hero_image` with a short visual prompt derived from the title.
   If it returns status="ok", prepend `![hero](URL)` to the article. On error, continue.
7) Call `save_blog_post` with the topic, final article, and image URL (or "").
   Report the returned `location` to the user (gs:// URI or local file path).
8) End with 3 alternate titles and 2 tweet-length hooks.

If any tool fails or times out, continue with what you have.

Date: {datetime.datetime.now().strftime("%Y-%m-%d")}
""",
   tools=_tools,
   before_model_callback=_pace_before_model,
)
