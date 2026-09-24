"""Platform-independent live/replay entry points."""

from __future__ import annotations

from pathlib import Path

from .fileio import CaptureLock
from .model import Meter
from .parser import Event, parse_safe
from .reader import LogReader
from .store import Store


def replay(path: Path) -> Meter:
    meter = Meter()
    meter.apply(Event("source_started"))
    with path.open("rb") as stream:
        for raw in stream:
            # A final fragment may still be in the middle of being written.
            if not raw.endswith(b"\n"):
                break
            event = parse_safe(raw.decode("utf-8", errors="replace"))
            if event:
                meter.apply(event)
    return meter


class Session:
    def __init__(self, log_path: Path, database: Path | str) -> None:
        self.lock = (
            None
            if str(database) == ":memory:"
            else CaptureLock(Path(database).with_suffix(".lock"))
        )
        self.store = None
        try:
            self.store = Store(database)
            self.meter = Meter()
            for event in self.store.events():
                self.meter.apply(event)
            self.reader = LogReader(log_path, self.store)
            self.status = "starting"
        except Exception:
            self.close()
            raise

    def poll(self):
        result = self.reader.poll()
        if result.rebuild:
            self.meter = Meter()
        for event in self.store.events() if result.rebuild else result.events:
            self.meter.apply(event)
        self.status = "catching up" if result.catching_up else result.status
        return result

    def close(self) -> None:
        if self.store:
            self.store.close()
        if self.lock:
            self.lock.close()
