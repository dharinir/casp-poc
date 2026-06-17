import asyncio
from collections import abc
import contextlib
import json
import os

import fastapi
from fastapi import responses
from fastapi.middleware import cors
import pydantic
import uvicorn

import agent_config
import looker_tool
from jetski_sdk.protos import trajectory_pb2
from jetski_sdk.protos import cortex_pb2
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


async def stream_agent_execution(
    query: str, session_id: str
) -> abc.AsyncGenerator[str, None]:
  """Streams agent steps and interactions as SSE."""
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
      await state.conv.send(query)
      async for step in state.conv.receive_steps():
        if step is None:
          continue
        if step.source and step.source.startswith("user"):
          continue
        await state.queue.put({"type": "step", "step": step})
      await state.queue.put({"type": "done"})
    except Exception as e:
      import traceback

      traceback.print_exc()
      await state.queue.put({"type": "error", "error": str(e)})

  state.task = asyncio.create_task(run_agent())

  # Read events from queue and stream to client
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
    import traceback

    traceback.print_exc()
    raise fastapi.HTTPException(status_code=500, detail=str(e))


@app.get("/api/query")
async def query_endpoint(
    query: str, session_id: str = "default"
) -> responses.StreamingResponse:
  """Endpoint to trigger a query and get streaming response."""
  headers = {
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
  }
  return responses.StreamingResponse(
      stream_agent_execution(query, session_id),
      media_type="text/event-stream",
      headers=headers,
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


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8000))
  uvicorn.run(app, host="0.0.0.0", port=port)
