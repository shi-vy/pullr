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

# These are injected from main.py
torrent_manager = None
logger = None


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    torrents = torrent_manager.get_status() if torrent_manager else {}
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "torrents": torrents}
    )


@app.post("/add")
async def add_magnet(magnet: str = Form(...), quick_download: str = Form("true")):
    """
    Add a magnet link to the queue.

    Args:
        magnet: The magnet link
        quick_download: "true" to enable automatic file selection, "false" to require manual selection
    """
    if torrent_manager:
        quick_download_enabled = quick_download.lower() == "true"
        torrent_id = torrent_manager.add_magnet(magnet, quick_download=quick_download_enabled)
        return {"status": "ok", "torrent_id": torrent_id}
    return {"status": "error", "message": "torrent manager not ready"}


@app.post("/set_metadata/{torrent_id}")
async def set_metadata(torrent_id: str, tmdb_id: str = Form(...)):
    """Set TMDB ID for a specific torrent."""
    if not torrent_manager:
        return {"status": "error", "message": "Manager not ready"}

    # Access the lock to ensure thread safety
    with torrent_manager.lock:
        torrent = torrent_manager.torrents.get(torrent_id)
        if not torrent:
            raise HTTPException(status_code=404, detail="Torrent not found")

        # Set the attribute dynamically
        torrent.tmdb_id = tmdb_id.strip()
        logger.info(f"Updated metadata for {torrent_id}: TMDB ID = {torrent.tmdb_id}")

    return {"status": "ok", "torrent_id": torrent_id, "tmdb_id": torrent.tmdb_id}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        # Wait briefly for torrent_manager to be ready
        await asyncio.sleep(1)

        while True:
            if not torrent_manager:
                await asyncio.sleep(1)
                continue

            data = torrent_manager.get_status()
            await websocket.send_json(data or {"status": "no torrents yet"})
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        if logger:
            logger.info("WebSocket client disconnected.")
    except Exception as e:
        if logger:
            logger.warning(f"WebSocket error: {e}")


@app.post("/select_files/{torrent_id}")
async def select_files(
        torrent_id: str,
        files: str = Form(...),
        folder_name: str = Form(None),
        strip_pattern: str = Form(None)
):
    """
    Select specific files (comma-separated IDs or 'all') for a torrent.
    Also accepts optional folder_name for multi-file downloads and strip_pattern for filename formatting.
    """
    try:
        if files == "all":
            torrent_manager.rd.select_torrent_files(torrent_id, ["all"])
        else:
            file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            torrent_manager.rd.select_torrent_files(torrent_id, file_ids)

        if torrent_manager and torrent_id in torrent_manager.torrents:
            torrent = torrent_manager.torrents[torrent_id]

            # Store selected file names for display
            if files == "all":
                torrent.selected_files = [f["name"] for f in torrent.files]
            else:
                file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
                torrent.selected_files = [
                    f["name"] for f in torrent.files if f["id"] in file_ids
                ]

            # Store custom folder name if provided and multiple files selected
            if folder_name and folder_name.strip():
                torrent.custom_folder_name = folder_name.strip()
                logger.info(f"Set custom folder name '{folder_name}' for torrent {torrent_id}")

            # Store filename strip pattern if provided
            if strip_pattern and strip_pattern.strip():
                torrent.filename_strip_pattern = strip_pattern.strip()
                logger.info(f"Set filename strip pattern '{strip_pattern}' for torrent {torrent_id}")

        logger.info(f"Selected files {files} for torrent {torrent_id}.")
        return {"status": "ok", "torrent_id": torrent_id, "selected": files}
    except Exception as e:
        logger.error(f"Error selecting files for {torrent_id}: {e}")
        return {"status": "error", "error": str(e)}


@app.get("/version")
async def get_version():
    """Return build metadata for display in the UI."""
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