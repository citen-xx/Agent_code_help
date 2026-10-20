"""Central logging configuration."""

import logging
import sys

from app.core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: int | None = None) -> None:
    """Configure root logging.

    Called once at application startup; individual modules simply use
    ``logging.getLogger(__name__)`` afterwards.
    """
    if level is None:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    # Keep third-party libraries at INFO; only our own modules are
    # verbose in development.
    if settings.APP_ENV.lower() == "development":
        logging.getLogger("app").setLevel(logging.DEBUG)
