import requests
import time
import random
from utils.logger import setup_logger

API_BASE = "https://api.real-debrid.com/rest/1.0"


class RealDebridError(Exception):
    pass


class RealDebridClient:
    def __init__(self, api_token: str, logger=None):
        self.api_token = api_token
        self.logger = logger or setup_logger("pullr.realdebrid", log_to_file=False)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_token}"})

    def _request(self, method: str, endpoint: str, **kwargs):
        import random, textwrap
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        backoff = 2
        last_error = None

        for attempt in range(10):
            try:
                resp = self.session.request(method, url, timeout=15, **kwargs)
                # Handle rate limit
                if resp.status_code == 429:
                    self.logger.warning(f"[{endpoint}] Rate limit hit, waiting 5s...")
                    time.sleep(5 + random.uniform(0, 1))
                    continue

                # Handle transient server errors
                if 500 <= resp.status_code < 600:
                    msg = textwrap.shorten(resp.text.strip().replace("\n", " "), width=120)
                    self.logger.warning(
                        f"[{endpoint}] Server error {resp.status_code}: {msg} "
                        f"(attempt {attempt + 1}/5, backoff {backoff}s)"
                    )
                    time.sleep(backoff + random.uniform(0, 1))
                    backoff = min(backoff * 2, 15)
                    last_error = f"{resp.status_code} {msg}"
                    continue

                # Hard failure (non-2xx, non-5xx)
                if not resp.ok:
                    msg = textwrap.shorten(resp.text.strip().replace("\n", " "), width=120)
                    raise RealDebridError(f"{resp.status_code} {msg}")

                # Success
                return resp.json() if resp.text else {}

            except (requests.RequestException, RealDebridError) as e:
                self.logger.warning(
                    f"[{endpoint}] Exception on attempt {attempt + 1}/5: {e}"
                )
                last_error = str(e)
                time.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 15)

        raise RealDebridError(
            f"Failed to reach RealDebrid after retries: {endpoint} — last error: {last_error}"
        )

    def get_user(self):
        """Validate API token and fetch user info."""
        return self._request("GET", "/user")

    def add_magnet(self, magnet_link: str) -> dict:
        """Add a magnet link to RealDebrid."""
        return self._request("POST", "/torrents/addMagnet", data={"magnet": magnet_link})

    def get_torrent_info(self, torrent_id: str) -> dict:
        """Fetch info for a given torrent."""
        return self._request("GET", f"/torrents/info/{torrent_id}")

    def list_torrent_files(self, torrent_id: str) -> dict:
        """Fetch the list of files available in the torrent."""
        return self._request("GET", f"/torrents/info/{torrent_id}")

    def select_torrent_files(self, torrent_id: str, file_ids: list[int]):
        """Select which files RealDebrid should download."""
        data = {"files": ",".join(map(str, file_ids))}
        return self._request("POST", f"/torrents/selectFiles/{torrent_id}", data=data)

    def unrestrict_link(self, link: str) -> dict:
        """Convert a RealDebrid link into a direct download URL."""
        return self._request("POST", "/unrestrict/link", data={"link": link})

    def select_files(self, torrent_id: str, files: str) -> dict:
        """Tell RealDebrid which files to download (can use 'all' or comma-separated IDs)."""
        return self._request("POST", f"/torrents/selectFiles/{torrent_id}", data={"files": files})

    def delete_torrent(self, torrent_id: str) -> dict:
        """Delete a torrent from RealDebrid once it's done."""
        return self._request("DELETE", f"/torrents/delete/{torrent_id}")

    def delete_download(self, download_id: str) -> dict:
        """Delete a download from RealDebrid account."""
        return self._request("DELETE", f"/downloads/delete/{download_id}")

    def list_torrents(self) -> list:
        """Get list of all torrents in RealDebrid account."""
        return self._request("GET", "/torrents")