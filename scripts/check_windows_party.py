"""Native party window checks plus an optional read-only live game sample.

Uses a separate data directory and no hotkey registrations or game input.
"""

import argparse
import ctypes
import json
import os
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QHelpEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolTip  # noqa: E402

from inkbound_meter.overlay import Overlay  # noqa: E402
from inkbound_meter.party_memory import PartyMemory  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Requires native Windows Python")
    app = QApplication([])
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.WindowFromPoint.argtypes = [wintypes.POINT]
    api.WindowFromPoint.restype = wintypes.HWND
    api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    api.GetAncestor.restype = wintypes.HWND
    api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    api.GetWindowLongW.restype = wintypes.LONG
    report = {}
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inkbound-party-native-") as root:
        window = Overlay(Path(root) / "settings.json", register_hotkeys=False)
        window.move(640, 100)
        party = window.party
        party.move(180, 100)
        data = {
            "source": "memory",
            "phase": "PlayerTurn",
            "turn": 3,
            "status": "Demo party",
            "players": [
                {
                    "id": i,
                    "name": name,
                    "class": character,
                    "hp": hp,
                    "max_hp": maximum,
                    "shield": shield,
                    "will": will,
                    "max_will": 6,
                    "ended_turn": i == 2,
                    "abilities": [
                        {
                            "id": str(slot),
                            "slot": slot,
                            "name": binding,
                            "cooldown": cooldown,
                            "will_crit": slot == 3,
                        }
                        for slot, (binding, cooldown) in enumerate(
                            zip(
                                ("Poison Shot", "Contaminate", "Flurry", "Cleave", "Discharge"),
                                (0, 2, 1, 0, 4),
                                strict=True,
                            )
                        )
                    ],
                }
                for i, name, character, hp, maximum, shield, will in (
                    (1, "Ember", "Magma Miner", 22, 80, 12, 4),
                    (2, "Wisp", "Clairvoyant", 58, 65, 0, 1),
                    (3, "Thorn", "Mosscloak", 43, 50, 8, 6),
                    (4, "A very long player display name", "Star Captain", 0, 72, 0, 0),
                )
            ],
        }
        party.update_memory(data)
        window.show()
        QTest.qWait(100)

        def hit_root():
            point = party.mapToGlobal(QPoint(30, 60))
            return api.GetAncestor(api.WindowFromPoint(wintypes.POINT(point.x(), point.y())), 2)

        assert hit_root() == int(party.winId())
        row, tile = party.rows[1], party.rows[1].tiles[1]
        for width in (300, 360):
            party.setFixedWidth(width)
            party.update_memory(data)
            app.processEvents()
            assert party.height() <= 550
            for item in party.rows.values():
                assert item.geometry().right() < party.width()
                assert item.geometry().bottom() < party.panel.height()
            party.setWindowOpacity(1)
            assert party.grab().save(str(args.screenshots / f"party-four-{width}.png"))
        assert party.rows[1] is row and row.tiles[1] is tile
        report["four_player_layouts"] = [300, 360]
        report["stable_rows"] = True
        point = tile.rect().center()
        QApplication.sendEvent(
            tile, QHelpEvent(QEvent.Type.ToolTip, point, tile.mapToGlobal(point))
        )
        QTest.qWait(200)  # Native Windows tooltip fade-in is asynchronous.
        assert QToolTip.isVisible() and "2 turns" in QToolTip.text()
        QToolTip.hideText()
        report["cooldown_tooltips"] = True
        old = party.pos()
        QTest.mousePress(party.header, Qt.MouseButton.LeftButton, pos=QPoint(95, 10))
        QTest.mouseMove(party.header, QPoint(125, 30))
        QTest.mouseRelease(party.header, Qt.MouseButton.LeftButton, pos=QPoint(125, 30))
        app.processEvents()
        assert party.pos() != old, "Party header did not drag"
        assert window.pos() == QPoint(640, 100)
        report["independent_drag"] = True
        window.set_interactive(False)
        QTest.qWait(50)
        assert api.GetWindowLongW(int(party.winId()), -20) & 0x20
        assert hit_root() != int(party.winId())
        assert party.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        report["native_click_through"] = True
        window.toggle_visibility()
        assert not party.isVisible()
        window.toggle_visibility()
        assert party.isVisible()
        window.recover()
        window.set_opacity(0.6)
        assert abs(party.windowOpacity() - 0.6) < 0.005
        party.close()
        assert window.isVisible() and not party.isVisible()
        menu = window.make_menu()
        next(a for a in menu.actions() if a.text() == "Party overlay").trigger()
        assert party.isVisible()
        report["shared_controls_and_menu_recovery"] = True

        reader = PartyMemory()
        try:
            timings = []
            for _ in range(8):
                begin = time.perf_counter()
                snapshot = reader.sample()
                timings.append(round((time.perf_counter() - begin) * 1000, 2))
            report["live_sample"] = snapshot
            report["sample_ms"] = timings
            if snapshot["source"] == "memory":
                party.update_memory(snapshot)
                app.processEvents()
                party.setWindowOpacity(1)
                party.grab().save(str(args.screenshots / "party-live.png"))
        finally:
            reader.close()
        party.memory_at -= 3
        party.render()
        assert all(not p.tiles for p in party.rows.values())
        report["stale_cooldowns_removed"] = True
        window.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
