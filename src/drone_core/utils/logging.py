import logging
import sys

def setup(level: str = "INFO") -> None:
    fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
