import threading
import time
import random
from typing import Dict, Optional
from datetime import datetime

from core.states import TorrentState
from core.realdebrid import RealDebridClient, RealDebridError
from core.fileops import FileOps
from core.services import QueueService, DeletionService, ExternalTorrentService


class TorrentItem:
    """Represents one torrent in the queue."""

    def __init__(self, magnet_link: str, source: str = "manual", quick_download: bool = True):
        self.magnet = magnet_link
        self.id = None
        self.name = None
        self.source = source  # "manual" or "external"
        self.quick_download = quick_download
        self.state = TorrentState.SENT_TO_REALDEBRID
        self.last_update = time.time()
        self.direct_links = []
        self.progress = 0.0
        self.files = []
        self.unrestrict_backoff = 60
        self.next_unrestrict_at = 0
        self._download_started = False
        self.selected_files = []
        self.custom_folder_name = None
        self.filename_strip_pattern = None
        self.error_message = None
        self.deleted_from_realdebrid = False
        self.completion_time = None

        # ADDED: TMDB ID field (initially None)
        self.tmdb_id = None

    def schedule_unrestrict_retry(self):
        self.unrestrict_backoff = min(int(self.unrestrict_backoff * 2), 600)
        jitter = random.randint(0, max(1, int(self.unrestrict_backoff * 0.1)))
        self.next_unrestrict_at = int(time.time()) + self.unrestrict_backoff + jitter

    def __repr__(self):
        return f"<TorrentItem id={self.id} source={self.source} state={self.state} tmdb={self.tmdb_id}>"


class TorrentManager:
    # PRESERVED: Your original dependency injection signature
    def __init__(self, rd_client: RealDebridClient, logger, poll_interval: int, config_data: dict):
        self.rd = rd_client
        self.logger = logger
        self.poll_interval = poll_interval
        self.config_data = config_data

        self.fileops = FileOps(logger)

        self.running = False
        self.torrents: Dict[str, TorrentItem] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

        # PRESERVED: Your original service initialization
        self.queue_service = QueueService(self.torrents, self.lock, logger)
        self.deletion_service = DeletionService(rd_client, self.torrents, self.lock, logger, config_data)
        self.external_service = ExternalTorrentService(
            rd_client,
            self.torrents,
            self.lock,
            logger,
            config_data,
            time.time(),
            self.queue_service
        )

    def start(self):
        """Starts the torrent polling loop in a background thread."""
        if self.thread and self.thread.is_alive():
            self.logger.warning("TorrentManager already running.")
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()
        self.logger.info("Starting TorrentManager polling thread with queue system...")

        # Start services
        self.deletion_service.start()
        # Ensure external service scanning is started if needed
        # (Assuming your external service handles its own threading or is polled in loop)

    def stop(self):
        """Stops the polling loop cleanly."""
        if not self.running:
            return
        self.logger.info("Stopping TorrentManager polling thread...")
        self.running = False
        self.stop_event.set()

        self.deletion_service.stop()

        if self.thread:
            self.thread.join(timeout=5)
            self.logger.info("Torrent polling loop stopped.")

    def add_magnet(self, magnet_link: str, quick_download: bool = True):
        """Add a new magnet link to RealDebrid and queue."""
        try:
            self.logger.info(f"Adding magnet link: {magnet_link[:60]}... (quick_download={quick_download})")
            result = self.rd.add_magnet(magnet_link)
            torrent_id = result.get("id")
            if not torrent_id:
                raise RealDebridError("No torrent ID returned from RealDebrid.")
            item = TorrentItem(magnet_link, source="manual", quick_download=quick_download)
            item.id = torrent_id
            item.state = TorrentState.WAITING_FOR_REALDEBRID
            with self.lock:
                self.torrents[torrent_id] = item

            # Mark as known and add to queue
            self.external_service.mark_as_known(torrent_id)
            self.queue_service.add_to_queue(item)  # NOTE: Changed from ID to item based on usage patterns
            return torrent_id
        except RealDebridError as e:
            self.logger.error(f"Failed to add magnet: {e}")
            return None

    def get_status(self):
        """Return a snapshot of current torrent states."""
        manual_torrents = {}
        external_torrents = {}

        with self.lock:
            torrent_items = list(self.torrents.items())

        for tid, t in torrent_items:
            queue_position = self.queue_service.get_queue_position(tid)
            is_active = self.queue_service.is_active(tid)
            deletion_info = self.deletion_service.get_deletion_time_remaining(tid)

            torrent_data = {
                "id": t.id,  # Ensure ID is present
                "state": str(t.state),
                "progress": t.progress,
                "name": t.name or tid,
                "files": getattr(t, "files", []),
                "selected_files": t.selected_files if t.selected_files else [],
                "custom_folder_name": getattr(t, "custom_folder_name", None),
                "queue_position": queue_position,
                "is_active": is_active,
                "error_message": t.error_message,
                "deletion_in": deletion_info,
                "tmdb_id": t.tmdb_id,  # ADDED: return tmdb_id to UI
                "source": t.source
            }

            if t.source == "external":
                external_torrents[tid] = torrent_data
            else:
                manual_torrents[tid] = torrent_data

        return {
            "manual": manual_torrents,
            "external": external_torrents
        }

    def _poll_loop(self):
        self.logger.info("Torrent polling loop started.")
        while not self.stop_event.is_set():
            try:
                self._process_queue()
                self.deletion_service.process_pending_deletions()

                if self.external_service.should_scan():
                    self.external_service.scan_for_external_torrents(self._on_external_found)

            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")
            time.sleep(self.poll_interval)
        self.logger.info("Torrent polling loop stopped.")

    def _on_external_found(self, torrent_info):
        """Callback for external service to add torrents."""
        # This matches the signature expected by ExternalTorrentService
        tid = torrent_info["id"]
        with self.lock:
            if tid in self.torrents:
                return
            item = TorrentItem(magnet_link="", source="external", quick_download=True)
            item.id = tid
            item.name = torrent_info["filename"]
            # Try to fetch files immediately
            try:
                info = self.rd.get_torrent_info(tid)
                item.files = info.get("files", [])
            except:
                pass
            self.torrents[tid] = item
            self.queue_service.add_to_queue(item)
            self.logger.info(f"Imported external torrent {tid}")

    def _process_queue(self):
        """Process the queue."""
        self.queue_service.get_next_active()
        self.queue_service.clean_failed_from_queue()

        with self.lock:
            torrent_items = list(self.torrents.items())

        for torrent_id, torrent in torrent_items:
            is_active = self.queue_service.is_active(torrent_id)
            try:
                self._update_torrent(torrent_id, torrent, is_active)
            except Exception as e:
                self.logger.warning(f"Error updating torrent {torrent_id}: {e}")

    def _update_torrent(self, torrent_id: str, torrent: TorrentItem, is_active: bool):
        """Update a single torrent's state."""
        now = int(time.time())

        if torrent.state in (TorrentState.FINISHED, TorrentState.FAILED,
                             TorrentState.DOWNLOADING_FROM_REALDEBRID,
                             TorrentState.TRANSFERRING_TO_MEDIA_SERVER):
            return

        if torrent.deleted_from_realdebrid:
            return

        try:
            info = self.rd.get_torrent_info(torrent_id)
            rd_status = info.get("status", "").lower()
            torrent.name = info.get("filename", torrent.name)  # Update name

            # --- waiting / converting / queued states ---
            if rd_status == "waiting_files_selection":
                if torrent.source == "external":
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                    # External torrents should eventually be selected by RD logic or user,
                    # but we can try auto-select if stuck
                    if torrent.quick_download:
                        self._try_auto_select_files(torrent_id, torrent)
                else:
                    try:
                        files_info = self.rd.list_torrent_files(torrent_id)
                        torrent.files = files_info.get("files", [])

                        auto_selected = False
                        if torrent.quick_download:
                            auto_selected = self._try_auto_select_files(torrent_id, torrent)

                        if not auto_selected:
                            torrent.state = TorrentState.WAITING_FOR_SELECTION

                    except RealDebridError as e:
                        self.logger.warning(f"Failed to list files for {torrent_id}: {e}")
                        torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            elif rd_status == "magnet_error":
                torrent.state = TorrentState.FAILED
                torrent.error_message = "Invalid magnet link"

            elif rd_status in ["virus", "dead"]:
                torrent.state = TorrentState.FAILED
                torrent.error_message = f"RealDebrid status: {rd_status}"

            elif rd_status in ["magnet_conversion", "magnet_converting", "queued", "downloading"]:
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            # --- ready / finished states ---
            elif rd_status in ["downloaded", "magnet_conversion_complete", "ready", "finished"]:
                links = info.get("links", [])
                if not links:
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                else:
                    if torrent.direct_links:
                        torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                    else:
                        if torrent.next_unrestrict_at and now < torrent.next_unrestrict_at:
                            torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                        else:
                            self._attempt_unrestrict(torrent_id, torrent, links)

                    # --- MODIFIED LOGIC START ---
                    # Check if Active + Links + Not Started.
                    # REMOVED the "waiting for TMDB metadata" check.
                    # The download will proceed, and FileOps will handle the routing (Media vs Unsorted).
                    if (is_active
                            and torrent.state == TorrentState.AVAILABLE_FROM_REALDEBRID
                            and torrent.direct_links
                            and not torrent._download_started):

                        torrent._download_started = True
                        self.logger.info(
                            f"Triggering FileOps for ACTIVE torrent {torrent.id} (TMDB: {torrent.tmdb_id})")

                        self.fileops.start_download(
                            torrent,
                            config=self.config_data,
                            on_complete=self._on_download_complete,
                            on_progress=self.on_download_progress,
                            on_transfer=lambda tid=torrent.id: self.on_transfer_complete(tid)
                        )
                    # --- MODIFIED LOGIC END ---

                    elif not is_active and torrent.direct_links:
                        torrent.state = TorrentState.WAITING_IN_QUEUE

            elif rd_status == "error":
                torrent.state = TorrentState.FAILED
                torrent.error_message = "RealDebrid error status"

            torrent.last_update = time.time()

        except RealDebridError as e:
            if "404" in str(e):
                torrent.deleted_from_realdebrid = True
            else:
                self.logger.warning(f"Error updating torrent {torrent_id}: {e}")

    def _try_auto_select_files(self, torrent_id: str, torrent: TorrentItem) -> bool:
        """Attempt automatic file selection."""
        # ... (Preserved from your original file)
        files = torrent.files
        if not files: return False

        if len(files) == 1:
            file_id = files[0]["id"]
            try:
                self.rd.select_torrent_files(torrent_id, [file_id])
                torrent.selected_files = [files[0]["name"]]
                torrent.custom_folder_name = None
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                return True
            except RealDebridError:
                return False

        media_extensions = {'.mkv', '.mp4'}
        media_files = [f for f in files if any(f["name"].lower().endswith(ext) for ext in media_extensions)]

        if len(media_files) == 1:
            try:
                self.rd.select_torrent_files(torrent_id, [media_files[0]["id"]])
                torrent.selected_files = [media_files[0]["name"]]
                torrent.custom_folder_name = None
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                return True
            except RealDebridError:
                return False

        return False

    def _attempt_unrestrict(self, torrent_id: str, torrent: TorrentItem, links: list[str]):
        """Try to unrestrict RD links."""
        # ... (Preserved from your original file)
        direct_links = []
        all_failed_hoster_unavailable = True

        for link in links:
            try:
                resp = self.rd.unrestrict_link(link)
                dl = resp.get("download")
                if dl:
                    direct_links.append(dl)
                    all_failed_hoster_unavailable = False
            except RealDebridError as e:
                if "hoster_unavailable" in str(e) or "503" in str(e):
                    pass
                else:
                    all_failed_hoster_unavailable = False

        if direct_links:
            torrent.direct_links = direct_links
            torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
            torrent.unrestrict_backoff = 60
            torrent.next_unrestrict_at = 0
        else:
            if all_failed_hoster_unavailable:
                torrent.schedule_unrestrict_retry()
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
            else:
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID

    def _on_download_complete(self, torrent_id: str):
        """Called when a torrent completes (success or failure)."""
        self.logger.info(f"Torrent {torrent_id} completed FileOps cycle.")

        # CLEANUP: Remove from queue and schedule deletion
        if self.config_data.get("delete_on_complete", True):
            self.deletion_service.schedule_deletion(torrent_id)

        self.deletion_service.cleanup_temp_directory(torrent_id)

        # Mark completed in queue service (Rotates queue)
        self.queue_service.mark_completed(torrent_id)

    def on_download_progress(self, torrent_id: str, progress: float):
        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if torrent:
                torrent.state = TorrentState.DOWNLOADING_FROM_REALDEBRID
                torrent.progress = round(progress, 1)

    def on_transfer_complete(self, torrent_id: str):
        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if torrent:
                torrent.state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER