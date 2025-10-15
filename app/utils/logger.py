import logging
from pathlib import Path


def running_in_docker() -> bool:
    """Detect if running inside Docker."""
    return Path("/.dockerenv").exists()

def setup_logger(name: str = "pullr", log_to_file: bool = True, log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Determine log directory depending on environment
    if log_dir is None:
        if running_in_docker():
            log_dir = Path("/logs")
        else:
            log_dir = Path.cwd() / "logs"

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler (optional)
    if log_to_file:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "pullr.log")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    # Avoid duplicates
    logger.propagate = False
    logger.info(f"Logger initialized. Writing logs to: {log_dir}")
    return logger
