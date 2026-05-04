"""Logging utilities for sqlcg.

All logging is directed to stderr to keep stdout clean for tool output.
"""

import logging
import sys


def getLogger(name: str | None = None) -> logging.Logger:
    """Get a logger instance configured to write to stderr.

    Args:
        name: Logger name (typically __name__ of the calling module)

    Returns:
        A logging.Logger configured with a StreamHandler to sys.stderr.
    """
    logger = logging.getLogger(name)

    # Only add handler if not already configured to avoid duplicates
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    return logger
