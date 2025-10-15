# app/core/fileops.py
import os
import shutil
import threading
import time
from pathlib import Path

import requests
from core.states import TorrentState


class FileOps:
    """Handles downloading and transferring files for torrents."""

    def __init__(self, logger):
        self.logger = logger

    def start_download(self, torrent, config, on_complete, on_progress=None, on_transfer=None):
        """Starts downloading in a separate thread."""
        thread = threading.Thread(
            target=self._process_torrent,
            args=(torrent, config, on_complete, on_progress, on_transfer),
            daemon=True
        )
        thread.start()

    def _process_torrent(self, torrent, config, on_complete, on_progress, on_transfer):
        try:
            self.logger.info(f"Starting download for {torrent.id}...")
            torrent.state = TorrentState.DOWNLOADING_FROM_REALDEBRID
            if on_progress:
                on_progress(torrent.id, 0.0)

            temp_path = Path(config["download_temp_path"]) / torrent.id
            media_path = Path(config["media_path"])
            temp_path.mkdir(parents=True, exist_ok=True)
            media_path.mkdir(parents=True, exist_ok=True)

            for i, url in enumerate(torrent.direct_links, start=1):
                filename = url.split("/")[-1].split("?")[0]
                dest = temp_path / filename
                self.logger.info(f"Downloading file {i}/{len(torrent.direct_links)}: {filename}")
                self._download_file(url, dest,
                                    on_progress=lambda p: on_progress(torrent.id, p) if on_progress else None)

            # Move files to media dir
            torrent.state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER
            if on_transfer:
                on_transfer(torrent.id)

            for file in temp_path.iterdir():
                target = media_path / file.name
                self.logger.info(f"Transferring {file} -> {target}")
                shutil.move(str(file), target)

            temp_path.rmdir()
            torrent.state = TorrentState.FINISHED
            self.logger.info(f"Torrent {torrent.id} finished successfully.")
            on_complete(torrent.id)

        except Exception as e:
            self.logger.error(f"FileOps error for {torrent.id}: {e}")
            torrent.state = TorrentState.FAILED
            on_complete(torrent.id)

    # ------------------------------
    # Helper: download with retries
    # ------------------------------
    def _download_file(self, url: str, dest: Path, on_progress=None, max_retries: int = 3,
                       chunk_size: int = 1024 * 1024):
        """Stream a file to disk with retries."""
        for attempt in range(1, max_retries + 1):
            try:
                with requests.get(url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    start_time = time.time()

                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    pct = downloaded / total * 100
                                    elapsed = time.time() - start_time
                                    rate = downloaded / (elapsed + 1e-6) / (1024 * 1024)
                                    self.logger.info(
                                        f"{dest.name}: {pct:.1f}% ({downloaded / (1024 * 1024):.2f} MB) @ {rate:.2f} MB/s"
                                    )
                                    if on_progress:
                                        on_progress(pct)
                    return  # success
            except Exception as e:
                self.logger.warning(f"Download attempt {attempt}/{max_retries} failed for {url}: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(3 * attempt)

