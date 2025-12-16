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
        self.quick_download = quick_download  # Enable/disable automatic file selection
        self.state = TorrentState.SENT_TO_REALDEBRID
        self.last_update = time.time()
        self.direct_links = []
        # Backoff for unrestrict attempts
        self.progress = 0.0
        self.files = []
        self.unrestrict_backoff = 60  # seconds, start at 1 min
        self.next_unrestrict_at = 0  # epoch seconds
        # Download trigger flag (so FileOps starts only once)
        self._download_started = False
        self.selected_files = []
        self.custom_folder_name = None
        self.filename_strip_pattern = None  # Pattern to strip from start of filenames
        self.error_message = None  # Store error details
        self.deleted_from_realdebrid = False  # Track if deleted from RD
        self.completion_time = None  # Track when the torrent finished

    def schedule_unrestrict_retry(self):
        # exponential backoff with jitter, cap at 10 min
        self.unrestrict_backoff = min(int(self.unrestrict_backoff * 2), 600)
        jitter = random.randint(0, max(1, int(self.unrestrict_backoff * 0.1)))
        self.next_unrestrict_at = int(time.time()) + self.unrestrict_backoff + jitter

    def __repr__(self):
        return f"<TorrentItem id={self.id} source={self.source} state={self.state} quick_download={self.quick_download}>"


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

        # Initialize services (must be done after lock is created)
        self.queue_service = QueueService(self.torrents, self.lock, logger)
        self.deletion_service = DeletionService(rd_client, self.torrents, self.lock, logger, config_data)
        self.external_service = ExternalTorrentService(
            rd_client,
            self.torrents,
            self.lock,
            logger,
            config_data,
            time.time(),  # app_start_time
            self.queue_service  # Pass queue service so external torrents can be added to queue
        )

    # ------------------------------
    # Public Methods
    # ------------------------------
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

            # Mark as known and add to queue (both use their own locks)
            self.external_service.mark_as_known(torrent_id)
            queue_position = self.queue_service.add_to_queue(torrent_id)
            return torrent_id
        except RealDebridError as e:
            self.logger.error(f"Failed to add magnet: {e}")
            return None

    def get_status(self):
        """Return a snapshot of current torrent states with queue information, separated by source."""
        manual_torrents = {}
        external_torrents = {}

        with self.lock:
            torrent_items = list(self.torrents.items())

        # Process torrents outside the lock to avoid deadlock with service methods
        for tid, t in torrent_items:
            # Get queue position from service (uses its own lock)
            queue_position = self.queue_service.get_queue_position(tid)
            is_active = self.queue_service.is_active(tid)

            # Get deletion info from service (uses its own lock)
            deletion_info = self.deletion_service.get_deletion_time_remaining(tid)

            torrent_data = {
                "state": str(t.state),
                "progress": t.progress,
                "name": t.name or tid,
                "files": getattr(t, "files", []),
                "selected_files": t.selected_files if t.selected_files else [],
                "custom_folder_name": getattr(t, "custom_folder_name", None),
                "queue_position": queue_position,
                "is_active": is_active,
                "error_message": t.error_message,
                "deletion_in": deletion_info
            }

            if t.source == "external":
                external_torrents[tid] = torrent_data
            else:
                manual_torrents[tid] = torrent_data

        return {
            "manual": manual_torrents,
            "external": external_torrents
        }

    # ------------------------------
    # Internal Polling Loop
    # ------------------------------
    def _poll_loop(self):
        self.logger.info("Torrent polling loop started with queue system.")
        while not self.stop_event.is_set():
            try:
                self._process_queue()

                # Process pending deletions via service
                self.deletion_service.process_pending_deletions()

                # Scan for external torrents via service
                if self.external_service.should_scan():
                    self.external_service.scan_for_external_torrents()

            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")
            time.sleep(self.poll_interval)
        self.logger.info("Torrent polling loop stopped.")

    def _process_queue(self):
        """Process the queue - activate next torrent if needed and update all torrents up to AVAILABLE state."""
        # Activate next torrent if needed
        self.queue_service.get_next_active()

        # Clean up failed torrents from queue
        self.queue_service.clean_failed_from_queue()

        # Get all torrent IDs and their queue status
        with self.lock:
            torrent_items = list(self.torrents.items())

        # Update all torrents (outside of lock to avoid blocking)
        for torrent_id, torrent in torrent_items:
            is_active = self.queue_service.is_active(torrent_id)
            try:
                self._update_torrent(torrent_id, torrent, is_active)
            except Exception as e:
                self.logger.warning(f"Error updating torrent {torrent_id}: {e}")

    def _update_torrent(self, torrent_id: str, torrent: TorrentItem, is_active: bool):
        """Update a single torrent's state. Only active torrents can proceed to downloading."""
        now = int(time.time())

        # Skip updates for finished, failed, downloading, or transferring torrents
        # These states are terminal or managed by FileOps
        if torrent.state in (TorrentState.FINISHED, TorrentState.FAILED,
                             TorrentState.DOWNLOADING_FROM_REALDEBRID,
                             TorrentState.TRANSFERRING_TO_MEDIA_SERVER):
            self.logger.debug(f"{torrent_id}: Skipping update (state={torrent.state})")
            return

        # Skip updates if torrent was deleted from RealDebrid
        if torrent.deleted_from_realdebrid:
            self.logger.debug(f"{torrent_id}: Skipping update (deleted from RealDebrid)")
            return

        try:
            info = self.rd.get_torrent_info(torrent_id)
            rd_status = info.get("status", "").lower()

            self.logger.debug(f"{torrent_id}: RD status -> {rd_status} (active={is_active})")

            # --- waiting / converting / queued states ---
            if rd_status == "waiting_files_selection":
                # Skip for external torrents (they should have files auto-selected already)
                if torrent.source == "external":
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                else:
                    try:
                        files_info = self.rd.list_torrent_files(torrent_id)
                        torrent.files = [
                            {
                                "id": f["id"],
                                "name": f["path"].split("/")[-1],
                                "bytes": f["bytes"]
                            }
                            for f in files_info.get("files", [])
                        ]

                        # Try automatic file selection ONLY if quick_download is enabled
                        auto_selected = False
                        if torrent.quick_download:
                            auto_selected = self._try_auto_select_files(torrent_id, torrent)

                        if not auto_selected:
                            # Manual selection required
                            torrent.state = TorrentState.WAITING_FOR_SELECTION
                            if torrent.quick_download:
                                self.logger.info(
                                    f"{torrent_id}: waiting for file selection ({len(torrent.files)} file(s) found).")
                            else:
                                self.logger.info(
                                    f"{torrent_id}: waiting for file selection (Quick Download disabled, "
                                    f"{len(torrent.files)} file(s) found).")

                    except RealDebridError as e:
                        self.logger.warning(f"Failed to list files for {torrent_id}: {e}")
                        torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            elif rd_status == "magnet_error":
                # Invalid magnet link - mark as failed immediately
                torrent.state = TorrentState.FAILED
                torrent.error_message = "Invalid magnet link"
                self.logger.error(f"Torrent {torrent_id} failed: Invalid magnet link (magnet_error from RealDebrid)")
                # This will trigger removal from queue on next cycle

            elif rd_status in ["virus", "dead"]:
                # Torrent contains virus or is dead - mark as failed
                torrent.state = TorrentState.FAILED
                torrent.error_message = f"RealDebrid status: {rd_status}"
                self.logger.error(f"Torrent {torrent_id} failed: RealDebrid reported status '{rd_status}'")

            elif rd_status in ["magnet_conversion", "magnet_converting", "queued", "downloading"]:
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            # --- ready / finished states ---
            elif rd_status in ["downloaded", "magnet_conversion_complete", "ready", "finished"]:
                links = info.get("links", [])
                if not links:
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                else:
                    # Skip unrestrict if we already have direct links
                    if torrent.direct_links:
                        torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                    else:
                        # Respect backoff before attempting unrestrict
                        if torrent.next_unrestrict_at and now < torrent.next_unrestrict_at:
                            torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                        else:
                            self._attempt_unrestrict(torrent_id, torrent, links)

                    # CRITICAL: Only trigger FileOps if this torrent is ACTIVE
                    if (is_active
                            and torrent.state == TorrentState.AVAILABLE_FROM_REALDEBRID
                            and torrent.direct_links
                            and not torrent._download_started):
                        torrent._download_started = True
                        self.logger.info(f"Triggering FileOps for ACTIVE torrent {torrent.id}...")
                        self.fileops.start_download(
                            torrent,
                            config=self.config_data,
                            on_complete=self._on_download_complete,
                            on_progress=self.on_download_progress,
                            on_transfer=lambda tid=torrent.id: self.on_transfer_complete(tid)
                        )
                    elif not is_active and torrent.direct_links:
                        # Torrent is ready but waiting in queue
                        torrent.state = TorrentState.WAITING_IN_QUEUE
                        self.logger.debug(f"{torrent_id}: Ready but waiting in queue (not active)")

            elif rd_status == "error":
                torrent.state = TorrentState.FAILED
                torrent.error_message = "RealDebrid error status"
                self.logger.error(f"Torrent {torrent_id} failed with error status from RealDebrid")

            else:
                self.logger.debug(
                    f"{torrent_id}: Unhandled RD status '{rd_status}', leaving state as {torrent.state}.")

            torrent.last_update = time.time()

        except RealDebridError as e:
            # Check if it's a 404 error (torrent deleted)
            if "404" in str(e):
                self.logger.debug(
                    f"Torrent {torrent_id} not found in RealDebrid (likely deleted), skipping further updates")
                torrent.deleted_from_realdebrid = True
            else:
                self.logger.warning(f"Error updating torrent {torrent_id}: {e}")

    # ------------------------------
    # Helpers
    # ------------------------------
    def _try_auto_select_files(self, torrent_id: str, torrent: TorrentItem) -> bool:
        """
        Attempt automatic file selection for a torrent.

        Rules:
        1. If only one file total → auto-select it
        2. If multiple files but only one media file (.mkv/.mp4) → auto-select that media file

        Args:
            torrent_id: ID of the torrent
            torrent: TorrentItem instance

        Returns:
            True if automatic selection was made, False if manual selection required
        """
        files = torrent.files

        if not files:
            return False

        # Rule 1: Single file - auto-select
        if len(files) == 1:
            file_id = files[0]["id"]
            file_name = files[0]["name"]

            try:
                self.rd.select_torrent_files(torrent_id, [file_id])
                torrent.selected_files = [file_name]
                torrent.custom_folder_name = None  # Single file, no folder needed
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID

                self.logger.info(
                    f"{torrent_id}: Auto-selected single file '{file_name}'"
                )
                return True

            except RealDebridError as e:
                self.logger.warning(f"Failed to auto-select single file for {torrent_id}: {e}")
                return False

        # Rule 2: Multiple files, but only one media file
        media_extensions = {'.mkv', '.mp4'}
        media_files = [
            f for f in files
            if any(f["name"].lower().endswith(ext) for ext in media_extensions)
        ]

        if len(media_files) == 1:
            media_file = media_files[0]
            file_id = media_file["id"]
            file_name = media_file["name"]

            try:
                self.rd.select_torrent_files(torrent_id, [file_id])
                torrent.selected_files = [file_name]
                torrent.custom_folder_name = None  # Single file, no folder needed
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID

                self.logger.info(
                    f"{torrent_id}: Auto-selected single media file '{file_name}' "
                    f"from {len(files)} total files"
                )
                return True

            except RealDebridError as e:
                self.logger.warning(f"Failed to auto-select media file for {torrent_id}: {e}")
                return False

        # Multiple files and either zero or multiple media files - manual selection required
        self.logger.info(
            f"{torrent_id}: Manual selection required "
            f"({len(files)} files, {len(media_files)} media files)"
        )
        return False

    def _attempt_unrestrict(self, torrent_id: str, torrent: TorrentItem, links: list[str]):
        """Try to unrestrict RD links into direct download URLs, with logging/backoff."""
        self.logger.info(f"Generating direct links for torrent {torrent_id}...")
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
                msg = str(e).lower()
                if "hoster_unavailable" in msg or "503" in msg:
                    self.logger.info(
                        f"Hoster unavailable for torrent {torrent_id}. Will retry later."
                    )
                else:
                    # Non-hoster failure — log and allow next loop to retry
                    all_failed_hoster_unavailable = False
                    self.logger.warning(f"Unrestrict error for {torrent_id}: {e}")

        if direct_links:
            torrent.direct_links = direct_links
            torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
            torrent.unrestrict_backoff = 60
            torrent.next_unrestrict_at = 0
            self.logger.info(
                f"Torrent {torrent_id} available: {len(torrent.direct_links)} file(s) ready for download."
            )
        else:
            if all_failed_hoster_unavailable:
                torrent.schedule_unrestrict_retry()
                wait = torrent.next_unrestrict_at - int(time.time())
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
                self.logger.info(
                    f"Hoster unavailable for all links of {torrent_id}. Retrying unrestrict in ~{wait}s."
                )
            else:
                # keep available; another loop will try again
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID

    def _on_download_complete(self, torrent_id: str):
        """Called when a torrent completes (success or failure)."""
        self.logger.info(f"Torrent {torrent_id} completed FileOps cycle.")

        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if not torrent:
                return

        # Schedule deletion via service
        if self.config_data.get("delete_on_complete", True):
            self.deletion_service.schedule_deletion(torrent_id)

        # Clean up temporary download directory via service
        self.deletion_service.cleanup_temp_directory(torrent_id)

        # Remove from queue and clear active via service
        self.queue_service.remove_from_queue(torrent_id)
        if self.queue_service.is_active(torrent_id):
            self.queue_service.clear_active()

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