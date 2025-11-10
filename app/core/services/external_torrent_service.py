import threading
import time
from datetime import datetime
from typing import Dict, Set
from core.states import TorrentState
from core.realdebrid import RealDebridError


class ExternalTorrentService:
    """
    Scans RealDebrid for externally-added torrents and imports them into Pullr.

    Responsibilities:
    - Periodically scan RealDebrid for new torrents not added via Pullr
    - Auto-select all files for external torrents
    - Track known torrent IDs to avoid duplicate processing
    - Filter out torrents added before Pullr started
    """

    def __init__(
            self,
            rd_client,
            torrents: Dict,
            lock: threading.Lock,
            logger,
            config_data: dict,
            app_start_time: float
    ):
        """
        Initialize the external torrent service.

        Args:
            rd_client: RealDebridClient instance for API calls
            torrents: Shared dict of torrent_id -> TorrentItem
            lock: Shared threading lock for thread-safe operations
            logger: Logger instance
            config_data: Configuration dict with scan settings
            app_start_time: Timestamp when Pullr started (to filter old torrents)
        """
        self.rd = rd_client
        self.torrents = torrents
        self.lock = lock
        self.logger = logger
        self.config_data = config_data
        self.app_start_time = app_start_time

        self.known_torrent_ids: Set[str] = set()
        self.last_scan_time: float = 0

        # Get scan interval from config (0 or None means disabled)
        scan_interval = config_data.get("external_torrent_scan_interval_seconds", 15)
        if scan_interval is None:
            scan_interval = 0
        self.scan_interval = int(scan_interval) if scan_interval > 0 else 0
        self.enabled = self.scan_interval > 0

        if self.enabled:
            self.logger.info(
                f"ExternalTorrentService initialized: scanning every {self.scan_interval}s"
            )
        else:
            self.logger.info("ExternalTorrentService initialized: scanning disabled")

    def should_scan(self) -> bool:
        """
        Check if enough time has elapsed to perform another scan.

        Returns:
            True if scan interval has elapsed and scanning is enabled, False otherwise
        """
        if not self.enabled:
            return False

        now = time.time()
        return (now - self.last_scan_time) >= self.scan_interval

    def scan_for_external_torrents(self) -> int:
        """
        Scan RealDebrid for externally-added torrents and import them.

        Returns:
            Number of new external torrents detected and added
        """
        if not self.enabled:
            return 0

        self.last_scan_time = time.time()

        try:
            self.logger.debug("Scanning for external torrents...")
            torrents = self.rd.list_torrents()

            if not isinstance(torrents, list):
                self.logger.warning(f"Unexpected response from list_torrents: {type(torrents)}")
                return 0

            new_count = 0
            for torrent_data in torrents:
                if self._process_external_torrent(torrent_data):
                    new_count += 1

            if new_count > 0:
                self.logger.info(
                    f"External torrent scan completed: {new_count} new torrent(s) added"
                )

            return new_count

        except Exception as e:
            self.logger.warning(f"External torrent scan error: {e}")
            return 0

    def _process_external_torrent(self, torrent_data: dict) -> bool:
        """
        Process a single torrent from RealDebrid API response.

        Args:
            torrent_data: Dict containing torrent info from RealDebrid API

        Returns:
            True if new external torrent was added, False otherwise
        """
        torrent_id = torrent_data.get("id")
        if not torrent_id:
            return False

        # Skip if already known
        if torrent_id in self.known_torrent_ids:
            return False

        # Check if torrent was added after Pullr started
        if not self._is_torrent_new(torrent_data):
            # Mark as known but don't process
            self.known_torrent_ids.add(torrent_id)
            return False

        # Only process torrents that need file selection or are already downloaded
        status = torrent_data.get("status", "").lower()
        if status not in ["waiting_files_selection", "downloaded"]:
            return False

        # Found a new external torrent!
        filename = torrent_data.get("filename", torrent_id)
        self.logger.info(
            f"Detected external torrent: {filename} ({torrent_id}) [status: {status}]"
        )

        # Try to process it
        return self._import_external_torrent(torrent_id, filename, status)

    def _is_torrent_new(self, torrent_data: dict) -> bool:
        """
        Check if torrent was added after Pullr started.

        Args:
            torrent_data: Dict containing torrent info from RealDebrid API

        Returns:
            True if torrent was added after app start, False otherwise
        """
        added_str = torrent_data.get("added")
        if not added_str:
            return False

        try:
            # Parse ISO format: "2025-10-25T05:00:51.000Z"
            added_dt = datetime.fromisoformat(added_str.replace('Z', '+00:00'))
            added_ts = added_dt.timestamp()

            # Only process if added after Pullr started
            return added_ts >= self.app_start_time

        except Exception as e:
            self.logger.debug(f"Failed to parse timestamp '{added_str}': {e}")
            return False

    def _import_external_torrent(self, torrent_id: str, filename: str, status: str) -> bool:
        """
        Import an external torrent into Pullr's management system.

        Args:
            torrent_id: RealDebrid torrent ID
            filename: Display name for the torrent
            status: Current status from RealDebrid

        Returns:
            True if successfully imported, False otherwise
        """
        try:
            # Auto-select all files (only needed if waiting for selection)
            if status == "waiting_files_selection":
                self.rd.select_files(torrent_id, "all")
                self.logger.info(f"Auto-selected all files for external torrent {torrent_id}")
            else:
                self.logger.info(
                    f"External torrent {torrent_id} already downloaded, skipping file selection"
                )

            # Import the TorrentItem class here to avoid circular imports
            from core.manager import TorrentItem

            # Create TorrentItem for this external torrent
            item = TorrentItem(magnet_link="", source="external")
            item.id = torrent_id
            item.name = filename
            item.custom_folder_name = filename  # Use RD's filename as folder name
            item.state = TorrentState.WAITING_FOR_REALDEBRID

            with self.lock:
                self.torrents[torrent_id] = item
                self.known_torrent_ids.add(torrent_id)

            self.logger.info(f"Successfully imported external torrent {torrent_id}")
            return True

        except RealDebridError as e:
            self.logger.warning(f"Failed to process external torrent {torrent_id}: {e}")

            # Still mark as known to avoid retry loops, but show as failed
            from core.manager import TorrentItem

            item = TorrentItem(magnet_link="", source="external")
            item.id = torrent_id
            item.name = filename
            item.state = TorrentState.FAILED
            item.error_message = f"Processing failed: {str(e)}"

            with self.lock:
                self.torrents[torrent_id] = item
                self.known_torrent_ids.add(torrent_id)

            return False

    def mark_as_known(self, torrent_id: str) -> None:
        """
        Mark a torrent ID as known (used for manual torrents to prevent re-detection).

        Args:
            torrent_id: ID of torrent to mark as known
        """
        with self.lock:
            self.known_torrent_ids.add(torrent_id)

    def is_known(self, torrent_id: str) -> bool:
        """
        Check if a torrent ID is already known.

        Args:
            torrent_id: ID of torrent to check

        Returns:
            True if torrent is known, False otherwise
        """
        with self.lock:
            return torrent_id in self.known_torrent_ids

    def get_known_count(self) -> int:
        """
        Get count of known torrent IDs.

        Returns:
            Number of torrents marked as known
        """
        with self.lock:
            return len(self.known_torrent_ids)

    def get_scan_status(self) -> dict:
        """
        Get current scan status for monitoring/debugging.

        Returns:
            Dict with scan information
        """
        return {
            "enabled": self.enabled,
            "scan_interval": self.scan_interval,
            "last_scan_time": self.last_scan_time,
            "known_torrents_count": self.get_known_count(),
            "time_until_next_scan": max(0, self.scan_interval - (time.time() - self.last_scan_time))
        }