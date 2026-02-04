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

        # Initialize Services
        self.queue_service = QueueService(self.torrents, self.lock, logger)

        # Deletion Service (No start/stop needed based on your code)
        self.deletion_service = DeletionService(rd_client, self.torrents, self.lock, logger, config_data)

        # External Service
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

        # NOTE: deletion_service and external_service do not need explicit start()
        # calls as their logic is invoked within the poll loop or handled internally.

    def stop(self):
        """Stops the polling loop cleanly."""
        if not self.running:
            return
        self.logger.info("Stopping TorrentManager polling thread...")
        self.running = False
        self.stop_event.set()

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

            # Mark as known to prevent re-import by external service
            self.external_service.mark_as_known(torrent_id)

            # Add to queue (passing ID to match external service usage)
            self.queue_service.add_to_queue(torrent_id)

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
                "id": t.id,
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
                "tmdb_id": t.tmdb_id,  # Return tmdb_id to UI
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
                # 1. Process Queue
                self._process_queue()

                # 2. Process Deletions
                self.deletion_service.process_pending_deletions()

                # 3. Scan for External Torrents
                if self.external_service.should_scan():
                    # No arguments needed; service updates shared torrents dict directly
                    self.external_service.scan_for_external_torrents()

            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")

            time.sleep(self.poll_interval)
        self.logger.info("Torrent polling loop stopped.")

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
            torrent.name = info.get("filename", torrent.name)

            # --- waiting / converting / queued states ---
            if rd_status == "waiting_files_selection":
                if torrent.source == "external":
                    # External torrents are auto-selected by the external service
                    # or RD logic, but we update state here.
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
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

                    # --- UNBLOCKING LOGIC ---
                    # Check if Active + Links + Not Started.
                    # We do NOT check for tmdb_id here, allowing the download to proceed.
                    # FileOps will route to "Unsorted" if metadata is missing.
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

    # 1. Update the _on_download_complete method
    def _on_download_complete(self, torrent_id: str):
        """Called when download is finished."""
        self.logger.info(f"Torrent {torrent_id} download finished.")

        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if not torrent:
                return

            # Check if we are missing metadata
            if not torrent.tmdb_id:
                self.logger.info(f"Torrent {torrent_id} finished but has NO metadata.")
                self.logger.info("Keeping in 'Unsorted' state. Add metadata via UI to finalize.")

                torrent.state = TorrentState.WAITING_FOR_METADATA

                # We still mark it as 'completed' in the QueueService so the NEXT download can start
                self.queue_service.mark_completed(torrent_id)

                # We do NOT schedule deletion yet because we want you to manage it
                return

            # If we DO have metadata, standard cleanup
            torrent.state = TorrentState.FINISHED

            # Schedule deletion from RD
            if self.config_data.get("delete_on_complete", True):
                self.deletion_service.schedule_deletion(torrent_id)

            self.deletion_service.cleanup_temp_directory(torrent_id)
            self.queue_service.mark_completed(torrent_id)

    # 2. Add this NEW method to TorrentManager class
    def retry_import_with_metadata(self, torrent_id: str):
        """Called when user adds metadata to an Unsorted torrent."""
        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if not torrent: return

            if torrent.state == TorrentState.WAITING_FOR_METADATA:
                self.logger.info(f"Metadata added for {torrent_id}. Moving from Unsorted -> Library.")

                # Run the move in a background thread to avoid blocking web request
                threading.Thread(target=self._run_move_job, args=(torrent,), daemon=True).start()

    def _run_move_job(self, torrent):
        """Background job to move files."""
        success = self.fileops.move_from_unsorted(torrent, self.config_data)

        if success:
            with self.lock:
                torrent.state = TorrentState.FINISHED
                self.logger.info(f"Import complete for {torrent.id}.")
                # Now we can schedule cleanup
                self.deletion_service.schedule_deletion(torrent.id)
        else:
            self.logger.error(f"Failed to move {torrent.id} from unsorted.")

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