"""Full production server — agent-building + frontend serving.

Startup order:
  1. ADK app  →  /run  /apps/*  /list-apps
  2. /ws/telemetry WebSocket  (BEFORE any catch-all so ADK can't intercept it)
  3. Custom API routes
  4. Static-file catch-all  (LAST)

Fixes in this version:
  * WebSocket 403 – route registered first, returns HELLO even when hub absent
  * build_pipeline crash – _admin_ctx wrapped (see build_pipeline.py fix too)
  * Model defaulted to gemini-3.6-flash everywhere
  * tectonic_runtime absent – every import wrapped in try/except
"""

import json
import os
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── Config ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
HOST = os.getenv("AGENT_BUILDER_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENT_BUILDER_PORT", "8080"))
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"
GENERATED_AGENTS_DIR = PROJECT_ROOT / os.getenv("AGENT_BUILDER_OUTPUT_DIR", "generated_agents")

GENERATED_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

origins = ["*"] if os.getenv("K_SERVICE") else os.getenv("ALLOWED_ORIGINS", "*").split(",")
print(f"[STARTUP] root={PROJECT_ROOT}  frontend={FRONTEND_DIR.exists()}")

ADK_AVAILABLE = TELEMETRY_AVAILABLE = RUNTIME_AVAILABLE = False

# ── Sanitize API keys: strip whitespace/newlines that cause header injection ───
# Secret Manager sometimes stores the secret with a trailing newline.
# aiohttp rejects HTTP headers containing control characters → 500 on every call.
def _clean_env_key(name: str) -> None:
    val = os.environ.get(name, "")
    cleaned = val.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    if cleaned != val:
        print(f"[STARTUP] WARNING: {name} contained whitespace/control chars — stripped.")
    if cleaned:
        os.environ[name] = cleaned

for _k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
    _clean_env_key(_k)

_key = os.environ.get("GOOGLE_API_KEY", "")
print(f"[STARTUP] GOOGLE_API_KEY present={bool(_key)} length={len(_key)} first6={_key[:6] if _key else 'N/A'}")

# ── 1. ADK app ─────────────────────────────────────────────────────────────────
try:
    from google.adk.cli.fast_api import get_fast_api_app
    try:
        from google.adk.cli.utils._nested_agent_loader import NestedAgentLoader
    except ImportError:
        from google.adk.cli.utils import NestedAgentLoader
    agent_loader = NestedAgentLoader(str(PROJECT_ROOT))
    app = get_fast_api_app(
        agents_dir=str(PROJECT_ROOT),
        agent_loader=agent_loader,
        web=False,
        host=HOST,
        port=PORT,
        allow_origins=origins,
        extra_plugins=["agent_builder.telemetry.telemetry_plugin"],
    )
    ADK_AVAILABLE = True
    print("[STARTUP] ADK ready  →  /run  /apps/*")
except Exception as exc:
    print(f"[STARTUP] ADK unavailable: {exc}")
    app = FastAPI(title="Tectonic Agent Builder")
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── 2. Telemetry hub ───────────────────────────────────────────────────────────
hub = None
try:
    from agent_builder.telemetry import hub as _hub
    hub = _hub
    TELEMETRY_AVAILABLE = True
    print("[STARTUP] Telemetry hub ready")
except Exception as exc:
    print(f"[STARTUP] Telemetry unavailable: {exc}")


# ── Module definitions – emitted on every WS connect ──────────────────────────
# tectonic_runtime is not loaded in Cloud Run, so emit these synthetically so
# the Command Center office floor plan always shows the 10 platform modules.
import time as _time, uuid as _uuid
_SERVER_ID = _uuid.uuid4().hex
_MODULE_DEFS = [
    ("guardrails",         "Guardrails",         "safety",       "Jailbreak detection, denied topics, PII redaction and groundedness checks."),
    ("intent_detection",   "Intent Detection",   "understanding","Classifies requests against an intent taxonomy with multi-intent decomposition."),
    ("short_term_memory",  "Short-Term Memory",  "memory",       "Session working memory with salience-aware overflow summarisation."),
    ("long_term_memory",   "Long-Term Memory",   "memory",       "Durable memory with private and shared scopes."),
    ("tool_orchestration", "Tool Orchestration", "execution",    "Executes registered tools with retry/circuit-breaker resilience."),
    ("agentic_rag",        "Agentic RAG",        "knowledge",    "Multi-hop retrieval-augmented generation over ingested knowledge bases."),
    ("vector_db",          "Vector DB",          "storage",      "Semantic vector store for embeddings and similarity search."),
    ("finops",             "FinOps",             "governance",   "Token-level cost metering, budget enforcement and spend dashboards."),
    ("promptops",          "PromptOps",          "governance",   "Versioned prompt management with A/B testing and rollback."),
    ("auditability",       "Auditability",       "compliance",   "Immutable audit trail of every agent action and module call."),
]

def _make_module_events() -> list[str]:
    """Build MODULE_REGISTERED + MODULE_STATE events for all 10 platform modules.

    Critical rules from the frontend store (useTelemetryStore.ts):
    1. seq must be POSITIVE and > lastSeq (which starts at 0 after HELLO resets).
       Negative seq numbers are filtered by: `if (seq <= get().lastSeq) return;`
    2. MODULE_STATE requires active_calls and total_calls fields (ModuleStateEvent type).
    3. MODULE_REGISTERED must send: module, label, description, category, operations.
    """
    now = _time.time()
    out = []
    for i, (mod, label, cat, desc) in enumerate(_MODULE_DEFS):
        seq_reg   = 1 + i * 2       # 1, 3, 5, 7, ... 19
        seq_state = 2 + i * 2       # 2, 4, 6, 8, ... 20
        out.append(json.dumps({
            "type": "MODULE_REGISTERED",
            "seq": seq_reg,
            "timestamp": now,
            "module": mod,
            "label": label,
            "category": cat,
            "description": desc,
            "operations": [],           # ModuleOperationSpec[] — empty is valid
        }))
        out.append(json.dumps({
            "type": "MODULE_STATE",
            "seq": seq_state,
            "timestamp": now,
            "module": mod,
            "state": "idle",
            "active_calls": 0,          # required by ModuleStateEvent
            "total_calls": 0,           # required by ModuleStateEvent
        }))
    return out


# ── 3. WebSocket /ws/telemetry  (BEFORE any catch-all) ────────────────────────
@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    """Streams live agent/module events to the Command Center UI.

    Event ordering is critical (useTelemetryStore.ts):
      1. ONE HELLO  — uses hub.server_id when hub exists so it matches the
                      hub's own HELLO (which we then discard from the queue).
                      Two different server_ids would cause a double reset that
                      wipes the module events we send in step 2.
      2. MODULE_REGISTERED + MODULE_STATE x10  — populates the office floor plan.
         These are STICKY_TYPES so they bypass the seq-dedup filter and always land.
      3. Hub replay (agent history, recent events) — hub's HELLO is skipped.
      4. Live streaming events as they arrive.

    When hub is None we keep the socket open so the frontend status stays 'open'
    and the "Simulate office" button runs the local scripted replay.
    """
    await websocket.accept()

    # Pick the server_id: use hub's so only ONE identity is ever seen per session.
    server_id = hub.server_id if hub is not None else _SERVER_ID

    # 1. HELLO
    await websocket.send_text(json.dumps({
        "type": "HELLO", "seq": 0, "timestamp": _time.time(), "server_id": server_id,
    }))

    # 2. Module definitions — sent AFTER HELLO so they land after the reset.
    #    STICKY_TYPES bypasses seq-dedup so they're always processed.
    for msg in _make_module_events():
        await websocket.send_text(msg)

    if hub is None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        return

    # 3 + 4. Hub replay + live stream, but skip the hub's own HELLO (seq 0)
    #         to avoid a second server_id reset that would wipe the modules.
    queue = hub.subscribe()
    print(f"[WS] connected  ({len(hub._subscribers)} total)")
    try:
        while True:
            event = await queue.get()
            # Skip the hub's HELLO — we already sent one with the same server_id
            if event.get("type") == "HELLO":
                continue
            await websocket.send_text(json.dumps(event, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(queue)
        print("[WS] disconnected")

# ── 4. Optional tectonic_runtime ──────────────────────────────────────────────
_runtime = None
_is_running_fn = lambda: False
try:
    from tectonic_runtime import get_runtime
    from tectonic_runtime.simulation import is_running
    _runtime = get_runtime()
    _is_running_fn = is_running
    RUNTIME_AVAILABLE = True
    print("[STARTUP] tectonic_runtime loaded")
except Exception as exc:
    print(f"[STARTUP] tectonic_runtime unavailable (optional): {exc}")

# ── 5. Custom API routes ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "adk": ADK_AVAILABLE,
            "telemetry": TELEMETRY_AVAILABLE, "runtime": RUNTIME_AVAILABLE}

@app.get("/api/env")
async def get_env():
    key = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    return {"hasKey": bool(key), "apiKey": key,
            "model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), "envExists": True}

@app.post("/api/env")
async def set_env(body: dict = Body(...)):
    return {"success": True, "message": "Managed via GCP Secret Manager"}

@app.get("/api/system/agents")
async def list_system_agents():
    """Returns CreatedAgent[] shape expected by the frontend TypeScript type:
       { name, appName, path, files, promptPreview, hasAgentPy, lastModified }
    """
    import datetime
    agents = []
    if GENERATED_AGENTS_DIR.exists():
        for d in sorted(GENERATED_AGENTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and not d.name.startswith((".", "_")):
                pkg = d / d.name
                ap = pkg / "agent.py"
                if not ap.exists():
                    continue
                # Collect all .py files as the file list (matches CreatedAgent.files: string[])
                file_list = [
                    str(f.relative_to(d)).replace("\\", "/")
                    for f in d.rglob("*.py")
                    if f.is_file()
                ]
                # appName = generated_agents.<project>.<project>  (ADK dotted path)
                app_name = f"generated_agents.{d.name}.{d.name}"
                agents.append({
                    "name": d.name,
                    "appName": app_name,
                    "path": str(d).replace("\\", "/"),
                    "files": file_list,
                    "promptPreview": "",
                    "hasAgentPy": True,
                    "lastModified": datetime.datetime.fromtimestamp(ap.stat().st_mtime).isoformat(),
                })
    return {"agents": agents}

@app.get("/api/system/agents/{name}/files")
async def get_agent_files(name: str):
    d = GENERATED_AGENTS_DIR / name
    if not d.exists():
        raise HTTPException(404, f"Agent '{name}' not found")
    files = {}
    for f in d.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            try:
                files[str(f.relative_to(d)).replace("\\", "/")] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    return {"files": files}

@app.post("/api/system/agents/{name}/files")
async def save_agent_file(name: str, body: dict = Body(...)):
    d = GENERATED_AGENTS_DIR / name
    if not d.exists():
        raise HTTPException(404, f"Agent '{name}' not found")
    fp, content = body.get("filePath"), body.get("content")
    if not fp or content is None:
        raise HTTPException(400, "filePath and content required")
    full = d / fp
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return {"success": True}

@app.get("/api/system/agents/{name}/env")
async def get_agent_env(name: str):
    """Returns AgentEnvConfig shape expected by the frontend TypeScript type:
       { agentName, hasKey, apiKey, model, agentEnvExists, envPath }
    """
    import re
    env_path = GENERATED_AGENTS_DIR / name / ".env"
    env_path_str = str(env_path).replace("\\", "/")
    if not env_path.exists():
        return {
            "agentName": name,
            "hasKey": False,
            "apiKey": "",
            "model": "gemini-3.6-flash",
            "agentEnvExists": False,
            "envPath": env_path_str,
        }
    txt = env_path.read_text(encoding="utf-8")
    gk = re.search(r"^GOOGLE_API_KEY=(.*)$", txt, re.MULTILINE)
    mk = re.search(r"^GEMINI_MODEL=(.*)$", txt, re.MULTILINE)
    key = gk.group(1).strip() if gk else ""
    return {
        "agentName": name,
        "hasKey": bool(key),
        "apiKey": key,
        "model": mk.group(1).strip() if mk else "gemini-3.6-flash",
        "agentEnvExists": True,
        "envPath": env_path_str,
    }

@app.post("/api/system/agents/{name}/env")
async def save_agent_env(name: str, body: dict = Body(...)):
    d = GENERATED_AGENTS_DIR / name
    if not d.exists():
        raise HTTPException(404, f"Agent '{name}' not found")
    key = (body.get("apiKey") or "").strip()
    model = (body.get("model") or "gemini-3.6-flash").strip()
    if not key:
        raise HTTPException(400, "apiKey required")
    env_file = d / ".env"
    env_file.write_text(f"GOOGLE_API_KEY={key}\nGEMINI_API_KEY={key}\nGEMINI_MODEL={model}\n")
    return {
        "success": True,
        "message": f"Saved .env for {name}",
        "envPath": str(env_file).replace("\\", "/"),
    }

@app.get("/modules")
async def list_modules():
    if RUNTIME_AVAILABLE:
        return {"modules": _runtime.describe(), "simulation_running": _is_running_fn()}
    return {"modules": [], "simulation_running": False}

@app.post("/modules/simulate")
async def simulate_office():
    if not RUNTIME_AVAILABLE:
        return {"status": "runtime_unavailable"}
    if _is_running_fn():
        return {"status": "already_running"}
    return {"status": "started"}

@app.post("/modules/{module}/{operation}")
async def invoke_module(module: str, operation: str, payload: dict = Body(default_factory=dict)):
    if not RUNTIME_AVAILABLE:
        raise HTTPException(503, "Runtime modules not available")
    if module not in _runtime.modules:
        raise HTTPException(404, f"Unknown module '{module}'")
    from tectonic_runtime import CallContext
    ctx = CallContext(agent_id="operator", role="operator", session_id="command-center")
    return (await _runtime.invoke(module, operation, payload, ctx)).to_dict()

@app.get("/directory/agents")
async def directory_agents():
    try:
        from agent_builder import directory
        return {"agents": directory.list_entries()}
    except Exception as exc:
        print(f"[ERROR] directory_agents: {exc}")
        return {"agents": []}

@app.post("/directory/chat")
async def directory_chat(body: dict = Body(...)):
    try:
        from agent_builder import directory
        return await directory.run_agent(
            str(body.get("target", "")), str(body.get("message", "")),
            session_key=str(body.get("session_key") or "studio"))
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.delete("/directory/agents/{target:path}")
async def directory_delete(target: str):
    try:
        from agent_builder import directory
        return await directory.delete_agent(target)
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.post("/directory/edit")
async def directory_edit(body: dict = Body(...)):
    try:
        from agent_builder import editor
        return await editor.edit_agent(
            str(body.get("target", "")), str(body.get("request", "")),
            session_key=str(body.get("session_key") or "studio"))
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.post("/directory/edit/undo")
async def directory_edit_undo(body: dict = Body(...)):
    try:
        from agent_builder import editor
        return await editor.undo_last_edit(str(body.get("target", "")))
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.get("/directory/edits/{target:path}")
async def directory_edits(target: str):
    try:
        from agent_builder import editor
        return editor.list_edits(target)
    except Exception as exc:
        raise HTTPException(404, str(exc))

@app.get("/studio/settings")
async def studio_settings_get():
    try:
        from agent_builder import model_settings
        return model_settings.status()
    except Exception as exc:
        return {"model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), "error": str(exc)}

@app.post("/studio/settings")
async def studio_settings_post(body: dict = Body(...)):
    try:
        from agent_builder import model_settings
        return await model_settings.apply(
            str(body.get("model", "")),
            body.get("anthropic_api_key"),
            body.get("google_api_key"))
    except Exception as exc:
        raise HTTPException(400, str(exc))

# ── 6. Static files — LAST ─────────────────────────────────────────────────────
if FRONTEND_DIR.exists():
    if (FRONTEND_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    _API_PREFIXES = ("api/", "health", "modules", "directory", "studio",
                     "run", "apps/", "list-apps", "ws/")

    @app.get("/{path:path}")
    async def serve_spa(request: Request, path: str):
        if any(path.startswith(p) for p in _API_PREFIXES):
            raise HTTPException(404)
        fp = FRONTEND_DIR / path
        if fp.exists() and fp.is_file():
            return FileResponse(fp)
        idx = FRONTEND_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        raise HTTPException(404)
else:
    @app.get("/")
    async def root():
        return {"message": "Tectonic backend running – frontend not built"}

if __name__ == "__main__":
    print(f"[STARTUP] Listening on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
