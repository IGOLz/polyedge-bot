import logging
import os
import sys

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)


class ColoredFormatter(logging.Formatter):
    """Console formatter that colors log lines based on message content.

    Message-specific colors take priority over log-level colors, so an INFO
    line containing "WIN" will print green, not white.
    """

    LEVEL_COLORS = {
        "CRITICAL": Fore.RED + Style.BRIGHT,
        "ERROR": Fore.RED + Style.BRIGHT,
        "WARNING": Fore.YELLOW + Style.BRIGHT,
        "INFO": Fore.WHITE,
        "DEBUG": Style.DIM + Fore.WHITE,
    }

    # Checked in order — first match wins.
    MESSAGE_COLORS: list[tuple[str, str]] = [
        # Critical / errors
        ("CRITICAL", Fore.RED + Style.BRIGHT),
        ("FAILED", Fore.RED + Style.BRIGHT),
        # Trade outcomes
        ("TRADE FILLED", Fore.GREEN + Style.BRIGHT),
        ("TRADE EXECUTED", Fore.GREEN + Style.BRIGHT),
        ("FILL PRICE REJECTED", Fore.RED + Style.BRIGHT),
        ("WIN", Fore.GREEN),
        ("LOSS", Fore.RED),
        ("STOP-LOSS", Fore.YELLOW),
        # Strategy signals
        ("[M3]", Fore.CYAN + Style.BRIGHT),
        ("M3 SIGNAL", Fore.CYAN + Style.BRIGHT),
        ("M3 THESIS", Fore.CYAN),
        ("[M4]", Fore.MAGENTA + Style.BRIGHT),
        ("M4 SIGNAL", Fore.MAGENTA + Style.BRIGHT),
        ("M4 THESIS", Fore.MAGENTA),
        ("SIGNAL", Fore.YELLOW + Style.BRIGHT),
        # Operational
        ("[CONFIG]", Fore.BLUE + Style.BRIGHT),
        ("USDC balance", Fore.BLUE),
        ("balance", Fore.BLUE),
        ("[HEARTBEAT]", Style.DIM + Fore.WHITE),
        ("Proxy", Fore.YELLOW),
        ("PostgreSQL", Fore.CYAN),
        ("[DRY RUN]", Fore.YELLOW + Style.BRIGHT),
        ("[BET]", Fore.GREEN),
        ("[TIMING]", Style.DIM + Fore.WHITE),
        ("FOK no fill", Style.DIM + Fore.YELLOW),
        # Hybrid execution
        ("[EXEC] ✅", Fore.GREEN + Style.BRIGHT),
        ("[EXEC] ❌", Fore.RED + Style.BRIGHT),
        ("[EXEC] ⏱️", Style.DIM + Fore.YELLOW),
        ("[EXEC]", Fore.CYAN),
        ("EXEC METRICS", Fore.BLUE),
        ("VARIANCE METRICS", Fore.BLUE),
        ("] LOCKED:", Fore.CYAN),
        ("High price variance", Fore.YELLOW + Style.BRIGHT),
        ("Share variance", Fore.YELLOW + Style.BRIGHT),
        ("Hybrid no fill", Style.DIM + Fore.YELLOW),
    ]

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()

        # Pick message-specific color (first match), fallback to level color
        color = self.LEVEL_COLORS.get(record.levelname, Fore.WHITE)
        for key, c in self.MESSAGE_COLORS:
            if key in msg:
                color = c
                break

        ts = Style.DIM + Fore.WHITE + self.formatTime(record, "%Y-%m-%d %H:%M:%S") + Style.RESET_ALL
        level = self.LEVEL_COLORS.get(record.levelname, "") + f"{record.levelname:<8}" + Style.RESET_ALL
        body = color + msg + Style.RESET_ALL

        return f"[{ts}] {level} {body}"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("polyedge")
    logger.setLevel(logging.DEBUG)

    # Console: colored
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ColoredFormatter())
    logger.addHandler(console)

    # File: plain (skip in Docker)
    if not os.path.exists("/.dockerenv"):
        plain_fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
        file_handler = logging.FileHandler("bot.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(plain_fmt)
        logger.addHandler(file_handler)

    return logger


def setup_debug_logging() -> logging.Logger:
    """Separate logger for signal debug lines → debug_signals.log only."""
    logger = logging.getLogger("polyedge.debug")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # Don't bubble up to main logger

    if not os.path.exists("/.dockerenv"):
        plain_fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
        fh = logging.FileHandler("debug_signals.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(plain_fmt)
        logger.addHandler(fh)

    return logger


log = setup_logging()
debug_log = setup_debug_logging()
