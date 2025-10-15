from utils.config import Config
from utils.logger import setup_logger
from core.realdebrid import RealDebridClient
from core.manager import TorrentManager
from web.server import app as web_app
from web import server as web_server
import uvicorn
import time

def main():
    config = Config()
    logger = setup_logger(log_to_file=config.get("log_to_file", True))
    logger.info("Pullr starting up...")

    rd = RealDebridClient(config["realdebrid_api_token"], logger)
    user_info = rd.get_user()
    logger.info(f"Authenticated as: {user_info.get('username')}")

    manager = TorrentManager(rd, logger, poll_interval=config["poll_interval_seconds"], config_data=config.data)
    manager.start()

    magnet = input("\nEnter magnet link: ").strip()
    torrent_id = manager.add_magnet(magnet)
    if not torrent_id:
        return

    logger.info(f"Fetching torrent file list for {torrent_id}...")
    info = rd.list_torrent_files(torrent_id)

    files = info.get("files", [])
    if not files:
        logger.warning("No files found for this torrent.")
        return

    # Display file list
    print("\nAvailable files:")
    for f in files:
        print(f"  [{f['id']}] {f['path']} ({round(f['bytes'] / 1_000_000, 2)} MB)")

    # Ask user which to select
    choice = input("\nSelect file IDs (comma-separated or 'all'): ").strip().lower()
    if choice == "all":
        selected_ids = [f["id"] for f in files]
    else:
        selected_ids = [int(x.strip()) for x in choice.split(",") if x.strip().isdigit()]

    rd.select_torrent_files(torrent_id, selected_ids)
    logger.info(f"Selected {len(selected_ids)} files for torrent {torrent_id}.")

    # Let polling loop continue for a bit
    logger.info("Monitoring torrent progress...")
    time.sleep(15)
    logger.info(f"Current statuses: {manager.get_status()}")
    manager.stop()

if __name__ == "__main__":
    config = Config()
    logger = setup_logger("pullr", log_to_file=config.log_to_file)
    rd = RealDebridClient(config.realdebrid_api_token, logger)
    manager = TorrentManager(rd, logger, config.poll_interval_seconds, config.data)
    manager.start()

    web_server.torrent_manager = manager
    web_server.logger = logger

    logger.info(f"Starting web dashboard on port {config.port}...")
    uvicorn.run(web_app, host="0.0.0.0", port=config.port)
