from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import asyncio
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

torrent_manager = None
logger = None


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
            "version": "1.0"  # Placeholder
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
            # Inject jellyfin config status into websocket stream for UI logic
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
        if files == "all":
            torrent_manager.rd.select_torrent_files(torrent_id, ["all"])
        else:
            file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
            torrent_manager.rd.select_torrent_files(torrent_id, file_ids)

        if torrent_manager and torrent_id in torrent_manager.torrents:
            torrent = torrent_manager.torrents[torrent_id]

            # Save metadata if provided (critical for Jellyfin mode)
            if tmdb_id:
                torrent.tmdb_id = tmdb_id.strip()
            if media_type:
                torrent.media_type = media_type.strip()

            # Trigger metadata fetch immediately to fail fast if ID invalid
            if torrent_manager.config_data.get("jellyfin_mode") and torrent.tmdb_id and not torrent.canonical_title:
                svc = torrent_manager.metadata_service
                if svc:
                    title = svc.fetch_metadata(torrent.tmdb_id, torrent.media_type or "tv")
                    if title:
                        torrent.canonical_title = title
                        # Cache if it's a show
                        if torrent.media_type == "tv":
                            svc.update_cache(torrent.name, torrent.tmdb_id)
                    else:
                        raise Exception("Invalid TMDB ID or API error")

            if files == "all":
                torrent.selected_files = [f["name"] for f in torrent.files]
            else:
                file_ids = [int(f) for f in files.split(",") if f.strip().isdigit()]
                torrent.selected_files = [f["name"] for f in torrent.files if f["id"] in file_ids]

            if folder_name and folder_name.strip():
                torrent.custom_folder_name = folder_name.strip()

            if strip_pattern and strip_pattern.strip():
                torrent.filename_strip_pattern = strip_pattern.strip()

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