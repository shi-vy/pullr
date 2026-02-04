import threading
import time
import random
from typing import Dict, Optional
from datetime import datetime

from core.states import TorrentState
from core.realdebrid import RealDebridClient, RealDebridError
from core.fileops import FileOps
from core.services import QueueService, DeletionService, ExternalTorrentService
from core.services.metadata_service import MetadataService


class TorrentItem:
    """Represents one torrent in the queue."""

    def __init__(self, magnet_link: str, source: str = "manual", quick_download: bool = True):
        self.magnet = magnet_link
        self.id = None
        self.name = None
        self.source = source
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

        # Jellyfin Metadata
        self.tmdb_id = None
        self.media_type = None  # 'movie' or 'tv'
        self.canonical_title = None

    def schedule_unrestrict_retry(self):
        self.unrestrict_backoff = min(int(self.unrestrict_backoff * 2), 600)
        jitter = random.randint(0, max(1, int(self.unrestrict_backoff * 0.1)))
        self.next_unrestrict_at = int(time.time()) + self.unrestrict_backoff + jitter

    def __repr__(self):
        return f"<TorrentItem id={self.id} state={self.state} tmdb={self.tmdb_id}>"

    @property
    def download_started(self):
        return self._download_started


class TorrentManager:
    def __init__(self, rd_client: RealDebridClient, logger, poll_interval: int, config_data: dict):
        self.rd = rd_client
        self.logger = logger
        self.poll_interval = poll_interval
        self.config_data = config_data

        self.fileops = FileOps(logger)
        self.metadata_service = None

        # Initialize MetadataService if Jellyfin mode is enabled
        if self.config_data.get("jellyfin_mode"):
            try:
                self.metadata_service = MetadataService(
                    self.config_data.get("tmdb_api_key"),
                    logger
                )
                self.logger.info("Jellyfin Mode enabled: MetadataService initialized.")
            except Exception as e:
                self.logger.error(f"Failed to initialize MetadataService: {e}")

        self.running = False
        self.torrents: Dict[str, TorrentItem] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

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

    # ------------------------------
    # Public Methods
    # ------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            self.logger.warning("TorrentManager already running.")
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()
        self.logger.info("Starting TorrentManager polling thread...")

    def stop(self):
        if not self.running:
            return
        self.logger.info("Stopping TorrentManager polling thread...")
        self.running = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            self.logger.info("Torrent polling loop stopped.")

    def add_magnet(self, magnet_link: str, quick_download: bool = True, tmdb_id: str = None, media_type: str = None):
        """Add a new magnet link to RealDebrid and queue."""
        try:
            self.logger.info(f"Adding magnet link: {magnet_link[:60]}... (quick={quick_download})")
            result = self.rd.add_magnet(magnet_link)
            torrent_id = result.get("id")
            if not torrent_id:
                raise RealDebridError("No torrent ID returned from RealDebrid.")

            item = TorrentItem(magnet_link, source="manual", quick_download=quick_download)
            item.id = torrent_id
            item.state = TorrentState.WAITING_FOR_REALDEBRID

            # Set initial metadata if provided
            if tmdb_id:
                item.tmdb_id = tmdb_id.strip()
            if media_type:
                item.media_type = media_type.strip().lower()

            with self.lock:
                self.torrents[torrent_id] = item

            self.external_service.mark_as_known(torrent_id)
            self.queue_service.add_to_queue(torrent_id)
            return torrent_id
        except RealDebridError as e:
            self.logger.error(f"Failed to add magnet: {e}")
            return None

    def get_status(self):
        manual_torrents = {}
        external_torrents = {}

        with self.lock:
            torrent_items = list(self.torrents.items())

        for tid, t in torrent_items:
            queue_position = self.queue_service.get_queue_position(tid)
            is_active = self.queue_service.is_active(tid)
            deletion_info = self.deletion_service.get_deletion_time_remaining(tid)

            torrent_data = {
                "state": str(t.state),
                "progress": t.progress,
                "name": t.name or tid,
                "files": getattr(t, "files", []),
                "selected_files": t.selected_files if t.selected_files else [],
                "queue_position": queue_position,
                "is_active": is_active,
                "error_message": t.error_message,
                "deletion_in": deletion_info,
                # Metadata fields
                "tmdb_id": t.tmdb_id,
                "media_type": t.media_type,
                "canonical_title": t.canonical_title,
                "needs_metadata": (self.config_data.get("jellyfin_mode") and not t.tmdb_id)
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
        self.logger.info("Torrent polling loop started.")
        while not self.stop_event.is_set():
            try:
                self._process_queue()
                self.deletion_service.process_pending_deletions()
                if self.external_service.should_scan():
                    self.external_service.scan_for_external_torrents()
            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")
            time.sleep(self.poll_interval)

    def _process_queue(self):
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

            if rd_status == "waiting_files_selection":
                # ... [Existing selection logic] ...
                jellyfin_enabled = self.config_data.get("jellyfin_mode", False)
                metadata_ready = True

                if jellyfin_enabled:
                    if not torrent.tmdb_id and self.metadata_service:
                        cached = self.metadata_service.get_cached_show_id(torrent.name)
                        if cached:
                            torrent.tmdb_id, torrent.media_type = cached
                            self.logger.info(f"{torrent_id}: Auto-resolved from cache -> ID {torrent.tmdb_id}")

                    if not torrent.tmdb_id:
                        metadata_ready = False

                    if torrent.tmdb_id and not torrent.canonical_title:
                        title = self.metadata_service.fetch_metadata(torrent.tmdb_id, torrent.media_type or "tv")
                        if title:
                            torrent.canonical_title = title
                            if torrent.media_type == "tv":
                                self.metadata_service.update_cache(torrent.name, torrent.tmdb_id)
                        else:
                            torrent.state = TorrentState.FAILED
                            torrent.error_message = "Failed to fetch TMDB metadata"
                            return

                # ... [Rest of selection logic] ...
                if torrent.source == "external":
                    torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                else:
                    try:
                        files_info = self.rd.list_torrent_files(torrent_id)
                        torrent.files = [
                            {"id": f["id"], "name": f["path"].split("/")[-1], "bytes": f["bytes"]}
                            for f in files_info.get("files", [])
                        ]

                        auto_selected = False
                        if torrent.quick_download and metadata_ready:
                            auto_selected = self._try_auto_select_files(torrent_id, torrent)

                        if not auto_selected:
                            torrent.state = TorrentState.WAITING_FOR_SELECTION
                            if jellyfin_enabled and not metadata_ready:
                                self.logger.info(f"{torrent_id}: waiting for metadata (TMDB ID required).")
                            else:
                                self.logger.info(f"{torrent_id}: waiting for file selection.")

                    except RealDebridError as e:
                        self.logger.warning(f"Failed to list files: {e}")
                        torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            elif rd_status == "magnet_error":
                torrent.state = TorrentState.FAILED
                torrent.error_message = "Invalid magnet link"

            elif rd_status in ["virus", "dead"]:
                torrent.state = TorrentState.FAILED
                torrent.error_message = f"RealDebrid status: {rd_status}"

            elif rd_status in ["magnet_conversion", "magnet_converting", "queued", "downloading"]:
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID

            elif rd_status in ["downloaded", "magnet_conversion_complete", "ready", "finished"]:
                # --- JELLYFIN MODE: Late Metadata Resolution ---
                if self.config_data.get("jellyfin_mode", False):
                    # If metadata is missing for a ready torrent (e.g. external), try cache
                    if not torrent.tmdb_id and self.metadata_service:
                        cached = self.metadata_service.get_cached_show_id(torrent.name)
                        if cached:
                            torrent.tmdb_id, torrent.media_type = cached
                            self.logger.info(
                                f"{torrent_id}: Auto-resolved from cache (Late Bind) -> ID {torrent.tmdb_id}")

                    if not torrent.tmdb_id:
                        # Still waiting for metadata
                        torrent.state = TorrentState.WAITING_FOR_SELECTION
                        self.logger.info(f"{torrent_id}: RD ready, but waiting for TMDB metadata.")
                        return

                    if not torrent.canonical_title:
                        title = self.metadata_service.fetch_metadata(torrent.tmdb_id, torrent.media_type or "tv")
                        if title:
                            torrent.canonical_title = title
                            if torrent.media_type == "tv":
                                self.metadata_service.update_cache(torrent.name, torrent.tmdb_id)
                        else:
                            torrent.state = TorrentState.FAILED
                            torrent.error_message = "Failed to fetch TMDB metadata"
                            return
                # -----------------------------------------------

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

                    if (is_active
                            and torrent.state == TorrentState.AVAILABLE_FROM_REALDEBRID
                            and torrent.direct_links
                            and not torrent.download_started):

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
        files = torrent.files
        if not files: return False

        selected_id = None
        selected_name = None

        if len(files) == 1:
            selected_id = files[0]["id"]
            selected_name = files[0]["name"]
        else:
            media_extensions = {'.mkv', '.mp4', '.avi'}
            media_files = [f for f in files if any(f["name"].lower().endswith(ext) for ext in media_extensions)]
            if len(media_files) == 1:
                selected_id = media_files[0]["id"]
                selected_name = media_files[0]["name"]

        if selected_id:
            try:
                self.rd.select_torrent_files(torrent_id, [selected_id])
                torrent.selected_files = [selected_name]
                torrent.custom_folder_name = None
                torrent.state = TorrentState.WAITING_FOR_REALDEBRID
                self.logger.info(f"{torrent_id}: Auto-selected '{selected_name}'")
                return True
            except RealDebridError:
                return False

        return False

    def _attempt_unrestrict(self, torrent_id: str, torrent: TorrentItem, links: list[str]):
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
                    self.logger.info(f"Hoster unavailable for torrent {torrent_id}.")
                else:
                    all_failed_hoster_unavailable = False
                    self.logger.warning(f"Unrestrict error for {torrent_id}: {e}")

        if direct_links:
            torrent.direct_links = direct_links
            torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID
            torrent.unrestrict_backoff = 60
            torrent.next_unrestrict_at = 0
            self.logger.info(f"Torrent {torrent_id} available: {len(torrent.direct_links)} links.")
        else:
            if all_failed_hoster_unavailable:
                torrent.schedule_unrestrict_retry()
                wait = torrent.next_unrestrict_at - int(time.time())
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID  # Keep available to retry
                self.logger.info(f"Hoster unavailable for {torrent_id}. Retrying in ~{wait}s.")
            else:
                torrent.state = TorrentState.AVAILABLE_FROM_REALDEBRID

    def _on_download_complete(self, torrent_id: str):
        self.logger.info(f"Torrent {torrent_id} completed FileOps cycle.")
        with self.lock:
            torrent = self.torrents.get(torrent_id)
            if not torrent: return

        if self.config_data.get("delete_on_complete", True):
            self.deletion_service.schedule_deletion(torrent_id)

        self.deletion_service.cleanup_temp_directory(torrent_id)
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