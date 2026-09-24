"""PyInstaller entry point; the package also supports python -m inkbound_meter."""

import logging
import sys
from logging.handlers import RotatingFileHandler

from inkbound_meter.cli import main
from inkbound_meter.config import data_directory


def launch():
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[
            RotatingFileHandler(
                directory / "errors.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
        ],
    )

    def exception_hook(kind, value, traceback):
        logging.error("Unexpected error", exc_info=(kind, value, traceback))
        if sys.stderr:
            sys.__excepthook__(kind, value, traceback)

    sys.excepthook = exception_hook
    return main()


if __name__ == "__main__":
    raise SystemExit(launch())
