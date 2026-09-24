"""Exercise native hotkey delivery, hit testing and live UI updates without game input.

Run with Windows Python from the checkout. No keystrokes are sent to the game:
WM_HOTKEY messages go only to this test's own UI thread.
"""

import argparse
import ctypes
import json
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QToolTip, QWidget

from inkbound_meter.charts import ComponentBar
from inkbound_meter.overlay import Collector, Overlay, payload
from inkbound_meter.service import replay


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This check requires native Windows Python and the windows Qt platform.")
    app = QApplication([])
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    report = {}
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/first_encounter.txt"
    with tempfile.TemporaryDirectory(prefix="inkbound-native-") as directory:
        root = Path(directory)
        log = root / "game.txt"
        log.write_bytes(fixture.read_bytes())
        target = QWidget()
        target.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        target.setGeometry(60, 80, 600, 650)
        target.show()
        # Leave the user's live F8/F9 bindings alone if their meter is running.
        (root / "settings.json").write_text(
            json.dumps({"interaction_key": "Ctrl+Shift+F10", "visibility_key": "Ctrl+Shift+F11"})
        )
        window = Overlay(root / "settings.json")
        window.move(100, 110)
        window.attach(Collector(log, root / "meter.sqlite3"))
        window.show()

        def check(condition, message):
            if not condition:
                raise AssertionError(message)

        def hit_root():
            point = window.mapToGlobal(QPoint(60, 95))
            hwnd = user32.WindowFromPoint(wintypes.POINT(point.x(), point.y()))
            return user32.GetAncestor(hwnd, 2)

        def press(identifier):
            check(user32.PostThreadMessageW(thread_id, 0x0312, identifier, 0), "PostThreadMessage")

        def step_first():
            check(not window.hotkey_error, window.hotkey_error)
            check(window.data["run"]["party_damage"] == 5522, "Live fixture total")
            report["native_hotkeys_registered"] = True
            report["live_fixture_total"] = 5522
            check(hit_root() == int(window.winId()), "Unlocked overlay receives hit testing")
            report["unlocked_hit_test"] = True
            press(1)

        def step_locked():
            check(not window.interactive, "F8 message did not lock")
            style = user32.GetWindowLongW(int(window.winId()), -20)
            check(style & 0x20, "WS_EX_TRANSPARENT missing")
            check(hit_root() != int(window.winId()), "Locked overlay still catches mouse hits")
            report["click_through_hit_test"] = True
            report["locked_does_not_accept_focus"] = bool(
                window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
            )
            press(2)

        def step_hidden():
            check(not window.isVisible(), "F9 message did not hide")
            press(2)
            press(1)

        started = [0.0]
        components = replay(fixture.with_name("frostbite_components.txt"))
        component_key = (17, "FrostBite_Damage_StatusEffect_Action")

        def step_append():
            check(window.isVisible() and window.interactive, "Hotkey recovery failed")
            report["hotkey_hide_show_unlock"] = True
            window.select_scope("run")
            window.toggle_player(9)
            event = next(
                line
                for line in fixture.read_text().splitlines()
                if "broadcasting EventOnUnitDamaged-" in line
            )
            # Repeat one real 23-damage event at a new file position.
            check("DamageAmount:23-" in event, "Unexpected fixture amount")
            started[0] = time.monotonic()
            with log.open("a", encoding="utf-8") as stream:
                stream.write(event + "\n")

        def step_updated():
            check(window.data["run"]["party_damage"] == 5545, "Live append not reflected")
            report["live_append_total"] = 5545
            report["live_append_verified_within_ms"] = round((time.monotonic() - started[0]) * 1000)
            check(
                window.scroll.geometry().bottom() < window.panel.height(),
                "Expanded rows extend outside the panel",
            )
            report["expanded_layout_no_overlap"] = True
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            check(window.grab().save(str(args.screenshot)), "Screenshot save")

        def step_status_tooltip():
            point = window.status.rect().center()
            QApplication.sendEvent(
                window.status,
                QHelpEvent(QEvent.Type.ToolTip, point, window.status.mapToGlobal(point)),
            )

        def step_components():
            check(QToolTip.isVisible(), "Status indicator tooltip is not visible")
            check("Following game log" in QToolTip.text(), "Status tooltip lacks capture state")
            report["compact_status_tooltip_visible"] = True
            check(abs(window.windowOpacity() - 0.5) < 0.005, "Default opacity is not 50%")
            report["default_opacity_50_percent"] = True
            QToolTip.hideText()
            window.update_data(payload(components, "replay"))
            window.toggle_player(17)
            window.toggle_source(component_key)
            window.select_source_mode(component_key, True)

        def step_tooltip():
            bar = window.scroll.widget().findChild(ComponentBar)
            check(bar is not None and not bar.compact, "Expanded component chart missing")
            check(round(bar.positive, 2) == 387, "Component chart total")
            report["component_hit_bar"] = True
            check(not window.isActiveWindow(), "Overlay unexpectedly took foreground focus")
            point = QPoint(15, 10)
            QApplication.sendEvent(
                bar, QHelpEvent(QEvent.Type.ToolTip, point, bar.mapToGlobal(point))
            )

        def step_stats():
            # Native tooltip creation is asynchronous; check on a later event-loop turn.
            check(QToolTip.isVisible(), "Tooltip is not visible on the inactive overlay")
            check("Base effect" in QToolTip.text(), "Wrong component tooltip")
            report["inactive_component_tooltip_visible"] = True
            QToolTip.hideText()
            QTest.mouseClick(
                window.scroll.widget().findChild(QPushButton, "disclosure"),
                Qt.MouseButton.LeftButton,
            )

        def step_stats_visible():
            body = window.scroll.widget()
            panel = body.findChild(QFrame, "hitStats")
            check(panel is not None and panel.isVisible(), "Click did not open hit stats")
            check("207" in [v.text() for v in panel.findChildren(QLabel)], "Wrong stat snapshot")
            window.update_data(payload(components, "replay"))
            check(window.scroll.widget() is body, "Unchanged refresh destroyed the hovered rows")
            check(panel.isVisible(), "Stats collapsed after refresh")
            report["clickable_stats_survive_refresh"] = True
            window.settings.width = 300
            window.setFixedWidth(300)
            window.render()

        def step_narrow():
            check(
                window.scroll.geometry().bottom() < window.panel.height(), "Narrow layout overflow"
            )
            window.scroll.verticalScrollBar().setValue(window.scroll.verticalScrollBar().maximum())
            check(
                window.grab().save(str(args.screenshot.with_stem(args.screenshot.stem + "-stats"))),
                "Stats screenshot save",
            )
            report["narrow_stats_layout"] = True

        def finish():
            window.close()
            target.close()
            app.quit()

        def guarded(callback):
            def call():
                try:
                    callback()
                except Exception as exc:
                    report["error"] = str(exc)
                    finish()

            return call

        for delay, callback in [
            (900, step_first),
            (1150, step_locked),
            (1400, step_hidden),
            (1650, step_append),
            (2100, step_updated),
            (2300, step_status_tooltip),
            (2550, step_components),
            (2800, step_tooltip),
            (3050, step_stats),
            (3300, step_stats_visible),
            (3550, step_narrow),
            (3750, finish),
        ]:
            QTimer.singleShot(delay, guarded(callback))
        app.exec()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
