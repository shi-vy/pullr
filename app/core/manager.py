import threading
import time
from pathlib import Path
from .realdebrid import RealDebrid
from .fileops import FileOps
from .states import TorrentState
from .services.queue_service import QueueService
from .services.deletion_service import DeletionService
from .services.external_torrent_service import ExternalTorrentService
from utils.logger import get_logger


class TorrentItem:
    """Represents one torrent in the queue."""

    def __init__(self, magnet_link: str, source: str = "manual", quick_download: bool = True):
        self.id = None  # RD ID
        self.magnet_link = magnet_link
        self.source = source  # "manual" or "external"
        self.state = TorrentState.WAITING_IN_QUEUE
        self.files = []
        self.selected_files = []
        self.progress = 0.0
        self.rd_status = "unknown"
        self.rd_filename = None
        self.rd_links = []
        self.direct_links = []
        self.queue_position = None
        self.error_message = None
        self.custom_folder_name = None
        self.filename_strip_pattern = None
        self.quick_download = quick_download
        self._download_started = False

        # Metadata field (initially None)
        self.tmdb_id = None

    def __repr__(self):
        return f"<TorrentItem id={self.id} source={self.source} state={self.state} tmdb={self.tmdb_id}>"


class TorrentManager:
    """Core logic for managing torrents, polling RD, and coordinating downloads."""

    def __init__(self, config_data):
        self.logger = get_logger()
        self.config_data = config_data
        self.rd = RealDebrid(config_data["realdebrid_api_token"])
        self.fileops = FileOps(self.logger)

        self.torrents = {}  # {torrent_id: TorrentItem}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        # Initialize Services
        self.queue_service = QueueService(self.torrents, self.rd, self.logger)
        self.deletion_service = DeletionService(self.rd, self.logger)
        self.external_service = ExternalTorrentService(self.rd, self.logger)

        self.poll_interval = int(config_data.get("poll_interval_seconds", 5))

    def start(self):
        """Start the background polling thread."""
        self.logger.info("Starting TorrentManager polling thread...")
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

        # Start deletion service
        self.deletion_service.start()

        # Start external torrent scanning
        self.external_service.start_scanning(self._on_external_torrent_found)

    def stop(self):
        """Stop all threads."""
        self.stop_event.set()
        self.deletion_service.stop()
        self.external_service.stop()

    def add_magnet(self, magnet_link, quick_download=True):
        """Add a manual magnet link."""
        with self.lock:
            # 1. Add magnet to RD
            rd_resp = self.rd.add_magnet(magnet_link)
            if "id" not in rd_resp:
                self.logger.error(f"Failed to add magnet: {rd_resp}")
                return None

            tid = rd_resp["id"]

            # 2. Create TorrentItem
            item = TorrentItem(magnet_link, source="manual", quick_download=quick_download)
            item.id = tid
            self.torrents[tid] = item

            # 3. Add to Queue Service
            self.queue_service.add_to_queue(item)

            self.logger.info(f"Added manual torrent {tid}")
            return tid

    def _on_external_torrent_found(self, torrent_info):
        """Callback when ExternalTorrentService finds a new torrent."""
        with self.lock:
            tid = torrent_info["id"]
            if tid in self.torrents:
                return

            # Create item
            item = TorrentItem(magnet_link="", source="external", quick_download=True)
            item.id = tid
            item.rd_filename = torrent_info["filename"]
            item.rd_status = torrent_info["status"]

            # Try to populate file list immediately if possible
            try:
                info = self.rd.get_torrent_info(tid)
                item.files = info.get("files", [])
            except:
                pass

            self.torrents[tid] = item
            self.queue_service.add_to_queue(item)
            self.logger.info(f"Successfully imported external torrent {tid}")

    def get_status(self):
        """Return a snapshot of all torrents for the UI."""
        with self.lock:
            manual_torrents = {}
            external_torrents = {}

            for tid, t in self.torrents.items():
                is_active = (self.queue_service.active_torrent_id == tid)

                # Get deletion info if applicable
                deletion_info = self.deletion_service.get_deletion_time(tid)

                torrent_data = {
                    "id": t.id,
                    "name": t.rd_filename or t.id,
                    "progress": t.progress,
                    "state": str(t.state).split(".")[-1],  # e.g. "WAITING_IN_QUEUE"
                    "files": t.files,
                    "selected_files": t.selected_files,
                    "rd_status": t.rd_status,
                    "queue_position": t.queue_position,
                    "is_active": is_active,
                    "error_message": t.error_message,
                    "deletion_in": deletion_info,
                    "tmdb_id": t.tmdb_id,  # Include metadata in status
                    "source": t.source
                }

                if t.source == "manual":
                    manual_torrents[tid] = torrent_data
                else:
                    external_torrents[tid] = torrent_data

            return {
                "manual": manual_torrents,
                "external": external_torrents
            }

    def _loop(self):
        """Main polling loop."""
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    # 1. Update queue positions
                    self.queue_service.update_queue_positions()

                    # 2. Get the active torrent (if any)
                    active_id = self.queue_service.active_torrent_id

                    # 3. Process all torrents (update status from RD)
                    # We create a copy of items to iterate safely
                    for tid, torrent in list(self.torrents.items()):
                        is_active = (tid == active_id)
                        self._update_torrent(tid, torrent, is_active)

            except Exception as e:
                self.logger.error(f"Error in manager loop: {e}")

            time.sleep(self.poll_interval)

    def _update_torrent(self, torrent_id: str, torrent: TorrentItem, is_active: bool):
        """Update a single torrent's state and trigger actions."""

        # If finished/failed/transferring, we don't need to poll RD constantly
        if torrent.state in [TorrentState.FINISHED, TorrentState.FAILED, TorrentState.TRANSFERRING_TO_MEDIA_SERVER]:
            return

        try:
            info = self.rd.get_torrent_info(torrent_id)
        except Exception as e:
            self.logger.warning(f"Failed to fetch info for {torrent_id}: {e}")
            return

        rd_status = info["status"]
        torrent.rd_status = rd_status
        torrent.rd_filename = info["filename"]
        torrent.files = info["files"]

        # Update progress if downloading on RD side
        if rd_status == "downloading":
            torrent.state = TorrentState.DOWNLOADING_ON_REALDEBRID
            torrent.progress = info.get("progress", 0)

        # Handle 'waiting_files_selection'
        if rd_status == "waiting_files_selection":
            if torrent.state != TorrentState.WAITING_FOR_SELECTION:
                torrent.state = TorrentState.WAITING_FOR_SELECTION

                # If quick_download is True, auto-select files
                if torrent.quick_download:
                    self.logger.info(f"Quick download enabled for {torrent_id}. Auto-selecting files...")
                    self._auto_select_files(torrent_id, torrent)

        # Handle 'downloaded' (Ready for download to local)
        elif rd_status in ["downloaded", "magnet_conversion_complete", "ready", "finished"]:
            torrent.rd_links = info.get("links", [])

            # Unrestrict links if not done yet
            if not torrent.direct_links and torrent.rd_links:
                self.logger.info(f"Unrestricting links for {torrent_id}...")
                try:
                    torrent.direct_links = self.rd.unrestrict_links(torrent.rd_links)
                except Exception as e:
                    torrent.error_message = f"Unrestrict failed: {e}"
                    torrent.state = TorrentState.FAILED
                    return

            # --- LOGIC UPDATE HERE ---
            # If we have direct links and this is the ACTIVE torrent, start download.
            # We do NOT check for tmdb_id here, allowing the download to proceed.
            if (is_active
                    and torrent.direct_links
                    and not torrent._download_started):
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                torrent._download_started = True

                self.logger.info(f"Starting download for active torrent {torrent.id} (TMDB: {torrent.tmdb_id})")

                self.fileops.start_download(
                    torrent,
                    config=self.config_data,
                    on_complete=self._on_download_complete,
                    on_progress=self.on_download_progress,
                    on_transfer=lambda tid=torrent.id: self.on_transfer_complete(tid)
                )

        # Handle error states from RD
        elif rd_status == "error":
            torrent.state = TorrentState.FAILED
            torrent.error_message = "Real-Debrid reported an error."

    def _auto_select_files(self, torrent_id, torrent):
        """Simple logic to select 'all' or specific video files."""
        # For now, just select 'all' to be safe, or you can implement logic to pick the biggest file
        try:
            self.rd.select_torrent_files(torrent_id, ["all"])
            torrent.selected_files = [f["name"] for f in torrent.files]
        except Exception as e:
            self.logger.error(f"Auto-selection failed for {torrent_id}: {e}")

    def on_download_progress(self, torrent_id, progress):
        """Callback for fileops progress."""
        if torrent_id in self.torrents:
            self.torrents[torrent_id].progress = progress

    def on_transfer_complete(self, torrent_id):
        """Callback when fileops starts moving files."""
        if torrent_id in self.torrents:
            self.torrents[torrent_id].state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER

    def _on_download_complete(self, torrent_id):
        """Callback when download is fully finished."""
        self.logger.info(f"Download complete for {torrent_id}")
        if torrent_id in self.torrents:
            # Mark as finished in queue service to rotate queue
            self.queue_service.mark_completed(torrent_id)

            # Schedule for deletion
            self.deletion_service.schedule_deletion(torrent_id)