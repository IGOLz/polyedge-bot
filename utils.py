import logging
import os
import sys


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("polyedge")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Only add file handler when not in Docker
    if not os.path.exists("/.dockerenv"):
        file_handler = logging.FileHandler("bot.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


log = setup_logging()
