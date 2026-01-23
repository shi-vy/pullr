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

            # Download Loop [Keep existing download logic]
            for i, url in enumerate(torrent.direct_links, start=1):
                raw_name = url.split("/")[-1].split("?")[0]
                filename = unquote(raw_name)

                if torrent.filename_strip_pattern:
                    filename = self._strip_filename_pattern(filename, torrent.filename_strip_pattern)

                filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
                dest = temp_path / filename
                self.logger.info(f"Downloading file {i}/{len(torrent.direct_links)}: {filename}")
                self._download_file(url, dest,
                                    on_progress=lambda p: on_progress(torrent.id, p) if on_progress else None)

            # Transfer Phase
            torrent.state = TorrentState.TRANSFERRING_TO_MEDIA_SERVER
            if on_transfer:
                on_transfer(torrent.id)

            # --- JELLYFIN MODE LOGIC ---
            if config.get("jellyfin_mode"):
                self._transfer_jellyfin_mode(torrent, temp_path, media_path)
            else:
                self._transfer_standard_mode(torrent, temp_path, media_path)

            # Clean up
            if temp_path and temp_path.exists():
                try:
                    temp_path.rmdir()
                except OSError:
                    pass  # Directory might not be empty if something failed

            torrent.state = TorrentState.FINISHED
            self.logger.info(f"Torrent {torrent.id} finished successfully.")
            on_complete(torrent.id)

        except Exception as e:
            self.logger.error(f"FileOps error for {torrent.id}: {e}")
            torrent.state = TorrentState.FAILED
            on_complete(torrent.id)

    def _transfer_jellyfin_mode(self, torrent, temp_path: Path, media_root: Path):
        """
        Move files into {Category}/{Title} [tmdbid-{ID}]/ folder.
        """
        if not torrent.canonical_title or not torrent.tmdb_id:
            # Fallback to standard if metadata missing (shouldn't happen given Manager logic)
            self.logger.warning(f"Missing metadata for {torrent.id}, falling back to standard transfer.")
            self._transfer_standard_mode(torrent, temp_path, media_root)
            return

        # Sanitize folder name
        safe_title = re.sub(r'[<>:"/\\|?*]', "_", torrent.canonical_title)
        folder_name = f"{safe_title} [tmdbid-{torrent.tmdb_id}]"

        # Determine category folder
        category = "Movies" if torrent.media_type == "movie" else "Shows"
        target_folder = media_root / category / folder_name

        self.logger.info(f"Jellyfin Mode: Transferring to {target_folder}")

        # Create target
        target_folder.mkdir(parents=True, exist_ok=True)

        # Move all files from temp to this folder
        for item in temp_path.iterdir():
            if item.is_file():
                shutil.move(str(item), str(target_folder / item.name))
            elif item.is_dir():
                # Flatten structure? Or keep subfolders?
                # User said "leave the files in the folder as is".
                # If torrent had subfolders, move them as is.
                dest_dir = target_folder / item.name
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.move(str(item), str(dest_dir))

    def _transfer_standard_mode(self, torrent, temp_path: Path, media_path: Path):
        """Original transfer logic."""
        files_in_temp = list(temp_path.iterdir())

        if len(files_in_temp) == 1 and files_in_temp[0].is_file():
            file = files_in_temp[0]
            target = media_path / file.name
            self.logger.info(f"Transferring single file: {file} -> {target}")
            shutil.move(str(file), target)
        else:
            folder_name = torrent.custom_folder_name if torrent.custom_folder_name else torrent.id
            folder_name = re.sub(r'[<>:"/\\|?*]', "_", folder_name)
            target_folder = media_path / folder_name
            self.logger.info(f"Transferring multiple files to folder: {target_folder}")

            if target_folder.exists():
                self.logger.warning(f"Target folder {target_folder} already exists, removing it first")
                shutil.rmtree(target_folder)

            shutil.move(str(temp_path), str(target_folder))

    def _strip_filename_pattern(self, filename: str, pattern: str) -> str:
        if not pattern or not filename: return filename
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
                                        self.logger.info(f"{dest.name}: {pct:.1f}% @ {rate:.2f} MB/s")
                                        last_logged_percent = int(pct / 20) * 20
                                    if on_progress:
                                        on_progress(pct)
                    return
            except Exception as e:
                self.logger.warning(f"Download attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries: raise
                time.sleep(3 * attempt)