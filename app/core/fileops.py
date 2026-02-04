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
            temp_path.mkdir(parents=True, exist_ok=True)

            # --- 1. DOWNLOAD PHASE ---
            for i, url in enumerate(torrent.direct_links, start=1):
                # Extract filename
                raw_name = url.split("/")[-1].split("?")[0]
                filename = unquote(raw_name)

                # Apply strip pattern
                if torrent.filename_strip_pattern:
                    filename = self._strip_filename_pattern(filename, torrent.filename_strip_pattern)

                # Sanitize
                filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

                dest = temp_path / filename
                self.logger.info(f"Downloading file {i}/{len(torrent.direct_links)}: {filename}")
                self._download_file(url, dest,
                                    on_progress=lambda p: on_progress(torrent.id, p) if on_progress else None)

            # --- 2. ROUTING PHASE ---
            # Update state
            torrent.state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER
            if on_transfer:
                on_transfer(torrent.id)

            # Check for Metadata to determine destination
            tmdb_id = getattr(torrent, 'tmdb_id', None)

            if tmdb_id:
                # Metadata exists -> Standard Media Path
                dest_root = Path(config["media_path"])
                self.logger.info(f"Torrent {torrent.id} has TMDB ID {tmdb_id}. Moving to Media Library.")
            else:
                # No Metadata -> Unsorted Path
                # Use config value or default to 'unsorted' subdir in media path
                unsorted_str = config.get("unsorted_path")
                if unsorted_str:
                    dest_root = Path(unsorted_str)
                else:
                    dest_root = Path(config["media_path"]) / "unsorted"

                self.logger.info(f"Torrent {torrent.id} has NO metadata. Moving to Unsorted: {dest_root}")

            dest_root.mkdir(parents=True, exist_ok=True)

            # --- 3. TRANSFER PHASE ---
            files_in_temp = list(temp_path.iterdir())

            if len(files_in_temp) == 1 and files_in_temp[0].is_file():
                # Single file
                file = files_in_temp[0]
                target = dest_root / file.name
                self.logger.info(f"Transferring single file: {file} -> {target}")
                shutil.move(str(file), target)
            else:
                # Multiple files
                folder_name = torrent.custom_folder_name if torrent.custom_folder_name else torrent.id
                folder_name = re.sub(r'[<>:"/\\|?*]', "_", folder_name)

                target_folder = dest_root / folder_name
                self.logger.info(f"Transferring multiple files to folder: {target_folder}")

                if target_folder.exists():
                    self.logger.warning(f"Target folder {target_folder} already exists, removing it first")
                    shutil.rmtree(target_folder)

                shutil.move(str(temp_path), str(target_folder))
                temp_path = None

            # Cleanup
            if temp_path and temp_path.exists():
                temp_path.rmdir()

            torrent.state = TorrentState.FINISHED
            self.logger.info(f"Torrent {torrent.id} finished successfully.")
            on_complete(torrent.id)

        except Exception as e:
            self.logger.error(f"FileOps error for {torrent.id}: {e}")
            torrent.state = TorrentState.FAILED
            on_complete(torrent.id)

    def _strip_filename_pattern(self, filename: str, pattern: str) -> str:
        if not pattern or not filename:
            return filename
        if filename.lower().startswith(pattern.lower()):
            cleaned = filename[len(pattern):].lstrip()
            self.logger.info(f"Stripped pattern '{pattern}': '{filename}' -> '{cleaned}'")
            return cleaned
        return filename

    def _download_file(self, url: str, dest: Path, on_progress=None, max_retries: int = 3,
                       chunk_size: int = 1024 * 1024):
        for attempt in range(1, max_retries + 1):
            try:
                with requests.get(url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    start_time = time.time()
                    last_logged_percent = -20

                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    pct = downloaded / total * 100
                                    if pct >= last_logged_percent + 20 or pct >= 100:
                                        elapsed = time.time() - start_time
                                        rate = downloaded / (elapsed + 1e-6) / (1024 * 1024)
                                        self.logger.info(
                                            f"{dest.name}: {pct:.1f}% ({downloaded / (1024 * 1024):.2f} MB) @ {rate:.2f} MB/s")
                                        last_logged_percent = int(pct / 20) * 20
                                    if on_progress:
                                        on_progress(pct)
                    return
            except Exception as e:
                self.logger.warning(f"Download attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(3 * attempt)

    # Add this new method to the FileOps class
    def move_from_unsorted(self, torrent, config):
        """Move a finished torrent from Unsorted to the final Media Library."""
        try:
            unsorted_str = config.get("unsorted_path")
            if unsorted_str:
                unsorted_root = Path(unsorted_str)
            else:
                unsorted_root = Path(config["media_path"]) / "unsorted"

            dest_root = Path(config["media_path"])

            # Determine the source folder/file
            folder_name = torrent.custom_folder_name if torrent.custom_folder_name else torrent.id
            folder_name = re.sub(r'[<>:"/\\|?*]', "_", folder_name)

            source_path = unsorted_root / folder_name
            target_path = dest_root / folder_name

            if not source_path.exists():
                # Check if it was a single file download (might be sitting directly in root)
                # This is a basic check, might need refinement depending on how your single files are named
                # For now, we assume the folder structure created during download
                self.logger.error(f"Cannot find source path {source_path} in unsorted.")
                return False

            self.logger.info(f"Moving {source_path} -> {target_path}")

            if target_path.exists():
                self.logger.warning(f"Target {target_path} exists. Overwriting.")
                shutil.rmtree(target_path)

            shutil.move(str(source_path), str(target_path))
            return True

        except Exception as e:
            self.logger.error(f"Error moving from unsorted: {e}")
            return False