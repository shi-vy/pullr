import threading
import time
import shutil
from pathlib import Path
from typing import Dict, Optional


class DeletionService:
    """
    Manages delayed deletion of torrents from RealDebrid and cleanup of temporary files.

    Responsibilities:
    - Schedule torrents for delayed deletion after completion
    - Process pending deletions when their time has come
    - Clean up temporary download directories
    - Track deletion status for UI display
    """

    def __init__(self, rd_client, torrents: Dict, lock: threading.Lock, logger, config_data: dict):
        """
        Initialize the deletion service.

        Args:
            rd_client: RealDebridClient instance for API calls
            torrents: Shared dict of torrent_id -> TorrentItem
            lock: Shared threading lock for thread-safe operations
            logger: Logger instance
            config_data: Configuration dict with deletion settings
        """
        self.rd = rd_client
        self.torrents = torrents
        self.lock = lock
        self.logger = logger
        self.config_data = config_data

        self.pending_deletions: Dict[str, float] = {}  # torrent_id -> deletion_time (epoch)
        self.deletion_delay_hours = config_data.get("deletion_delay_hours", 3)

        self.logger.info(
            f"DeletionService initialized: {self.deletion_delay_hours} hour delay after completion"
        )

    def schedule_deletion(self, torrent_id: str) -> None:
        """
        Schedule a torrent for delayed deletion from RealDebrid.

        Args:
            torrent_id: ID of torrent to schedule for deletion
        """
        deletion_time = time.time() + (self.deletion_delay_hours * 3600)

        with self.lock:
            self.pending_deletions[torrent_id] = deletion_time

            # Track completion time on the torrent itself
            if torrent_id in self.torrents:
                self.torrents[torrent_id].completion_time = time.time()

        self.logger.info(
            f"Scheduled deletion of torrent {torrent_id} from RealDebrid "
            f"in {self.deletion_delay_hours} hours"
        )

    def process_pending_deletions(self) -> int:
        """
        Check for torrents ready to be deleted and delete them from RealDebrid.

        Returns:
            Number of torrents deleted in this cycle
        """
        now = time.time()
        to_delete = []

        # Find torrents ready for deletion (outside lock for minimal lock time)
        with self.lock:
            for torrent_id, deletion_time in list(self.pending_deletions.items()):
                if now >= deletion_time:
                    to_delete.append(torrent_id)

        # Process deletions
        deleted_count = 0
        for torrent_id in to_delete:
            if self._delete_from_realdebrid(torrent_id):
                deleted_count += 1

        return deleted_count

    def _delete_from_realdebrid(self, torrent_id: str) -> bool:
        """
        Delete a torrent from RealDebrid and mark it as deleted.

        Args:
            torrent_id: ID of torrent to delete

        Returns:
            True if deletion succeeded, False otherwise
        """
        try:
            self.rd.delete_torrent(torrent_id)
            self.logger.info(f"Deleted torrent {torrent_id} from RealDebrid (delayed deletion)")

            with self.lock:
                # Mark as deleted so polling stops trying to update it
                if torrent_id in self.torrents:
                    self.torrents[torrent_id].deleted_from_realdebrid = True

                # Remove from pending deletions
                self.pending_deletions.pop(torrent_id, None)

            return True

        except Exception as e:
            self.logger.warning(f"Failed to delete torrent {torrent_id}: {e}")

            # Remove from pending anyway to avoid retry loops
            with self.lock:
                self.pending_deletions.pop(torrent_id, None)

            return False

    def cleanup_temp_directory(self, torrent_id: str) -> bool:
        """
        Delete the temporary download directory for a torrent.

        Args:
            torrent_id: ID of torrent whose temp directory to delete

        Returns:
            True if cleanup succeeded, False otherwise
        """
        try:
            temp_path = Path(self.config_data.get("download_temp_path", "/downloads")) / torrent_id

            if temp_path.exists():
                shutil.rmtree(temp_path)
                self.logger.info(f"Deleted temporary files for {torrent_id}")
                return True
            else:
                self.logger.debug(f"No temp directory found for {torrent_id}")
                return False

        except Exception as e:
            self.logger.warning(f"Failed to delete temp files for {torrent_id}: {e}")
            return False

    def is_pending_deletion(self, torrent_id: str) -> bool:
        """
        Check if a torrent is scheduled for deletion.

        Args:
            torrent_id: ID of torrent to check

        Returns:
            True if torrent is scheduled for deletion, False otherwise
        """
        with self.lock:
            return torrent_id in self.pending_deletions

    def get_deletion_time_remaining(self, torrent_id: str) -> Optional[str]:
        """
        Get formatted time remaining until deletion for a torrent.

        Args:
            torrent_id: ID of torrent to check

        Returns:
            Formatted string like "2h 30m", or None if not scheduled
        """
        with self.lock:
            if torrent_id not in self.pending_deletions:
                return None

            deletion_time = self.pending_deletions[torrent_id]
            time_remaining = deletion_time - time.time()

            if time_remaining <= 0:
                return "deleting soon"

            hours = int(time_remaining // 3600)
            minutes = int((time_remaining % 3600) // 60)
            return f"{hours}h {minutes}m"

    def cancel_deletion(self, torrent_id: str) -> bool:
        """
        Cancel a scheduled deletion for a torrent.

        Args:
            torrent_id: ID of torrent to cancel deletion for

        Returns:
            True if deletion was scheduled and cancelled, False otherwise
        """
        with self.lock:
            if torrent_id in self.pending_deletions:
                self.pending_deletions.pop(torrent_id)
                self.logger.info(f"Cancelled scheduled deletion for torrent {torrent_id}")
                return True
            return False

    def get_pending_deletions_count(self) -> int:
        """
        Get count of torrents pending deletion.

        Returns:
            Number of torrents scheduled for deletion
        """
        with self.lock:
            return len(self.pending_deletions)

    def get_deletion_status(self) -> dict:
        """
        Get status of all pending deletions for monitoring/debugging.

        Returns:
            Dict mapping torrent_id to time remaining string
        """
        with self.lock:
            status = {}
            for torrent_id in self.pending_deletions:
                status[torrent_id] = self.get_deletion_time_remaining(torrent_id)
            return status