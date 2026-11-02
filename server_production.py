"""Production-ready backend server for Cloud Run deployment."""

import os
from pathlib import Path
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent
HOST = os.getenv("AGENT_BUILDER_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENT_BUILDER_PORT", "8080"))
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"
GENERATED_AGENTS_DIR = PROJECT_ROOT / os.getenv("AGENT_BUILDER_OUTPUT_DIR", "generated_agents")

GENERATED_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Tectonic Agent Builder", version="1.0.0")

origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/api/env")
async def get_env():
    raw_key = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    return {"hasKey": len(raw_key) > 0, "apiKey": raw_key, "model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), "envExists": True}

@app.post("/api/env")
async def set_env(body: dict = Body(...)):
    return {"success": True, "message": "Managed via GCP Secret Manager"}

@app.get("/api/system/agents")
async def list_system_agents():
    import datetime
    agents = []
    if GENERATED_AGENTS_DIR.exists():
        for d in GENERATED_AGENTS_DIR.iterdir():
            if d.is_dir() and not d.name.startswith('.') and d.name != "_platform":
                ap = d / d.name / "agent.py"
                if ap.exists():
                    agents.append({"name": d.name, "lastModified": datetime.datetime.fromtimestamp(ap.stat().st_mtime).isoformat(), "hasEnv": (d / ".env").exists()})
    return {"agents": agents}

@app.get("/api/system/agents/{name}/files")
async def get_agent_files(name: str):
    d = GENERATED_AGENTS_DIR / name
    if not d.exists(): raise HTTPException(404, f"Agent {name} not found")
    files = {}
    for f in d.rglob("*"):
        if f.is_file() and not f.name.startswith('.'):
            try: files[str(f.relative_to(d)).replace("\\", "/")] = f.read_text(encoding='utf-8')
            except: pass
    return {"files": files}

@app.get("/modules")
async def list_modules():
    return {"modules": {}, "simulation_running": False}

@app.get("/directory/agents")
async def directory_agents():
    return {"agents": []}

if FRONTEND_DIR.exists():
    if (FRONTEND_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")
    
    @app.get("/{path:path}")
    async def serve_spa(request: Request, path: str):
        if path.startswith(("api/", "health", "modules", "directory/")):
            raise HTTPException(404)
        fp = FRONTEND_DIR / path
        if fp.exists() and fp.is_file():
            return FileResponse(fp)
        idx = FRONTEND_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        raise HTTPException(404)

if __name__ == "__main__":
    print(f"Starting on {HOST}:{PORT}, frontend exists: {FRONTEND_DIR.exists()}")
    uvicorn.run(app, host=HOST, port=PORT)
