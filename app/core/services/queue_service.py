import threading
from collections import deque
from typing import Dict, Optional
from core.states import TorrentState


class QueueService:
    """
    Manages the torrent processing queue and active torrent tracking.
    """

    def __init__(self, torrents: Dict, lock: threading.Lock, logger):
        self.torrents = torrents
        self.lock = lock
        self.logger = logger

        self.queue: deque = deque()
        self.active_torrent_id: Optional[str] = None

    def add_to_queue(self, torrent_id: str) -> int:
        with self.lock:
            self.queue.append(torrent_id)
            position = len(self.queue)
            self.logger.info(f"[QUEUE] Added torrent {torrent_id} to queue at position {position}")
            return position

    def remove_from_queue(self, torrent_id: str) -> bool:
        with self.lock:
            if torrent_id in self.queue:
                self.queue.remove(torrent_id)
                remaining = len(self.queue)
                self.logger.info(f"[QUEUE] Removed torrent {torrent_id} from queue. {remaining} remaining.")
                return True
            return False

    def get_next_active(self) -> Optional[str]:
        with self.lock:
            if self.active_torrent_id is not None:
                return None

            if len(self.queue) == 0:
                return None

            self.active_torrent_id = self.queue[0]
            queue_length = len(self.queue)
            self.logger.info(
                f"[QUEUE] Activating torrent {self.active_torrent_id} from queue "
                f"(position 1 of {queue_length})"
            )
            return self.active_torrent_id

    def clear_active(self) -> None:
        with self.lock:
            if self.active_torrent_id:
                self.logger.info(
                    f"[QUEUE] Cleared active torrent {self.active_torrent_id}, "
                    "next torrent will be activated in next poll cycle."
                )
                self.active_torrent_id = None

    def get_queue_position(self, torrent_id: str) -> Optional[int]:
        with self.lock:
            if torrent_id in self.queue:
                return list(self.queue).index(torrent_id) + 1
            return None

    def is_active(self, torrent_id: str) -> bool:
        with self.lock:
            return torrent_id == self.active_torrent_id

    def clean_failed_from_queue(self) -> int:
        with self.lock:
            failed_torrents = [
                tid for tid in self.queue
                if tid in self.torrents and self.torrents[tid].state == TorrentState.FAILED
            ]

            for tid in failed_torrents:
                self.queue.remove(tid)
                self.logger.info(f"[QUEUE] Removed failed torrent {tid} from queue. {len(self.queue)} remaining.")

                if self.active_torrent_id == tid:
                    self.active_torrent_id = None
                    self.logger.info("[QUEUE] Cleared active torrent (was failed).")

            return len(failed_torrents)

    def get_queue_status(self) -> dict:
        with self.lock:
            return {
                "queue_length": len(self.queue),
                "active_torrent_id": self.active_torrent_id,
                "queue_ids": list(self.queue)
            }

    def get_queue_length(self) -> int:
        with self.lock:
            return len(self.queue)

    def mark_completed(self, torrent_id: str):
        """Mark a torrent as complete and advance the queue."""
        self.logger.info(f"[QUEUE] mark_completed called for {torrent_id}. Waiting for lock...")
        with self.lock:
            self.logger.info(f"[QUEUE] Lock acquired for mark_completed {torrent_id}.")
            if self.active_torrent_id == torrent_id:
                self.active_torrent_id = None
                self.logger.info(f"[QUEUE] Torrent {torrent_id} marked complete. Queue slot freed.")
            else:
                self.logger.info(f"[QUEUE] mark_completed: {torrent_id} was NOT the active torrent ({self.active_torrent_id}).")
        self.logger.info(f"[QUEUE] mark_completed finished for {torrent_id}.")