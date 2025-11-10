import threading
from collections import deque
from typing import Dict, Optional
from core.states import TorrentState


class QueueService:
    """
    Manages the torrent processing queue and active torrent tracking.

    Responsibilities:
    - Maintain queue of torrent IDs waiting to be processed
    - Track which torrent is currently active (downloading)
    - Calculate queue positions for UI display
    - Clean up failed torrents from queue
    """

    def __init__(self, torrents: Dict, lock: threading.Lock, logger):
        """
        Initialize the queue service.

        Args:
            torrents: Shared dict of torrent_id -> TorrentItem
            lock: Shared threading lock for thread-safe operations
            logger: Logger instance
        """
        self.torrents = torrents
        self.lock = lock
        self.logger = logger

        self.queue: deque = deque()
        self.active_torrent_id: Optional[str] = None

    def add_to_queue(self, torrent_id: str) -> int:
        """
        Add a torrent to the end of the queue.

        Args:
            torrent_id: ID of torrent to add

        Returns:
            Queue position (1-indexed)
        """
        with self.lock:
            self.queue.append(torrent_id)
            position = len(self.queue)
            self.logger.info(f"Added torrent {torrent_id} to queue at position {position}")
            return position

    def remove_from_queue(self, torrent_id: str) -> bool:
        """
        Remove a specific torrent from the queue.

        Args:
            torrent_id: ID of torrent to remove

        Returns:
            True if torrent was in queue and removed, False otherwise
        """
        with self.lock:
            if torrent_id in self.queue:
                self.queue.remove(torrent_id)
                remaining = len(self.queue)
                self.logger.info(f"Removed torrent {torrent_id} from queue. {remaining} remaining.")
                return True
            return False

    def get_next_active(self) -> Optional[str]:
        """
        Activate the next torrent in queue if no torrent is currently active.

        Returns:
            The newly activated torrent ID, or None if queue is empty or torrent already active
        """
        with self.lock:
            # Only activate if nothing is currently active
            if self.active_torrent_id is not None:
                return None

            # Check if queue has torrents
            if len(self.queue) == 0:
                return None

            # Activate the first torrent in queue
            self.active_torrent_id = self.queue[0]
            queue_length = len(self.queue)
            self.logger.info(
                f"Activating torrent {self.active_torrent_id} from queue "
                f"(position 1 of {queue_length})"
            )
            return self.active_torrent_id

    def clear_active(self) -> None:
        """Clear the currently active torrent."""
        with self.lock:
            if self.active_torrent_id:
                self.logger.info(
                    f"Cleared active torrent {self.active_torrent_id}, "
                    "next torrent will be activated in next poll cycle."
                )
                self.active_torrent_id = None

    def get_queue_position(self, torrent_id: str) -> Optional[int]:
        """
        Get the queue position for a specific torrent.

        Args:
            torrent_id: ID of torrent to check

        Returns:
            Queue position (1-indexed), or None if not in queue
        """
        with self.lock:
            if torrent_id in self.queue:
                return list(self.queue).index(torrent_id) + 1
            return None

    def is_active(self, torrent_id: str) -> bool:
        """
        Check if a specific torrent is the currently active torrent.

        Args:
            torrent_id: ID of torrent to check

        Returns:
            True if torrent is active, False otherwise
        """
        with self.lock:
            return torrent_id == self.active_torrent_id

    def clean_failed_from_queue(self) -> int:
        """
        Remove all failed torrents from the queue.

        Returns:
            Number of failed torrents removed
        """
        with self.lock:
            failed_torrents = [
                tid for tid in self.queue
                if tid in self.torrents and self.torrents[tid].state == TorrentState.FAILED
            ]

            for tid in failed_torrents:
                self.queue.remove(tid)
                self.logger.info(f"Removed failed torrent {tid} from queue. {len(self.queue)} remaining.")

                # If this was the active torrent, clear it
                if self.active_torrent_id == tid:
                    self.active_torrent_id = None
                    self.logger.info("Cleared active torrent (was failed), next will activate on next cycle.")

            return len(failed_torrents)

    def get_queue_status(self) -> dict:
        """
        Get current queue status for monitoring/debugging.

        Returns:
            Dict with queue information
        """
        with self.lock:
            return {
                "queue_length": len(self.queue),
                "active_torrent_id": self.active_torrent_id,
                "queue_ids": list(self.queue)
            }

    def get_queue_length(self) -> int:
        """
        Get the current length of the queue.

        Returns:
            Number of torrents in queue
        """
        with self.lock:
            return len(self.queue)