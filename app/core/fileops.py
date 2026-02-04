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

    def _get_dest_info(self, torrent, config):
        """Helper to determine destination path components based on metadata."""
        base_media_path = Path(config["media_path"])
        tmdb_id = getattr(torrent, 'tmdb_id', None)

        # DEBUG LOGGING
        self.logger.info(
            f"[FILEOPS] _get_dest_info: ID={tmdb_id}, Name={torrent.name}, Title={getattr(torrent, 'tmdb_title', 'N/A')}")

        if not tmdb_id:
            # Unsorted Logic
            unsorted_str = config.get("unsorted_path")
            if unsorted_str:
                dest_root = Path(unsorted_str)
            else:
                dest_root = base_media_path / "unsorted"

            # For unsorted, we just use the torrent name or custom folder
            folder_name = torrent.custom_folder_name if torrent.custom_folder_name else (torrent.name or torrent.id)
            folder_name = re.sub(r'[<>:"/\\|?*]', " ", folder_name).strip()

            return dest_root, folder_name

        # Metadata Logic
        media_type = getattr(torrent, 'media_type', 'unknown').lower()

        # 1. Determine Root Subfolder (Shows vs Movies)
        if media_type == 'tv':
            subdir = "Shows"
        elif media_type == 'movie':
            subdir = "Movies"
        else:
            subdir = "Others"

        dest_root = base_media_path / subdir

        # 2. Determine Folder Name: "Title [tmdbid-12345]"
        # Prefer the official TMDB Title if available
        title_to_use = getattr(torrent, 'tmdb_title', None)
        if not title_to_use:
            title_to_use = torrent.name

        clean_name = re.sub(r'[<>:"/\\|?*]', " ", title_to_use).strip()
        folder_name = f"{clean_name} [tmdbid-{tmdb_id}]"

        return dest_root, folder_name

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

            # Use helper to get consistent paths
            dest_root, target_folder_name = self._get_dest_info(torrent, config)

            self.logger.info(f"Routing {torrent.id} to: {dest_root} / {target_folder_name}")

            # Destination is the specific folder for this media
            final_dest_dir = dest_root / target_folder_name
            final_dest_dir.mkdir(parents=True, exist_ok=True)

            # --- 3. TRANSFER PHASE ---
            files_in_temp = list(temp_path.iterdir())

            if len(files_in_temp) == 1 and files_in_temp[0].is_file():
                # Single file -> Move INTO the final folder
                file = files_in_temp[0]
                target = final_dest_dir / file.name
                self.logger.info(f"Transferring single file: {file} -> {target}")
                shutil.move(str(file), target)
            else:
                # Multiple files -> Move CONTENTS into the final folder
                self.logger.info(f"Transferring {len(files_in_temp)} items to: {final_dest_dir}")
                for item in files_in_temp:
                    target = final_dest_dir / item.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    shutil.move(str(item), str(target))

            # Cleanup temp
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)

            torrent.state = TorrentState.FINISHED
            self.logger.info(f"Torrent {torrent.id} finished successfully.")
            on_complete(torrent.id)

        except Exception as e:
            self.logger.error(f"FileOps error for {torrent.id}: {e}", exc_info=True)
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

    def move_from_unsorted(self, torrent, config):
        """Move a finished torrent from Unsorted to the final Media Library."""
        try:
            # 1. Source Determination
            unsorted_str = config.get("unsorted_path")
            if unsorted_str:
                unsorted_root = Path(unsorted_str)
            else:
                unsorted_root = Path(config["media_path"]) / "unsorted"

            # Identify source name (sanitized)
            folder_name_in_unsorted = torrent.custom_folder_name if torrent.custom_folder_name else (
                        torrent.name or torrent.id)
            folder_name_in_unsorted = re.sub(r'[<>:"/\\|?*]', " ", folder_name_in_unsorted).strip()

            source_path = unsorted_root / folder_name_in_unsorted

            # Fallback: Check for single file if folder not found
            if not source_path.exists() and torrent.files:
                first_file_name = torrent.files[0]['path'].lstrip('/')
                sanitized_file_name = re.sub(r'[<>:"/\\|?*]', "_", first_file_name)

                potential_path = unsorted_root / sanitized_file_name
                if potential_path.exists():
                    source_path = potential_path

            if not source_path.exists():
                self.logger.error(f"Cannot find source in Unsorted: {source_path}")
                return False

            # 2. Destination Determination
            # Use the helper to get the canonical folder name
            dest_root, dest_folder_name = self._get_dest_info(torrent, config)

            dest_folder = dest_root / dest_folder_name
            dest_folder.mkdir(parents=True, exist_ok=True)

            self.logger.info(f"Moving from {source_path} to {dest_folder}")

            # 3. Execution
            destination = dest_folder / source_path.name

            if destination.exists():
                self.logger.warning(f"Destination {destination} exists. Overwriting.")
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()

            shutil.move(str(source_path), str(destination))
            return True

        except Exception as e:
            self.logger.error(f"Error moving from unsorted: {e}", exc_info=True)
            return False