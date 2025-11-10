import threading
import time
import random
from typing import Dict, Optional
from collections import deque
from datetime import datetime

from core.states import TorrentState
from core.realdebrid import RealDebridClient, RealDebridError
from core.fileops import FileOps


class TorrentItem:
    """Represents one torrent in the queue."""

    def __init__(self, magnet_link: str, source: str = "manual"):
        self.magnet = magnet_link
        self.id = None
        self.name = None
        self.source = source  # "manual" or "external"
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
        self.error_message = None  # Store error details
        self.deleted_from_realdebrid = False  # Track if deleted from RD
        self.completion_time = None  # Track when the torrent finished

    def schedule_unrestrict_retry(self):
        # exponential backoff with jitter, cap at 10 min
        self.unrestrict_backoff = min(int(self.unrestrict_backoff * 2), 600)
        jitter = random.randint(0, max(1, int(self.unrestrict_backoff * 0.1)))
        self.next_unrestrict_at = int(time.time()) + self.unrestrict_backoff + jitter

    def __repr__(self):
        return f"<TorrentItem id={self.id} source={self.source} state={self.state}>"


class TorrentManager:
    def __init__(self, rd_client: RealDebridClient, logger, poll_interval: int, config_data: dict):
        self.rd = rd_client
        self.logger = logger
        self.poll_interval = poll_interval
        self.config_data = config_data

        self.fileops = FileOps(logger)

        self.running = False
        self.torrents: Dict[str, TorrentItem] = {}
        self.queue: deque = deque()  # Queue of torrent IDs to process
        self.active_torrent_id: Optional[str] = None  # Currently processing torrent
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

        # Delayed deletion
        self.deletion_delay_hours = config_data.get("deletion_delay_hours", 3)
        self.pending_deletions: Dict[str, float] = {}  # torrent_id -> deletion_time

        # External torrent scanning
        self.known_torrent_ids: set = set()
        self.app_start_time = time.time()
        self.external_scan_interval = config_data.get("external_torrent_scan_interval_seconds", 15)
        if self.external_scan_interval is None:
            self.external_scan_interval = 0
        self.external_scan_interval = int(self.external_scan_interval) if self.external_scan_interval > 0 else 0
        self.external_scan_enabled = self.external_scan_interval > 0
        self.last_external_scan = 0

        if self.external_scan_enabled:
            self.logger.info(f"External torrent scanning enabled (interval: {self.external_scan_interval}s)")
        else:
            self.logger.info("External torrent scanning disabled")

        self.logger.info(f"Delayed deletion enabled: {self.deletion_delay_hours} hours after completion")

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

    def add_magnet(self, magnet_link: str):
        """Add a new magnet link to RealDebrid and queue."""
        try:
            self.logger.info(f"Adding magnet link: {magnet_link[:60]}...")
            result = self.rd.add_magnet(magnet_link)
            torrent_id = result.get("id")
            if not torrent_id:
                raise RealDebridError("No torrent ID returned from RealDebrid.")
            item = TorrentItem(magnet_link, source="manual")
            item.id = torrent_id
            item.state = TorrentState.WAITING_FOR_REALDEBRID
            with self.lock:
                self.torrents[torrent_id] = item
                self.queue.append(torrent_id)
                self.known_torrent_ids.add(torrent_id)
                queue_position = len(self.queue)
            self.logger.info(f"Added torrent {torrent_id} to queue at position {queue_position}.")
            return torrent_id
        except RealDebridError as e:
            self.logger.error(f"Failed to add magnet: {e}")
            return None

    def get_status(self):
        """Return a snapshot of current torrent states with queue information, separated by source."""
        with self.lock:
            manual_torrents = {}
            external_torrents = {}

            for tid, t in self.torrents.items():
                # Calculate queue position
                queue_position = None
                if tid in self.queue:
                    queue_position = list(self.queue).index(tid) + 1

                is_active = (tid == self.active_torrent_id)

                # Calculate time until deletion if scheduled
                deletion_info = None
                if tid in self.pending_deletions:
                    deletion_time = self.pending_deletions[tid]
                    time_remaining = deletion_time - time.time()
                    if time_remaining > 0:
                        hours = int(time_remaining // 3600)
                        minutes = int((time_remaining % 3600) // 60)
                        deletion_info = f"{hours}h {minutes}m"

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

                # Process pending deletions
                self._process_pending_deletions()

                # Scan for external torrents if enabled and interval elapsed
                if self.external_scan_enabled:
                    now = time.time()
                    if now - self.last_external_scan >= self.external_scan_interval:
                        self._scan_external_torrents()
                        self.last_external_scan = now

            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")
            time.sleep(self.poll_interval)
        self.logger.info("Torrent polling loop stopped.")

    def _process_pending_deletions(self):
        """Check for torrents that are ready to be deleted from RealDebrid."""
        now = time.time()
        to_delete = []

        with self.lock:
            for torrent_id, deletion_time in list(self.pending_deletions.items()):
                if now >= deletion_time:
                    to_delete.append(torrent_id)

        for torrent_id in to_delete:
            try:
                self.rd.delete_torrent(torrent_id)
                self.logger.info(f"Deleted torrent {torrent_id} from RealDebrid (delayed deletion)")

                with self.lock:
                    # Mark as deleted so we stop trying to poll it
                    if torrent_id in self.torrents:
                        self.torrents[torrent_id].deleted_from_realdebrid = True
                    # Remove from pending deletions
                    self.pending_deletions.pop(torrent_id, None)

            except Exception as e:
                self.logger.warning(f"Failed to delete torrent {torrent_id}: {e}")
                # Remove from pending anyway to avoid retry loops
                with self.lock:
                    self.pending_deletions.pop(torrent_id, None)

    def _process_queue(self):
        """Process the queue - activate next torrent if needed and update all torrents up to AVAILABLE state."""
        with self.lock:
            # Check if we need to activate the next torrent
            if self.active_torrent_id is None and len(self.queue) > 0:
                self.active_torrent_id = self.queue[0]
                self.logger.info(
                    f"Activating torrent {self.active_torrent_id} from queue (position 1 of {len(self.queue)})")

            # Clean up failed torrents from queue
            failed_torrents = [
                tid for tid, t in self.torrents.items()
                if t.state == TorrentState.FAILED and tid in self.queue
            ]
            for tid in failed_torrents:
                self.queue.remove(tid)
                self.logger.info(f"Removed failed torrent {tid} from queue. {len(self.queue)} remaining.")
                # If it was active, clear it
                if self.active_torrent_id == tid:
                    self.active_torrent_id = None
                    self.logger.info("Cleared active torrent (was failed), next will activate on next cycle.")

            # Get all torrent IDs and their queue status
            torrent_items = list(self.torrents.items())

        # Update all torrents (outside of lock to avoid blocking)
        for torrent_id, torrent in torrent_items:
            is_active = (torrent_id == self.active_torrent_id)
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
                        torrent.state = TorrentState.WAITING_FOR_SELECTION
                        self.logger.info(
                            f"{torrent_id}: waiting for file selection ({len(torrent.files)} file(s) found).")
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
    # External Torrent Scanning
    # ------------------------------
    def _scan_external_torrents(self):
        """Scan RealDebrid for externally-added torrents and auto-select files."""
        try:
            self.logger.debug("Scanning for external torrents...")
            torrents = self.rd.list_torrents()

            if not isinstance(torrents, list):
                self.logger.warning(f"Unexpected response from list_torrents: {type(torrents)}")
                return

            new_count = 0
            for t in torrents:
                torrent_id = t.get("id")
                if not torrent_id:
                    continue

                # Skip if already known
                if torrent_id in self.known_torrent_ids:
                    continue

                # Parse added timestamp
                added_str = t.get("added")
                if not added_str:
                    continue

                try:
                    # Parse ISO format: "2025-10-25T05:00:51.000Z"
                    added_dt = datetime.fromisoformat(added_str.replace('Z', '+00:00'))
                    added_ts = added_dt.timestamp()
                except Exception as e:
                    self.logger.debug(f"Failed to parse timestamp '{added_str}': {e}")
                    continue

                # Skip if added before Pullr started
                if added_ts < self.app_start_time:
                    self.known_torrent_ids.add(torrent_id)
                    continue

                # Process torrents in either waiting_files_selection OR already downloaded status
                status = t.get("status", "").lower()
                if status not in ["waiting_files_selection", "downloaded"]:
                    continue

                # Found a new external torrent!
                filename = t.get("filename", torrent_id)
                self.logger.info(f"Detected external torrent: {filename} ({torrent_id}) [status: {status}]")

                try:
                    # Auto-select all files (only needed if waiting for selection)
                    if status == "waiting_files_selection":
                        self.rd.select_files(torrent_id, "all")
                        self.logger.info(f"Auto-selected all files for external torrent {torrent_id}")
                    else:
                        self.logger.info(f"External torrent {torrent_id} already downloaded, skipping file selection")

                    # Create TorrentItem
                    item = TorrentItem(magnet_link="", source="external")
                    item.id = torrent_id
                    item.name = filename
                    item.custom_folder_name = filename  # Use RD's filename as folder name
                    item.state = TorrentState.WAITING_FOR_REALDEBRID

                    with self.lock:
                        self.torrents[torrent_id] = item
                        self.queue.append(torrent_id)
                        self.known_torrent_ids.add(torrent_id)

                    new_count += 1

                except RealDebridError as e:
                    self.logger.warning(f"Failed to process external torrent {torrent_id}: {e}")
                    # Still mark as known to avoid retry loops, but show as failed
                    item = TorrentItem(magnet_link="", source="external")
                    item.id = torrent_id
                    item.name = filename
                    item.state = TorrentState.FAILED
                    item.error_message = f"Processing failed: {str(e)}"

                    with self.lock:
                        self.torrents[torrent_id] = item
                        self.known_torrent_ids.add(torrent_id)

            if new_count > 0:
                self.logger.info(f"External torrent scan completed: {new_count} new torrent(s) added")

        except Exception as e:
            self.logger.warning(f"External torrent scan error: {e}")

    # ------------------------------
    # Helpers
    # ------------------------------
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

        # Schedule deletion if enabled
        if self.config_data.get("delete_on_complete", True):
            deletion_time = time.time() + (self.deletion_delay_hours * 3600)
            with self.lock:
                self.pending_deletions[torrent_id] = deletion_time
                torrent.completion_time = time.time()

            self.logger.info(
                f"Scheduled deletion of torrent {torrent_id} from RealDebrid in {self.deletion_delay_hours} hours"
            )

        # Clean up temporary download directory
        try:
            from pathlib import Path
            import shutil
            temp_path = Path(self.config_data.get("download_temp_path", "/downloads")) / torrent_id
            if temp_path.exists():
                shutil.rmtree(temp_path)
                self.logger.info(f"Deleted temporary files for {torrent_id}")
        except Exception as e:
            self.logger.warning(f"Failed to delete temp files for {torrent_id}: {e}")

        # Move to next torrent in queue
        with self.lock:
            # Remove from queue if present
            if torrent_id in self.queue:
                self.queue.remove(torrent_id)
                self.logger.info(f"Removed {torrent_id} from queue. {len(self.queue)} torrent(s) remaining.")

            # Clear active torrent
            if self.active_torrent_id == torrent_id:
                self.active_torrent_id = None
                self.logger.info("Cleared active torrent, next torrent will be activated in next poll cycle.")

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