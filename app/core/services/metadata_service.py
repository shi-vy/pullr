import re
import json
import requests
from pathlib import Path
from typing import Optional, Dict, Tuple


class MetadataService:
    """
    Handles TMDB metadata fetching and caching for Jellyfin mode.
    """
    TMDB_BASE_URL = "https://api.themoviedb.org/3"
    CACHE_FILE = Path("/config/tv_cache.json")

    def __init__(self, api_key: str, logger):
        self.api_key = api_key
        self.logger = logger
        self.cache: Dict[str, Dict] = self._load_cache()

    def _load_cache(self) -> Dict:
        if self.CACHE_FILE.exists():
            try:
                with open(self.CACHE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to load TV cache: {e}")
        return {}

    def _save_cache(self):
        try:
            with open(self.CACHE_FILE, 'w') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save TV cache: {e}")

    def fetch_metadata(self, tmdb_id: str, media_type: str) -> Optional[str]:
        """
        Fetch title from TMDB.
        Returns: The formatted title (e.g. "Slow Horses") or None if failed.
        """
        if not self.api_key:
            return None

        endpoint = "movie" if media_type.lower() == "movie" else "tv"
        url = f"{self.TMDB_BASE_URL}/{endpoint}/{tmdb_id}?api_key={self.api_key}"

        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # TMDB movies use 'title', TV shows use 'name'
                title = data.get("title") if endpoint == "movie" else data.get("name")
                return title
            else:
                self.logger.error(f"TMDB API Error {resp.status_code}: {resp.text}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to connect to TMDB: {e}")
            return None

    def get_cached_show_id(self, filename: str) -> Optional[Tuple[str, str]]:
        """
        Try to find a cached TMDB ID for a TV show based on filename.
        Returns: (tmdb_id, "tv") or None
        """
        # Heuristic: Extract "Show.Name" from "Show.Name.S01E01"
        # Look for SxxExx or 4-digit year pattern
        match = re.search(r"^(.*?)(?:\.S\d{2}| S\d{2}|\.\d{4}| \d{4})", filename, re.IGNORECASE)
        if match:
            clean_name = match.group(1).replace('.', ' ').strip().lower()
            if clean_name in self.cache:
                entry = self.cache[clean_name]
                self.logger.info(f"Cache hit for '{clean_name}': {entry}")
                return entry.get("tmdb_id"), "tv"

        return None

    def update_cache(self, filename: str, tmdb_id: str):
        """
        Cache a TV show ID for future lookups.
        """
        # Same heuristic extraction
        match = re.search(r"^(.*?)(?:\.S\d{2}| S\d{2}|\.\d{4}| \d{4})", filename, re.IGNORECASE)
        if match:
            clean_name = match.group(1).replace('.', ' ').strip().lower()
            if clean_name not in self.cache:
                self.cache[clean_name] = {"tmdb_id": tmdb_id, "type": "tv"}
                self._save_cache()
                self.logger.info(f"Cached '{clean_name}' -> ID {tmdb_id}")