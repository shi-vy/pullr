import shutil
import os
import re
from urllib.parse import unquote
from pathlib import Path
from core.states import TorrentState


class FileOps:
    def __init__(self, logger):
        self.logger = logger

    def _strip_filename_pattern(self, filename: str, pattern: str) -> str:
        """Strip a specific pattern from the start of a filename."""
        if not pattern:
            return filename

        # Escape the pattern for regex, but allow flexibility
        escaped_pattern = re.escape(pattern)
        # Regex: Start of string, optional whitespace, pattern, optional whitespace
        regex = f"^{escaped_pattern}\\s*"

        new_name = re.sub(regex, "", filename, flags=re.IGNORECASE)
        if new_name != filename:
            self.logger.info(f"Stripped pattern '{pattern}': {filename} -> {new_name}")
        return new_name

    def start_download(self, torrent, config, on_complete, on_progress=None, on_transfer=None):
        """Starts the download process in a background thread."""
        import threading
        t = threading.Thread(target=self._process_torrent,
                             args=(torrent, config, on_complete, on_progress, on_transfer))
        t.daemon = True
        t.start()

    def _get_dest_info(self, torrent, config):
        """Helper to determine destination path components based on metadata."""
        base_media_path = Path(config["media_path"])
        tmdb_id = getattr(torrent, 'tmdb_id', None)

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
        clean_name = re.sub(r'[<>:"/\\|?*]', " ", torrent.name).strip()
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
                raw_name = url.split("/")[-1].split("?")[0]
                filename = unquote(raw_name)

                if torrent.filename_strip_pattern:
                    filename = self._strip_filename_pattern(filename, torrent.filename_strip_pattern)

                filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
                dest = temp_path / filename
                self.logger.info(f"Downloading file {i}/{len(torrent.direct_links)}: {filename}")
                self._download_file(url, dest,
                                    on_progress=lambda p: on_progress(torrent.id, p) if on_progress else None)

            # --- 2. ROUTING PHASE ---
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
                # Note: If we downloaded a structure like "Season 1/Ep1.mkv", it preserves that structure
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

            # Fallback: Check for single file if folder not found (common for single-file torrents)
            if not source_path.exists() and torrent.files:
                # Try the name of the first file (this matches how _process_torrent handles single files in Unsorted)
                first_file_name = torrent.files[0]['path'].lstrip('/')
                # Sanitize filename as _process_torrent would have
                sanitized_file_name = re.sub(r'[<>:"/\\|?*]', "_", first_file_name)

                potential_path = unsorted_root / sanitized_file_name
                if potential_path.exists():
                    source_path = potential_path

            if not source_path.exists():
                self.logger.error(f"Cannot find source in Unsorted: {source_path}")
                return False

            # 2. Destination Determination (Reuse Helper logic manually or call it if refactored, doing manually here for clarity)
            base_media_path = Path(config["media_path"])
            media_type = getattr(torrent, 'media_type', 'unknown').lower()
            tmdb_id = getattr(torrent, 'tmdb_id', '0')

            if media_type == 'tv':
                dest_subdir = "Shows"
            elif media_type == 'movie':
                dest_subdir = "Movies"
            else:
                dest_subdir = "Others"

            # Naming: Name [tmdbid-ID]
            clean_name = re.sub(r'[<>:"/\\|?*]', " ", torrent.name).strip()
            new_folder_name = f"{clean_name} [tmdbid-{tmdb_id}]"

            dest_folder = base_media_path / dest_subdir / new_folder_name
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

    def _download_file(self, url, dest_path, on_progress=None):
        import requests
        import time

        try:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                total_length = r.headers.get('content-length')

                with open(dest_path, 'wb') as f:
                    if total_length is None:
                        f.write(r.content)
                    else:
                        dl = 0
                        total_length = int(total_length)
                        start_time = time.time()
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                dl += len(chunk)
                                f.write(chunk)
                                if on_progress:
                                    percent = (dl / total_length) * 100
                                    # Basic throttling of progress updates
                                    if int(time.time() * 10) % 5 == 0:
                                        on_progress(percent)
        except Exception as e:
            raise e