"""Backend entrypoint: the ADK API server plus the Command Center telemetry WebSocket.

Replaces ``adk api_server`` — same routes (/run, /run_sse, /apps/...) on the same port, with a
global TelemetryPlugin attached to every agent run and ``/ws/telemetry`` streaming its events.
The shared core modules (tectonic_runtime) stream their activity on the same socket.

    python server.py            # http://127.0.0.1:8000, ws://127.0.0.1:8000/ws/telemetry
"""

import asyncio
import gc
import os
from pathlib import Path

import uvicorn
from fastapi import Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils._nested_agent_loader import NestedAgentLoader

from agent_builder import directory, editor, model_settings
from agent_builder.telemetry import hub
from agent_builder.telemetry import router as telemetry_router
from tectonic_runtime import bus as module_bus
from tectonic_runtime import CallContext, get_runtime
from tectonic_runtime.simulation import is_running, run_office_simulation

PROJECT_ROOT = Path(__file__).resolve().parent
HOST = os.getenv("AGENT_BUILDER_HOST", "127.0.0.1")
PORT = int(os.getenv("AGENT_BUILDER_PORT", "8000"))
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"

# Get allowed origins from environment or use defaults
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
# Add wildcard for Cloud Run deployments
if os.getenv("K_SERVICE"):  # Cloud Run sets this environment variable
    ALLOWED_ORIGINS.append("*")

agent_loader = NestedAgentLoader(str(PROJECT_ROOT))
app = get_fast_api_app(
    agents_dir=str(PROJECT_ROOT),
    # Generated agents are addressed by dotted path (generated_agents.<project>.<project>). With
    # web=False ADK defaults to the flat AgentLoader, which rejects dots; the nested loader accepts them.
    agent_loader=agent_loader,
    web=False,
    host=HOST,
    port=PORT,
    allow_origins=ALLOWED_ORIGINS,
    extra_plugins=["agent_builder.telemetry.telemetry_plugin"],
)
app.include_router(telemetry_router)

# --- API routes for environment configuration (replaces vite plugin in production) ---
@app.get("/api/env")
async def get_env():
    """Return environment configuration status."""
    google_key = os.getenv("GOOGLE_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    raw_key = google_key or gemini_key
    return {
        "hasKey": len(raw_key) > 0,
        "apiKey": raw_key,
        "model": model,
        "envExists": True
    }

@app.post("/api/env")
async def set_env(body: dict = Body(...)):
    """In production, env vars are set via Secret Manager, so this is a no-op."""
    return {"success": True, "message": "Environment variables are managed via GCP Secret Manager"}


# --- System API routes for generated agents (replaces vite plugin in production) ---
GENERATED_AGENTS_DIR = PROJECT_ROOT / os.getenv("AGENT_BUILDER_OUTPUT_DIR", "generated_agents")


@app.get("/api/system/agents")
async def list_system_agents():
    """List all generated agents from disk."""
    agents = []
    if GENERATED_AGENTS_DIR.exists():
        for agent_dir in GENERATED_AGENTS_DIR.iterdir():
            if agent_dir.is_dir() and not agent_dir.name.startswith('.'):
                agent_py = agent_dir / agent_dir.name / "agent.py"
                if agent_py.exists():
                    import datetime
                    stat = agent_py.stat()
                    agents.append({
                        "name": agent_dir.name,
                        "lastModified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "hasEnv": (agent_dir / ".env").exists()
                    })
    return {"agents": agents}


@app.get("/api/system/agents/{agent_name}/files")
async def get_agent_files(agent_name: str):
    """Get all files for a generated agent."""
    agent_dir = GENERATED_AGENTS_DIR / agent_name
    if not agent_dir.exists():
        raise HTTPException(404, f"Agent {agent_name} not found")
    
    files = {}
    for file_path in agent_dir.rglob("*"):
        if file_path.is_file() and not file_path.name.startswith('.'):
            rel_path = str(file_path.relative_to(agent_dir))
            try:
                files[rel_path] = file_path.read_text(encoding='utf-8')
            except Exception:
                pass  # Skip binary files
    return {"files": files}


@app.post("/api/system/agents/{agent_name}/files")
async def save_agent_file(agent_name: str, body: dict = Body(...)):
    """Save a file for a generated agent."""
    agent_dir = GENERATED_AGENTS_DIR / agent_name
    if not agent_dir.exists():
        raise HTTPException(404, f"Agent {agent_name} not found")
    
    file_path = body.get("filePath")
    content = body.get("content")
    if not file_path or content is None:
        raise HTTPException(400, "filePath and content required")
    
    full_path = agent_dir / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding='utf-8')
    return {"success": True, "message": f"Saved {file_path}"}


@app.get("/api/system/agents/{agent_name}/env")
async def get_agent_env(agent_name: str):
    """Get environment config for a generated agent."""
    agent_dir = GENERATED_AGENTS_DIR / agent_name
    env_path = agent_dir / ".env"
    
    if not env_path.exists():
        return {"hasKey": False, "apiKey": "", "model": "gemini-3.6-flash"}
    
    content = env_path.read_text(encoding='utf-8')
    import re
    google_key = re.search(r'^GOOGLE_API_KEY=(.*)$', content, re.MULTILINE)
    gemini_key = re.search(r'^GEMINI_API_KEY=(.*)$', content, re.MULTILINE)
    model_match = re.search(r'^GEMINI_MODEL=(.*)$', content, re.MULTILINE)
    
    raw_key = (google_key.group(1).strip() if google_key else '') or (gemini_key.group(1).strip() if gemini_key else '')
    model = model_match.group(1).strip() if model_match else 'gemini-3.6-flash'
    
    return {"hasKey": len(raw_key) > 0, "apiKey": raw_key, "model": model}


@app.post("/api/system/agents/{agent_name}/env")
async def save_agent_env(agent_name: str, body: dict = Body(...)):
    """Save environment config for a generated agent."""
    agent_dir = GENERATED_AGENTS_DIR / agent_name
    if not agent_dir.exists():
        raise HTTPException(404, f"Agent {agent_name} not found")
    
    api_key = body.get("apiKey", "").strip()
    model = body.get("model", "gemini-3.6-flash").strip()
    
    if not api_key:
        raise HTTPException(400, "apiKey required")
    
    env_content = f"GOOGLE_API_KEY={api_key}\nGEMINI_API_KEY={api_key}\nGEMINI_MODEL={model}\n"
    (agent_dir / ".env").write_text(env_content, encoding='utf-8')
    
    return {"success": True, "message": f"Saved .env for {agent_name}", "envPath": f"generated_agents/{agent_name}/.env"}


def _forward_module_event(event: dict) -> None:
    payload = {k: v for k, v in event.items() if k not in ("type", "timestamp")}
    hub.emit(event["type"], **payload)


module_bus.subscribe(_forward_module_event)
runtime = get_runtime()  # registers every module (state "offline" until first use)
_background: set[asyncio.Task] = set()

# Audit trail: agent telemetry comes from the hub (module events reach it directly off the bus).
_audit = runtime.module("auditability")
hub.add_listener(lambda event: None if event.get("type", "").startswith("MODULE_") else _audit.observe(event))  # type: ignore[attr-defined]

# The Command Center UI acts as the platform operator: admin-equivalent, separately attributed.
OPERATOR = CallContext(agent_id="operator", role="operator", session_id="command-center")


@app.post("/modules/{module}/{operation}")
async def invoke_module(module: str, operation: str, payload: dict = Body(default_factory=dict)) -> dict:
    if module not in runtime.modules:
        raise HTTPException(404, f"unknown module {module!r}")
    result = await runtime.invoke(module, operation, payload, OPERATOR)
    return result.to_dict()


@app.on_event("shutdown")
async def _flush_state() -> None:
    await _audit.flush()  # type: ignore[attr-defined]
    finops = runtime.module("finops")
    if getattr(finops, "repository", None) is not None:
        finops.repository.flush()  # type: ignore[attr-defined]


@app.get("/modules")
async def list_modules() -> dict:
    return {"modules": runtime.describe(), "simulation_running": is_running()}


@app.post("/modules/simulate")
async def simulate_office() -> dict:
    if is_running():
        return {"status": "already_running"}
    task = asyncio.create_task(run_office_simulation(runtime, agent_events=hub.emit))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "started"}


# --- Agent directory: every agent reachable from the Builder chat, and deletable -------------------
def _evict_app(app_name: str) -> None:
    """Drop a generated app's cached agent and runner so its next run loads the code on disk."""
    agent_loader.remove_agent_from_cache(app_name)
    # get_fast_api_app does not expose its ApiServer; the runner cache lives on that single instance.
    from google.adk.cli.api_server import ApiServer

    for server in (o for o in gc.get_objects() if isinstance(o, ApiServer)):
        server.runners_to_clean.add(app_name)


directory.on_evict(_evict_app)


@app.get("/directory/agents")
async def directory_agents() -> dict:
    return {"agents": directory.list_entries()}


@app.post("/directory/chat")
async def directory_chat(body: dict = Body(...)) -> dict:
    try:
        return await directory.run_agent(str(body.get("target", "")), str(body.get("message", "")),
                                         session_key=str(body.get("session_key") or "studio"))
    except directory.DirectoryError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/studio/settings")
async def studio_settings() -> dict:
    """The Builder's model, the models offered, and which provider keys are configured (masked)."""
    return model_settings.status()


@app.post("/studio/settings")
async def studio_settings_update(body: dict = Body(...)) -> dict:
    """{model, anthropic_api_key?, google_api_key?}: validate, save to .env and switch the Builder live."""
    try:
        return await model_settings.apply(str(body.get("model", "")), body.get("anthropic_api_key"), body.get("google_api_key"))
    except model_settings.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/directory/edit")
async def directory_edit(body: dict = Body(...)) -> dict:
    """Conversational edit of a generated agent: {target, request, session_key} → summary, diffs, status."""
    try:
        return await editor.edit_agent(str(body.get("target", "")), str(body.get("request", "")),
                                       session_key=str(body.get("session_key") or "studio"))
    except directory.DirectoryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/directory/edit/undo")
async def directory_edit_undo(body: dict = Body(...)) -> dict:
    try:
        return await editor.undo_last_edit(str(body.get("target", "")))
    except directory.DirectoryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/directory/edits/{target:path}")
async def directory_edits(target: str) -> dict:
    try:
        return editor.list_edits(target)
    except directory.DirectoryError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/directory/agents/{target:path}")
async def directory_delete(target: str) -> dict:
    try:
        return await directory.delete_agent(target)
    except directory.DirectoryError as exc:
        raise HTTPException(400, str(exc)) from exc


# --- Static file serving for production (serves built frontend) ---
# This must be AFTER all API routes to not interfere with them
if FRONTEND_DIR.exists():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="static-assets")
    
    # Serve other static files from frontend dist
    @app.get("/{full_path:path}")
    async def serve_frontend(request: Request, full_path: str):
        """Serve frontend files. Falls back to index.html for SPA routing."""
        # Skip API routes
        if full_path.startswith(("api/", "ws/", "run", "apps/", "list-apps", "directory/", "modules")):
            raise HTTPException(404, "Not found")
        
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        
        # For SPA routing, return index.html
        index_path = FRONTEND_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        
        raise HTTPException(404, "Frontend not built")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
