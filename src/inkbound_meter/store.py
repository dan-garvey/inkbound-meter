"""An append-only normalized event journal with atomic reader checkpoints."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .parser import PARSER_VERSION, Event


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise ValueError(f"Unsupported meter database version {version}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS streams (
                path TEXT PRIMARY KEY, generation TEXT NOT NULL, identity TEXT NOT NULL,
                anchor_size INTEGER NOT NULL, anchor_hash TEXT NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0, tail_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, generation TEXT NOT NULL,
                byte_offset INTEGER NOT NULL, kind TEXT NOT NULL,
                timestamp TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE(generation, byte_offset)
            );
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(streams)")}
        if "parser_version" not in columns:
            self.db.execute("ALTER TABLE streams ADD COLUMN parser_version INTEGER DEFAULT 1")
        self.db.execute("PRAGMA user_version=2")
        self.db.commit()

    def cursor(self, path: str) -> dict | None:
        row = self.db.execute("SELECT * FROM streams WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def begin(self, path: str, identity: str, anchor: bytes) -> tuple[dict, Event]:
        from hashlib import sha256

        generation = uuid.uuid4().hex
        event = Event("source_started", data={"generation": generation})
        with self.db:
            self.db.execute(
                """
                INSERT INTO streams VALUES(?,?,?,?,?,0,'',?)
                ON CONFLICT(path) DO UPDATE SET generation=excluded.generation,
                identity=excluded.identity, anchor_size=excluded.anchor_size,
                anchor_hash=excluded.anchor_hash, offset=0, tail_hash='',
                parser_version=excluded.parser_version
                """,
                (
                    path,
                    generation,
                    identity,
                    len(anchor),
                    sha256(anchor).hexdigest(),
                    PARSER_VERSION,
                ),
            )
            self.db.execute(
                """
                INSERT INTO events(generation,byte_offset,kind,timestamp,data) VALUES(?,?,?,?,?)
                """,
                (generation, -1, event.kind, "", json.dumps(event.data)),
            )
        cursor = self.cursor(path)
        assert cursor is not None
        return cursor, event

    def append(
        self, cursor: dict, records: list[tuple[int, Event]], offset: int, tail_hash: str
    ) -> list[Event]:
        inserted = []
        with self.db:
            for position, event in records:
                result = self.db.execute(
                    """
                    INSERT OR IGNORE INTO events(generation,byte_offset,kind,timestamp,data)
                    VALUES(?,?,?,?,?)
                    """,
                    (
                        cursor["generation"],
                        position,
                        event.kind,
                        event.timestamp,
                        json.dumps(event.data, ensure_ascii=False),
                    ),
                )
                if result.rowcount:
                    inserted.append(event)
            self.db.execute(
                "UPDATE streams SET offset=?, tail_hash=? WHERE path=?",
                (offset, tail_hash, cursor["path"]),
            )
        return inserted

    def events(self):
        # Backfilled stat events belong at their original byte positions, inside
        # their original generation. Never append them after historical damage.
        for row in self.db.execute("""
            WITH generations AS (
                SELECT generation, MIN(sequence) AS first FROM events GROUP BY generation
            )
            SELECT e.kind,e.timestamp,e.data FROM events e
            JOIN generations g ON e.generation=g.generation
            ORDER BY g.first,e.byte_offset
        """):
            yield Event(row["kind"], row["timestamp"], json.loads(row["data"]))

    def enrich(self, cursor: dict, records: list[tuple[int, Event]]) -> bool:
        """Atomically enrich a verified unchanged prefix; preserve every old fact.

        A mismatch declines enrichment without touching damage or checkpoints.
        The reader can continue capturing new lines with the new parser.
        """
        old = {
            row["byte_offset"]: row
            for row in self.db.execute(
                "SELECT * FROM events WHERE generation=? AND byte_offset>=0",
                (cursor["generation"],),
            )
        }
        new = dict(records)
        compatible = all(
            pos in new
            and row["kind"] == new[pos].kind
            and row["timestamp"] == new[pos].timestamp
            and all(new[pos].data.get(k) == v for k, v in json.loads(row["data"]).items())
            for pos, row in old.items()
        )
        with self.db:
            if compatible:
                for pos, event in records:
                    self.db.execute(
                        """
                        INSERT INTO events(generation,byte_offset,kind,timestamp,data)
                        VALUES(?,?,?,?,?) ON CONFLICT(generation,byte_offset)
                        DO UPDATE SET data=excluded.data
                    """,
                        (
                            cursor["generation"],
                            pos,
                            event.kind,
                            event.timestamp,
                            json.dumps(event.data, ensure_ascii=False),
                        ),
                    )
            self.db.execute(
                "UPDATE streams SET parser_version=? WHERE path=?", (PARSER_VERSION, cursor["path"])
            )
        return compatible

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
