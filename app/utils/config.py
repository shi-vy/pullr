import os
import yaml
from pathlib import Path

DEFAULT_PATHS = [
    Path("/config/config.yaml"),
    Path(__file__).parent.parent.parent / "config" / "config.yaml",
]


def running_in_docker() -> bool:
    """Detect if running inside a Docker container."""
    return Path("/.dockerenv").exists()


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or self._find_config_path()
        self.data = self._load_config()
        self._validate_config()
        self._prepare_directories()

    def _find_config_path(self) -> Path:
        for p in DEFAULT_PATHS:
            if p.exists():
                return p
        raise ConfigError(f"Could not find config.yaml in any expected locations: {DEFAULT_PATHS}")

    def _load_config(self) -> dict:
        if not self.path.exists():
            raise ConfigError(f"Config file not found at {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise ConfigError(f"Invalid YAML in config: {e}")
        return data or {}

    def _validate_config(self):
        required = [
            "realdebrid_api_token",
            "poll_interval_seconds",
            "download_temp_path",
            "media_path",
            "port",
        ]
        missing = [key for key in required if key not in self.data]
        if missing:
            raise ConfigError(f"Missing required config keys: {', '.join(missing)}")

        # Validate external_torrent_scan_interval_seconds if present
        scan_interval = self.data.get("external_torrent_scan_interval_seconds")
        if scan_interval is not None and not isinstance(scan_interval, (int, float)):
            raise ConfigError("external_torrent_scan_interval_seconds must be a number")

        # Validate Jellyfin mode requirements
        if self.data.get("jellyfin_mode", False):
            if not self.data.get("tmdb_api_key"):
                raise ConfigError("tmdb_api_key is required when jellyfin_mode is enabled")

    def _prepare_directories(self):
        temp_path = Path(self.data["download_temp_path"])
        media_path = Path(self.data["media_path"])
        # Use config value or default to a subdirectory
        unsorted_path = Path(self.data.get("unsorted_path", media_path / "unsorted"))
        logs_path = Path("/logs")

        # If running locally (not Docker), map paths
        if not running_in_docker():
            if str(temp_path).startswith("/downloads"):
                temp_path = Path.cwd() / "downloads/tmp"
            if str(media_path).startswith("/media"):
                media_path = Path.cwd() / "media"
                # Update unsorted path relative to new media path if it wasn't explicitly set
                if "unsorted_path" not in self.data:
                    unsorted_path = media_path / "unsorted"
            logs_path = Path.cwd() / "logs"

            self.data["download_temp_path"] = str(temp_path)
            self.data["media_path"] = str(media_path)
            self.data["unsorted_path"] = str(unsorted_path) # Store resolved path

        for p in [temp_path, media_path, logs_path, unsorted_path]:
            p.mkdir(parents=True, exist_ok=True)

        # If Jellyfin mode, ensure subfolders exist
        if self.jellyfin_mode:
            (media_path / "Shows").mkdir(exist_ok=True)
            (media_path / "Movies").mkdir(exist_ok=True)

        # Verify media path writable
        test_file = Path(self.data["media_path"]) / ".pullr_write_test"
        try:
            with open(test_file, "w") as f:
                f.write("test")
            test_file.unlink()
        except Exception as e:
            raise ConfigError(f"Media path '{self.data['media_path']}' is not writable: {e}")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    @property
    def realdebrid_api_token(self) -> str:
        token = self.data.get("realdebrid_api_token")
        if not token:
            raise ConfigError("Missing realdebrid_api_token in config.yaml")
        return token

    @property
    def poll_interval_seconds(self) -> int:
        return int(self.data.get("poll_interval_seconds", 30))

    @property
    def download_temp_path(self) -> str:
        return self.data.get("download_temp_path")

    @property
    def media_path(self) -> str:
        return self.data.get("media_path")

    @property
    def unsorted_path(self) -> str:
        return self.data.get("unsorted_path")

    @property
    def port(self) -> int:
        return int(self.data.get("port", 8080))

    @property
    def log_to_file(self) -> bool:
        return bool(self.data.get("log_to_file", True))

    @property
    def external_torrent_scan_interval_seconds(self) -> int:
        """Get external torrent scan interval. Returns 0 if disabled."""
        interval = self.data.get("external_torrent_scan_interval_seconds", 15)
        if interval is None:
            return 0
        return int(interval) if interval > 0 else 0

    @property
    def jellyfin_mode(self) -> bool:
        return bool(self.data.get("jellyfin_mode", False))

    @property
    def tmdb_api_key(self) -> str | None:
        return self.data.get("tmdb_api_key")

    def __repr__(self):
        return f"<Config path={self.path} keys={list(self.data.keys())}>"