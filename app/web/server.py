from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import asyncio
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Injected from main.py
torrent_manager = None
logger = None


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    torrents = torrent_manager.get_status() if torrent_manager else {}
    return templates.TemplateResponse("index.html", {"request": request, "torrents": torrents})


@app.post("/add")
async def add_magnet(magnet: str = Form(...), quick_download: str = Form("true")):
    if torrent_manager:
        quick = quick_download.lower() == "true"
        tid = torrent_manager.add_magnet(magnet, quick_download=quick)
        return {"status": "ok", "torrent_id": tid}
    return {"status": "error", "message": "manager not ready"}


@app.post("/set_metadata/{torrent_id}")
async def set_metadata(torrent_id: str, tmdb_id: str = Form(...)):
    """Set TMDB ID for a specific torrent."""
    if not torrent_manager:
        return {"status": "error", "message": "Manager not ready"}

    with torrent_manager.lock:
        torrent = torrent_manager.torrents.get(torrent_id)
        if not torrent:
            raise HTTPException(status_code=404, detail="Torrent not found")

        torrent.tmdb_id = tmdb_id.strip()
        if logger:
            logger.info(f"Updated metadata for {torrent_id}: TMDB ID = {torrent.tmdb_id}")

        # TRIGGER: If waiting in Unsorted, move it now
        if torrent.state == TorrentState.WAITING_FOR_METADATA:
            torrent_manager.retry_import_with_metadata(torrent_id)

    return {"status": "ok", "torrent_id": torrent_id, "tmdb_id": torrent.tmdb_id}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        await asyncio.sleep(1)
        while True:
            if not torrent_manager:
                await asyncio.sleep(1)
                continue
            data = torrent_manager.get_status()
            await websocket.send_json(data or {"status": "no torrents yet"})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        if logger:
            logger.warning(f"WebSocket error: {e}")


@app.post("/select_files/{torrent_id}")
async def select_files(torrent_id: str, files: str = Form(...), folder_name: str = Form(None),
                       strip_pattern: str = Form(None)):
    try:
        if files == "all":
            torrent_manager.rd.select_torrent_files(torrent_id, ["all"])
        else:
            fids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            torrent_manager.rd.select_torrent_files(torrent_id, fids)

        if torrent_manager and torrent_id in torrent_manager.torrents:
            t = torrent_manager.torrents[torrent_id]
            if files == "all":
                t.selected_files = [f["name"] for f in t.files]
            else:
                fids = [int(f) for f in files.split(",") if f.strip().isdigit()]
                t.selected_files = [f["name"] for f in t.files if f["id"] in fids]

            if folder_name and folder_name.strip():
                t.custom_folder_name = folder_name.strip()

            if strip_pattern and strip_pattern.strip():
                t.filename_strip_pattern = strip_pattern.strip()

        return {"status": "ok", "torrent_id": torrent_id}
    except Exception as e:
        if logger:
            logger.error(f"Error selecting files: {e}")
        return {"status": "error", "error": str(e)}


@app.get("/version")
async def get_version():
    try:
        with open("/app/commit.txt") as f:
            commit = f.read().strip()
    except:
        commit = "unknown"
    try:
        with open("/app/branch.txt") as f:
            branch = f.read().strip()
    except:
        branch = "unknown"
    return {"branch": branch, "commit": commit}