from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect
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
async def add_magnet(magnet: str = Form(...)):
    if torrent_manager:
        torrent_id = torrent_manager.add_magnet(magnet)
        return {"status": "ok", "torrent_id": torrent_id}
    return {"status": "error", "message": "torrent manager not ready"}


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
async def select_files(torrent_id: str, files: str = Form(...)):
    """
    Select specific files (comma-separated IDs or 'all') for a torrent.
    """
    try:
        if files == "all":
            torrent_manager.rd.select_torrent_files(torrent_id, ["all"])
        else:
            file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            torrent_manager.rd.select_torrent_files(torrent_id, file_ids)

        logger.info(f"Selected files {files} for torrent {torrent_id}.")
        return {"status": "ok", "torrent_id": torrent_id, "selected": files}
    except Exception as e:
        logger.error(f"Error selecting files for {torrent_id}: {e}")
        return {"status": "error", "error": str(e)}
