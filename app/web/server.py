from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import asyncio
import logging
from pathlib import Path
from core.states import TorrentState

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

torrent_manager = None
logger = logging.getLogger("pullr.web")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    status_data = torrent_manager.get_status() if torrent_manager else {}
    jellyfin_mode = torrent_manager.config_data.get("jellyfin_mode", False) if torrent_manager else False

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "torrents": status_data,
            "jellyfin_mode": jellyfin_mode,
            "version": "1.0"
        }
    )


@app.post("/add")
async def add_magnet(
        magnet: str = Form(...),
        quick_download: str = Form("true"),
        tmdb_id: str = Form(None),
        media_type: str = Form(None)
):
    if torrent_manager:
        quick = quick_download.lower() == "true"
        tid = torrent_manager.add_magnet(
            magnet,
            quick_download=quick,
            tmdb_id=tmdb_id,
            media_type=media_type
        )
        return {"status": "ok", "torrent_id": tid}
    return {"status": "error", "message": "manager not ready"}


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
            data["config"] = {"jellyfin_mode": torrent_manager.config_data.get("jellyfin_mode", False)}
            await websocket.send_json(data)
            await asyncio.sleep(2)
    except (WebSocketDisconnect, Exception):
        pass


@app.post("/select_files/{torrent_id}")
async def select_files(
        torrent_id: str,
        files: str = Form(...),
        folder_name: str = Form(None),
        strip_pattern: str = Form(None),
        tmdb_id: str = Form(None),
        media_type: str = Form(None)
):
    try:
        logger.info(f"Selecting files for {torrent_id}: raw_files='{files}', tmdb={tmdb_id}")

        if not torrent_manager or torrent_id not in torrent_manager.torrents:
            return {"status": "error", "error": "Torrent not found"}

        torrent = torrent_manager.torrents[torrent_id]

        # 1. Update Metadata
        if tmdb_id:
            torrent.tmdb_id = tmdb_id.strip()
        if media_type:
            torrent.media_type = media_type.strip()

        # Validate metadata if in Jellyfin mode
        if torrent_manager.config_data.get("jellyfin_mode") and torrent.tmdb_id and not torrent.canonical_title:
            svc = torrent_manager.metadata_service
            if svc:
                title = svc.fetch_metadata(torrent.tmdb_id, torrent.media_type or "tv")
                if title:
                    torrent.canonical_title = title
                    if torrent.media_type == "tv":
                        svc.update_cache(torrent.name, torrent.tmdb_id)
                else:
                    raise Exception(f"Could not validate TMDB ID {torrent.tmdb_id}")

        # 2. Select Files in RealDebrid
        if files == "all":
            # Pass list of strings "all" -> map(str) -> "all"
            resp = torrent_manager.rd.select_torrent_files(torrent_id, ["all"])
        else:
            file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            if not file_ids:
                logger.error(f"No valid file IDs found in input: {files}")
                return {"status": "error", "error": "No valid file IDs provided"}

            logger.info(f"Sending file IDs to RD: {file_ids}")
            resp = torrent_manager.rd.select_torrent_files(torrent_id, file_ids)

        # 3. Update Local State
        if files == "all":
            torrent.selected_files = [f["name"] for f in torrent.files]
        else:
            file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            torrent.selected_files = [f["name"] for f in torrent.files if f["id"] in file_ids]

        if folder_name and folder_name.strip():
            torrent.custom_folder_name = folder_name.strip()

        if strip_pattern and strip_pattern.strip():
            torrent.filename_strip_pattern = strip_pattern.strip()

        # FORCE STATE UPDATE
        # This prevents the UI from "hanging" on the selection screen while waiting for the next RD poll
        torrent.state = TorrentState.WAITING_FOR_REALDEBRID
        logger.info(f"Successfully selected files for {torrent_id}. State set to WAITING_FOR_REALDEBRID.")

        return {"status": "ok", "torrent_id": torrent_id}
    except Exception as e:
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