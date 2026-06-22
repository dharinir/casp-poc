import asyncio
from collections import abc
import contextlib
import json
import os
import re
import traceback
import urllib.request

from absl import app as absl_app
import fastapi
from fastapi import responses
from fastapi.middleware import cors
import google.auth
from googleapiclient import discovery
from googleapiclient import errors
import pydantic
import uvicorn

import agent_config
import looker_tool
from jetski_sdk.protos import trajectory_pb2
from jetski_sdk.protos import cortex_pb2
from jetski_sdk.protos import language_server_pb2
from jetski_sdk import agent
from jetski_sdk import client
from jetski_sdk import conversation
from jetski_sdk import types
from jetski_sdk.utils import path_utils


class SessionState:
  conv: conversation.Conversation
  queue: asyncio.Queue
  active_future: asyncio.Future | None
  task: asyncio.Task | None

  def __init__(self, conv: conversation.Conversation):
    self.conv = conv
    self.queue = asyncio.Queue()
    self.active_future = None
    self.task = None


class ResolveResponse(pydantic.BaseModel):
  selected_option_ids: list[str] = []
  write_in_response: str = ""
  skipped: bool = False


class InteractionResolveRequest(pydantic.BaseModel):
  session_id: str
  responses: list[ResolveResponse]


class QueryRequest(pydantic.BaseModel):
  query: str
  context: str = ""
  session_id: str = "default"


app_state = {}
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))


@contextlib.asynccontextmanager
async def lifespan(_app: fastapi.FastAPI):
  # Setup Looker SDK config path
  os.environ["LOOKERSDK_INI"] = os.path.join(CURRENT_DIR, "looker.ini")

  # Setup the environment config for Antigravity SDK
  port = os.environ.get("JETSKI_PORT")
  csrf_token = os.environ.get("JETSKI_TOKEN", "default-token")

  if port:
    ls_config = types.LanguageServerConnectionConfig(
        local_address=f"localhost:{port}",
        env_address=f"localhost:{port}",
        csrf_token=csrf_token,
    )
  else:
    # If not running inside Jetski IDE/CLI, spin up a local Language Server
    is_g3 = path_utils.is_google3_workspace(CURRENT_DIR)
    ls_config = types.LanguageServerCreationConfig(
        is_google3_workspace=is_g3,
        use_stubby_auth=True,
    )

  env_config = types.EnvironmentConfig(
      language_server_config=ls_config,
  )

  # Initialize the SDK Client and Agent
  async with client.AntigravitySDKClient(env_config) as sdk_client:
    app_state["sdk_client"] = sdk_client
    app_state["agent_obj"] = await sdk_client.agent(
        agent_config.get_agent_config()
    )
    app_state["sessions"] = {}
    yield
    # Teardown


app = fastapi.FastAPI(lifespan=lifespan)

# Allow CORS for development
app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DRIVE_URL_RE = re.compile(
    r"https?://(?:docs|drive)\.google\.com/(?:document|file|spreadsheets)/d/([a-zA-Z0-9-_]+)"
)
URL_RE = re.compile(
    r"https?://[^\s\>]+"
)  # Exclude trailing > if in markdown links


def extract_drive_file_id(url: str) -> str | None:
  match = DRIVE_URL_RE.search(url)
  return match.group(1) if match else None


def fetch_drive_file_content(file_id: str) -> str:
  try:
    print(f"fetch_drive_file_content: starting for {file_id}", flush=True)
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    quota_project = "dharinir-lags-codelab"
    credentials = credentials.with_quota_project(quota_project)
    drive_service = discovery.build("drive", "v3", credentials=credentials)

    file_metadata = (
        drive_service.files()
        .get(fileId=file_id, fields="mimeType,name")
        .execute()
    )
    mime_type = file_metadata.get("mimeType", "")
    name = file_metadata.get("name", "Document")
    print(
        f"fetch_drive_file_content: file name='{name}', mime='{mime_type}'",
        flush=True,
    )

    if "document" in mime_type:
      content = (
          drive_service.files()
          .export(fileId=file_id, mimeType="text/plain")
          .execute()
      )
      print("fetch_drive_file_content: exported doc successfully", flush=True)
      return (
          f"\n--- Content of Google Doc '{name}'"
          f" ---\n{content.decode('utf-8')}\n--- End of Doc ---\n"
      )
    elif "spreadsheet" in mime_type:
      content = (
          drive_service.files()
          .export(fileId=file_id, mimeType="text/csv")
          .execute()
      )
      print("fetch_drive_file_content: exported sheet successfully", flush=True)
      return (
          f"\n--- Content of Google Sheet '{name}' (CSV)"
          f" ---\n{content.decode('utf-8')}\n--- End of Sheet ---\n"
      )
    else:
      content = drive_service.files().get_media(fileId=file_id).execute()
      print(
          "fetch_drive_file_content: downloaded file successfully", flush=True
      )
      return (
          f"\n--- Content of File '{name}'"
          f" ---\n{content.decode('utf-8', errors='ignore')}\n--- End of File"
          " ---\n"
      )
  except errors.HttpError as e:
    print(
        f"HTTP Error fetching Drive file {file_id}: {traceback.format_exc()}",
        flush=True,
    )
    if e.resp.status == 403:
      return (
          f"\n[Error fetching Drive File {file_id}: Insufficient Permission"
          " (403). Please run 'gcloud auth application-default login"
          " --scopes=https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/cloud-platform'"
          " in your terminal and restart the server.]\n"
      )
    return f"\n[Error fetching Drive File {file_id}: {str(e)}]\n"
  except Exception as e:
    print(
        f"Error fetching Drive file {file_id}: {traceback.format_exc()}",
        flush=True,
    )
    return f"\n[Error fetching Drive File {file_id}: {str(e)}]\n"


def fetch_web_url_content(url: str) -> str:
  if "docs.google.com" in url or "drive.google.com" in url:
    return ""
  try:
    print(f"fetch_web_url_content: starting for {url}", flush=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=10) as response:
      html = response.read().decode("utf-8", errors="ignore")
      html = re.sub(
          r"<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>",
          "",
          html,
          flags=re.IGNORECASE,
      )
      html = re.sub(
          r"<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>",
          "",
          html,
          flags=re.IGNORECASE,
      )
      text = re.sub(r"<[^>]+>", " ", html)
      text = re.sub(r"\s+", " ", text).strip()
      print(
          f"fetch_web_url_content: fetched successfully, length={len(text)}",
          flush=True,
      )
      return (
          f"\n--- Content of Web URL '{url}' ---\n{text}\n--- End of Web"
          " Content ---\n"
      )
  except Exception as e:
    print(f"Error fetching Web URL {url}: {traceback.format_exc()}", flush=True)
    return f"\n[Error fetching Web URL '{url}': {str(e)}]\n"


def resolve_context_urls(context: str) -> str:
  print(f"resolve_context_urls: input context: {context}", flush=True)
  if not context:
    return ""
  resolved = context
  urls = URL_RE.findall(context)
  print(f"resolve_context_urls: found URLs: {urls}", flush=True)
  for url in urls:
    drive_id = extract_drive_file_id(url)
    if drive_id:
      print(f"resolve_context_urls: resolving Drive URL {url}", flush=True)
      content = fetch_drive_file_content(drive_id)
      resolved = resolved.replace(url, content)
    else:
      print(f"resolve_context_urls: resolving Web URL {url}", flush=True)
      content = fetch_web_url_content(url)
      if content:
        resolved = resolved.replace(url, content)
  print(
      f"resolve_context_urls: resolved context preview: {resolved[:500]}...",
      flush=True,
  )
  return resolved


async def init_agent_session(query: str, context: str, session_id: str) -> None:
  """Initializes the agent session and starts the execution in the background."""
  agent_obj: agent.Agent = app_state["agent_obj"]
  sessions = app_state["sessions"]

  if session_id not in sessions:
    workspace_root = path_utils.get_workspace_root()

    # Define the handler inside to capture session_id closure
    async def make_handler(sid: str):
      async def handler(
          requested_interaction: cortex_pb2.RequestedInteraction,
          _step: trajectory_pb2.Step,
      ) -> cortex_pb2.CascadeUserInteraction:
        interaction_type = requested_interaction.WhichOneof("interaction")
        print(
            f"[{sid}] Received interaction request of type: {interaction_type}"
        )

        response = cortex_pb2.CascadeUserInteraction()
        if interaction_type == "permission":
          print(
              f"[{sid}] Permission request: {requested_interaction.permission}",
              flush=True,
          )
          response.permission.allow = True
          return response
        elif interaction_type == "file_permission":
          response.file_permission.allow = True
          return response

        if interaction_type == "ask_question":
          state = app_state["sessions"].get(sid)
          if not state:
            return response

          # Create future to wait for user response
          loop = asyncio.get_running_loop()
          future = loop.create_future()
          state.active_future = future

          # Extract questions to send to UI
          questions = []
          for q in requested_interaction.ask_question.questions:
            opts = [{"id": o.id, "text": o.text} for o in q.options]
            questions.append({
                "question": q.question,
                "is_multi_select": q.is_multi_select,
                "options": opts,
            })

          # Push interaction event to queue
          await state.queue.put({
              "type": "interaction_required",
              "interaction_type": "ask_question",
              "questions": questions,
          })

          # Wait until endpoint resolves the future
          try:
            payload = await future
          except asyncio.CancelledError:
            print(f"[{sid}] Interaction cancelled")
            response.ask_question.cancelled = True
            return response
          finally:
            state.active_future = None

          # Populate user responses
          for r in payload.get("responses", []):
            entry = response.ask_question.responses.add()
            if "selected_option_ids" in r:
              entry.selected_option_ids.extend(r["selected_option_ids"])
            if "write_in_response" in r:
              entry.write_in_response = r["write_in_response"]
            if "skipped" in r:
              entry.skipped = r["skipped"]

          return response

        return response

      return handler

    h = await make_handler(session_id)
    conv = await agent_obj.conversation(
        workspaces=[workspace_root],
        handle_interaction=h,
    )
    sessions[session_id] = SessionState(conv)

  state: SessionState = sessions[session_id]

  # Cancel any previous running query for this session
  if state.task and not state.task.done():
    state.task.cancel()
    if state.active_future and not state.active_future.done():
      state.active_future.cancel()
    # Clear queue
    while not state.queue.empty():
      try:
        state.queue.get_nowait()
      except asyncio.QueueEmpty:
        break

  # Background agent loop runner
  async def run_agent():
    try:
      # Resolve URLs in a background thread
      loop = asyncio.get_running_loop()
      resolved_context = await loop.run_in_executor(
          None, resolve_context_urls, context
      )

      full_query = query
      if resolved_context:
        full_query += f"\n\nAdditional Context:\n{resolved_context}"
      await state.conv.send(full_query)
      async for step in state.conv.receive_steps():
        if step is None:
          continue
        if step.source and step.source.startswith("user"):
          continue
        await state.queue.put({"type": "step", "step": step})
      await state.queue.put({"type": "done"})
    except Exception as e:
      traceback.print_exc()
      await state.queue.put({"type": "error", "error": str(e)})

  state.task = asyncio.create_task(run_agent())


async def stream_agent_events(
    session_id: str,
) -> abc.AsyncGenerator[str, None]:
  """Streams agent steps and interactions as SSE from the session queue."""
  sessions = app_state["sessions"]
  if session_id not in sessions:
    yield (
        "data:"
        f" {json.dumps({'status': 'error', 'error': 'Session not found'})}\n\n"
    )
    return

  state: SessionState = sessions[session_id]
  step_idx = 0
  while True:
    try:
      event = await state.queue.get()
    except asyncio.CancelledError:
      if state.task and not state.task.done():
        state.task.cancel()
      if state.active_future and not state.active_future.done():
        state.active_future.cancel()
      break

    if event["type"] == "done":
      yield f"data: {json.dumps({'status': 'done'})}\n\n"
      break
    elif event["type"] == "error":
      yield (
          "data:"
          f" {json.dumps({'status': 'error', 'error': event['error']})}\n\n"
      )
      break
    elif event["type"] == "interaction_required":
      data = {
          "type": "interaction_required",
          "interaction_type": event["interaction_type"],
          "questions": event["questions"],
      }
      yield f"data: {json.dumps(data)}\n\n"
    elif event["type"] == "step":
      step = event["step"]
      if step.thinking:
        data = {"step": step_idx, "type": "thought", "text": step.thinking}
        yield f"data: {json.dumps(data)}\n\n"
        step_idx += 1
      if step.tool_call:
        data = {
            "step": step_idx,
            "type": "tool_call",
            "name": step.tool_call.name,
            "args": str(step.tool_call.args),
        }
        yield f"data: {json.dumps(data)}\n\n"
        step_idx += 1
      if step.content and step.type == "planner_response":
        data = {
            "step": step_idx,
            "type": "final_output",
            "text": step.content,
        }
        yield f"data: {json.dumps(data)}\n\n"
        step_idx += 1


@app.get("/", response_class=responses.HTMLResponse)
async def root_endpoint() -> responses.HTMLResponse:
  """Serves the frontend HTML page."""
  html_path = os.path.join(CURRENT_DIR, "index.html")
  try:
    with open(html_path, "r") as f:
      content = f.read()
    return responses.HTMLResponse(content=content)
  except FileNotFoundError:
    return responses.HTMLResponse(
        content="<h1>index.html not found</h1>", status_code=404
    )


@app.get("/api/dashboard/{dashboard_id}")
async def get_dashboard_endpoint(dashboard_id: str):
  """Endpoint to get dashboard metadata and tile data."""
  try:
    dashboard_data_str = looker_tool.looker_get_dashboard(dashboard_id)
    dashboard_data = json.loads(dashboard_data_str)
    if "error" in dashboard_data:
      print(f"Dashboard error: {dashboard_data['error']}")
      raise fastapi.HTTPException(
          status_code=500, detail=dashboard_data["error"]
      )
    return dashboard_data
  except Exception as e:
    traceback.print_exc()
    raise fastapi.HTTPException(status_code=500, detail=str(e))


@app.post("/api/query/init")
async def query_init_endpoint(req: QueryRequest):
  """Initializes the query session."""
  try:
    await init_agent_session(req.query, req.context, req.session_id)
    return {"status": "initialized", "session_id": req.session_id}
  except Exception as e:
    traceback.print_exc()
    raise fastapi.HTTPException(status_code=500, detail=str(e))


@app.get("/api/query/stream")
async def query_stream_endpoint(
    session_id: str = "default",
) -> responses.StreamingResponse:
  """Streams the agent execution steps."""
  headers = {
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
  }
  return responses.StreamingResponse(
      stream_agent_events(session_id),
      media_type="text/event-stream",
      headers=headers,
  )


@app.get("/api/mcp/states")
async def get_mcp_states():
  """Returns the current state of all MCP servers."""
  sdk_client = app_state.get("sdk_client")
  # pylint: disable=protected-access
  if not sdk_client or not sdk_client._stub:
    raise fastapi.HTTPException(
        status_code=500,
        detail="SDK client not initialized or connection unavailable.",
    )

  req = language_server_pb2.GetMcpServerStatesRequest()
  try:
    resp: language_server_pb2.GetMcpServerStatesResponse = (
        await sdk_client._stub.GetMcpServerStates(req)
    )
    # pylint: enable=protected-access
    states = []
    for s in resp.states:
      states.append({
          "server_name": s.spec.server_name,
          "server_url": s.spec.server_url,
          "status": s.status,
          "error": s.error,
          "has_auth_token": s.has_auth_token,
          "auth_url": s.auth_url,
      })
    return {"states": states, "is_loading": resp.is_loading}
  except Exception as e:
    traceback.print_exc()
    raise fastapi.HTTPException(
        status_code=500, detail=f"gRPC call failed: {e}"
    )


class CompleteOAuthRequest(pydantic.BaseModel):
  server_name: str
  code: str


@app.post("/api/mcp/complete_oauth")
async def complete_mcp_oauth(req: CompleteOAuthRequest):
  """Completes the OAuth flow for an MCP server."""
  sdk_client = app_state.get("sdk_client")
  # pylint: disable=protected-access
  if not sdk_client or not sdk_client._stub:
    raise fastapi.HTTPException(
        status_code=500,
        detail="SDK client not initialized or connection unavailable.",
    )

  g_req = language_server_pb2.CompleteMcpOAuthRequest(
      server_name=req.server_name,
      authorization_code=req.code,
  )
  try:
    await sdk_client._stub.CompleteMcpOAuth(g_req)
    # pylint: enable=protected-access
    return {"status": "success"}
  except Exception as e:
    traceback.print_exc()
    raise fastapi.HTTPException(
        status_code=500, detail=f"gRPC call failed: {e}"
    )


@app.post("/api/interaction/resolve")
async def resolve_interaction_endpoint(req: InteractionResolveRequest):
  """Endpoint to resolve a pending interaction for a session."""
  sessions = app_state.get("sessions", {})
  state: SessionState = sessions.get(req.session_id)
  if not state or not state.active_future or state.active_future.done():
    raise fastapi.HTTPException(
        status_code=400, detail="No active interaction found for this session."
    )

  formatted_responses = []
  for r in req.responses:
    formatted_responses.append({
        "selected_option_ids": r.selected_option_ids,
        "write_in_response": r.write_in_response,
        "skipped": r.skipped,
    })

  state.active_future.set_result({"responses": formatted_responses})
  return {"status": "success"}


def main(argv: list[str]) -> None:
  del argv  # Unused
  port = int(os.environ.get("PORT", 8000))
  uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
  absl_app.run(main)
