import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import unquote
import re
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
                # Extract the raw filename from URL
                raw_name = url.split("/")[-1].split("?")[0]

                # Decode URL-encoded characters like %20, %28, etc.
                filename = unquote(raw_name)

                # Apply filename strip pattern if provided
                if torrent.filename_strip_pattern:
                    filename = self._strip_filename_pattern(filename, torrent.filename_strip_pattern)

                # Sanitize filename to avoid filesystem issues
                filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

                dest = temp_path / filename
                self.logger.info(f"Downloading file {i}/{len(torrent.direct_links)}: {filename}")
                self._download_file(url, dest,
                                    on_progress=lambda p: on_progress(torrent.id, p) if on_progress else None)

            # Move files to media dir
            torrent.state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER
            if on_transfer:
                on_transfer(torrent.id)

            # Check if we have multiple files
            files_in_temp = list(temp_path.iterdir())

            if len(files_in_temp) == 1 and files_in_temp[0].is_file():
                # Single file: move directly to media root
                file = files_in_temp[0]
                target = media_path / file.name
                self.logger.info(f"Transferring single file: {file} -> {target}")
                shutil.move(str(file), target)
            else:
                # Multiple files: create a subfolder in media
                # Use custom folder name if provided, otherwise fall back to torrent.id
                folder_name = torrent.custom_folder_name if torrent.custom_folder_name else torrent.id
                # Sanitize folder name
                folder_name = re.sub(r'[<>:"/\\|?*]', "_", folder_name)

                target_folder = media_path / folder_name
                self.logger.info(f"Transferring multiple files to folder: {target_folder}")

                # Move the entire temp folder to media
                if target_folder.exists():
                    self.logger.warning(f"Target folder {target_folder} already exists, removing it first")
                    shutil.rmtree(target_folder)

                shutil.move(str(temp_path), str(target_folder))
                # temp_path is now gone, so we can't rmdir it
                temp_path = None

            # Clean up temp directory if it still exists
            if temp_path and temp_path.exists():
                temp_path.rmdir()

            torrent.state = TorrentState.FINISHED
            self.logger.info(f"Torrent {torrent.id} finished successfully.")
            on_complete(torrent.id)

        except Exception as e:
            self.logger.error(f"FileOps error for {torrent.id}: {e}")
            torrent.state = TorrentState.FAILED
            on_complete(torrent.id)

    # ------------------------------
    # Helper: strip filename pattern
    # ------------------------------
    def _strip_filename_pattern(self, filename: str, pattern: str) -> str:
        """
        Strip a pattern from the start of a filename and clean up leftover whitespace.

        Args:
            filename: Original filename
            pattern: Pattern to strip from the start (case-insensitive)

        Returns:
            Cleaned filename with pattern removed and whitespace stripped
        """
        if not pattern or not filename:
            return filename

        # Case-insensitive check if filename starts with pattern
        if filename.lower().startswith(pattern.lower()):
            # Remove the pattern from the start
            cleaned = filename[len(pattern):]
            # Strip any leading whitespace
            cleaned = cleaned.lstrip()
            self.logger.info(f"Stripped pattern '{pattern}' from filename: '{filename}' -> '{cleaned}'")
            return cleaned

        return filename

    # ------------------------------
    # Helper: download with retries
    # ------------------------------
    def _download_file(self, url: str, dest: Path, on_progress=None, max_retries: int = 3,
                       chunk_size: int = 1024 * 1024):
        """Stream a file to disk with retries and reduced logging."""
        for attempt in range(1, max_retries + 1):
            try:
                with requests.get(url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    start_time = time.time()
                    last_logged_percent = -20  # Start at -20 so 0% gets logged

                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)

                                if total:
                                    pct = downloaded / total * 100

                                    # Only log every 20% or at completion
                                    if pct >= last_logged_percent + 20 or pct >= 100:
                                        elapsed = time.time() - start_time
                                        rate = downloaded / (elapsed + 1e-6) / (1024 * 1024)
                                        self.logger.info(
                                            f"{dest.name}: {pct:.1f}% ({downloaded / (1024 * 1024):.2f} MB) @ {rate:.2f} MB/s"
                                        )
                                        last_logged_percent = int(pct / 20) * 20

                                    if on_progress:
                                        on_progress(pct)
                    return  # success
            except Exception as e:
                self.logger.warning(f"Download attempt {attempt}/{max_retries} failed for {url}: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(3 * attempt)