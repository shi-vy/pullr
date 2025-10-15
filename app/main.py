import uvicorn
from utils.config import Config
from utils.logger import setup_logger
from core.realdebrid import RealDebridClient
from core.manager import TorrentManager
from web import server as web_server
from web.server import app as web_app


def main():
    # Load config
    config = Config()
    logger = setup_logger("pullr", log_to_file=config.log_to_file)
    logger.info("Pullr starting up...")

    # Authenticate with RealDebrid
    rd = RealDebridClient(config.realdebrid_api_token, logger)
    user_info = rd.get_user()
    logger.info(f"Authenticated as: {user_info.get('username')}")

    # Start Torrent Manager (background polling loop)
    manager = TorrentManager(rd, logger, config.poll_interval_seconds, config.data)
    manager.start()

    # Attach manager + logger to web server module for shared state
    web_server.torrent_manager = manager
    web_server.logger = logger

    # Start the web dashboard
    logger.info(f"Starting web dashboard on port {config.port}...")
    uvicorn.run(web_app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
