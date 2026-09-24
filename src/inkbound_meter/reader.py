"""Incremental binary log reads; checkpoints always end after a complete line."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .fileio import open_log
from .parser import PARSER_VERSION, Event, parse_safe
from .store import Store


@dataclass
class Poll:
    events: list[Event] = field(default_factory=list)
    bytes_read: int = 0
    catching_up: bool = False
    status: str = "live"
    rebuild: bool = False


class LogReader:
    CHUNK = 2 * 1024 * 1024

    def __init__(self, path: Path, store: Store) -> None:
        self.path = path.resolve()
        self.key = os.path.normcase(str(self.path))
        self.store = store

    def poll(self) -> Poll:
        # Open anew to allow Windows to rotate/replace the log between reads.
        try:
            stream = open_log(self.path)
        except FileNotFoundError:
            return Poll(status="waiting for log")
        with stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size == 0:
                return Poll(status="waiting for game")
            identity = f"{stat.st_dev}:{stat.st_ino}"
            cursor = self.store.cursor(self.key)
            changed = cursor is None or cursor["identity"] != identity
            if cursor and not changed:
                anchor = stream.read(cursor["anchor_size"])
                changed = (
                    stat.st_size < cursor["offset"]
                    or sha256(anchor).hexdigest() != cursor["anchor_hash"]
                )
                if not changed and cursor["offset"]:
                    stream.seek(max(0, cursor["offset"] - 512))
                    tail = stream.read(min(512, cursor["offset"]))
                    changed = sha256(tail).hexdigest() != cursor["tail_hash"]
            result = Poll()
            if changed:
                stream.seek(0)
                cursor, event = self.store.begin(self.key, identity, stream.read(256))
                result.events.append(event)
            assert cursor is not None
            if cursor["parser_version"] < PARSER_VERSION:
                stream.seek(0)
                records = []
                position = 0
                while position < cursor["offset"]:
                    raw = stream.readline(min(self.CHUNK, cursor["offset"] - position))
                    if not raw.endswith(b"\n"):
                        break
                    event = parse_safe(raw.decode("utf-8", errors="replace"))
                    if event:
                        records.append((position, event))
                    position += len(raw)
                # A file may rotate during enrichment. Recheck the prefix tail
                # and identity before committing any historical annotations.
                stream.seek(max(0, cursor["offset"] - 512))
                tail = stream.read(min(512, cursor["offset"]))
                if position != cursor["offset"] or (
                    position and sha256(tail).hexdigest() != cursor["tail_hash"]
                ):
                    return Poll(status="log changed during history update", catching_up=True)
                result.rebuild = self.store.enrich(cursor, records)
                if not result.rebuild:
                    result.status = "history details unavailable; capturing new events"
            start = cursor["offset"]
            stream.seek(start)
            chunk = stream.read(self.CHUNK)
            end = chunk.rfind(b"\n") + 1
            if not end:
                if len(chunk) == self.CHUNK:
                    raise ValueError("Unsupported log: a single line exceeds 2 MiB")
                return result
            complete = chunk[:end]
            position = start
            records = []
            for raw in complete.splitlines(keepends=True):
                event = parse_safe(raw.decode("utf-8", errors="replace"))
                if event:
                    records.append((position, event))
                position += len(raw)
            stream.seek(max(0, position - 512))
            tail_hash = sha256(stream.read(min(position, 512))).hexdigest()
            result.events.extend(self.store.append(cursor, records, position, tail_hash))
            result.bytes_read = end
            result.catching_up = stat.st_size - position > 0 and len(chunk) == self.CHUNK
            return result
