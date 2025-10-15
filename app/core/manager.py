import threading
import time
import random
from typing import Dict

from core.states import TorrentState
from core.realdebrid import RealDebridClient, RealDebridError
from core.fileops import FileOps


class TorrentItem:
    """Represents one torrent in the queue."""
    def __init__(self, magnet_link: str):
        self.magnet = magnet_link
        self.id = None
        self.name = None
        self.state = TorrentState.SENT_TO_REALDEBRID
        self.last_update = time.time()
        self.direct_links = []
        # Backoff for unrestrict attempts
        self.progress = 0.0
        self.files = []
        self.unrestrict_backoff = 60  # seconds, start at 1 min
        self.next_unrestrict_at = 0   # epoch seconds
        # Download trigger flag (so FileOps starts only once)
        self._download_started = False

    def schedule_unrestrict_retry(self):
        # exponential backoff with jitter, cap at 10 min
        self.unrestrict_backoff = min(int(self.unrestrict_backoff * 2), 600)
        jitter = random.randint(0, max(1, int(self.unrestrict_backoff * 0.1)))
        self.next_unrestrict_at = int(time.time()) + self.unrestrict_backoff + jitter

    def __repr__(self):
        return f"<TorrentItem id={self.id} state={self.state}>"


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
        self.logger.info("Starting TorrentManager polling thread...")

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
            item = TorrentItem(magnet_link)
            item.id = torrent_id
            item.state = TorrentState.WAITING_FOR_REALDEBRID
            with self.lock:
                self.torrents[torrent_id] = item
            self.logger.info(f"Added torrent {torrent_id} to queue.")
            return torrent_id
        except RealDebridError as e:
            self.logger.error(f"Failed to add magnet: {e}")
            return None

    def get_status(self):
        """Return a snapshot of current torrent states."""
        with self.lock:
            return {
                tid: {
                    "state": str(t.state),
                    "progress": t.progress,
                    "files": getattr(t, "files", [])
                }
                for tid, t in self.torrents.items()
            }

    # ------------------------------
    # Internal Polling Loop
    # ------------------------------
    def _poll_loop(self):
        self.logger.info("Torrent polling loop started.")
        while not self.stop_event.is_set():
            try:
                self.logger.debug(f"Polling {len(self.torrents)} torrent(s)...")
                self._update_all()
            except Exception as e:
                self.logger.warning(f"Polling loop error: {e}")
            time.sleep(self.poll_interval)
        self.logger.info("Torrent polling loop stopped.")

    def _update_all(self):
        """Poll RealDebrid for status updates and trigger FileOps when appropriate."""
        now = int(time.time())
        with self.lock:
            items = list(self.torrents.items())

        for torrent_id, torrent in items:
            try:
                info = self.rd.get_torrent_info(torrent_id)
                rd_status = info.get("status", "").lower()

                # NEW: always log RD status to see what's happening
                self.logger.debug(f"{torrent_id}: RD status -> {rd_status}")

                # --- waiting / converting / queued states ---
                if rd_status == "waiting_files_selection":
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

                        # If available, trigger FileOps once
                        if (torrent.state == TorrentState.AVAILABLE_FROM_REALDEBRID
                                and torrent.direct_links
                                and not torrent._download_started):
                            torrent._download_started = True
                            self.logger.info(f"Triggering FileOps for torrent {torrent.id}...")
                            self.fileops.start_download(
                                torrent,
                                config=self.config_data,
                                on_complete=self._on_download_complete,
                                on_progress=self.on_download_progress,
                                on_transfer=lambda tid=torrent.id: self.on_transfer_complete(tid)
                            )

                elif rd_status == "error":
                    torrent.state = TorrentState.FAILED

                # NEW: handle any other unexpected RD status explicitly
                else:
                    self.logger.debug(
                        f"{torrent_id}: Unhandled RD status '{rd_status}', leaving state as {torrent.state}.")

                torrent.last_update = time.time()

            except RealDebridError as e:
                self.logger.warning(f"Error updating torrent {torrent_id}: {e}")

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
        self.logger.info(f"Torrent {torrent_id} completed FileOps cycle.")

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
