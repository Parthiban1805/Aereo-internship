import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
    # pymongo is chatty at DEBUG
    logging.getLogger("pymongo").setLevel(logging.WARNING)
