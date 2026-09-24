"""Independent compact party window; no capture journal or hotkey registrations."""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .party_memory import PartyMemory


def text_label(text="", name=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


class PartyCollector(QThread):
    updated = Signal(dict)

    def run(self):
        reader = PartyMemory()
        try:
            while not self.isInterruptionRequested():
                self.updated.emit(reader.sample())
                self.msleep(250)
        except Exception:
            self.updated.emit(
                {
                    "source": "unavailable",
                    "players": [],
                    "status": "Party reader stopped; using log values",
                }
            )
        finally:
            reader.close()


class ResourceBar(QWidget):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.current = self.maximum = self.shield = None
        self.setFixedHeight(21 if kind == "health" else 18)

    def set_values(self, current, maximum, shield=None):
        self.current, self.maximum, self.shield = current, maximum, shield
        amount = "—" if current is None else str(current)
        cap = "—" if maximum is None else str(maximum)
        unit = "HP" if self.kind == "health" else "Will"
        tooltip = f"{amount} / {cap} {unit}"
        if self.kind == "health":
            tooltip += f" · {'—' if shield is None else shield} shield"
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        value = "—" if self.current is None else str(self.current)
        maximum = "—" if self.maximum is None else str(self.maximum)
        if self.kind == "health":
            rect = QRectF(0, 3, self.width(), 17)
            painter.setBrush(QColor("#273242"))
            painter.drawRoundedRect(rect, 4, 4)
            if self.current is not None and self.maximum:
                fraction = min(1, max(0, self.current / self.maximum))
                painter.setBrush(QColor("#a74e5c" if fraction <= 0.3 else "#337d69"))
                painter.drawRoundedRect(QRectF(0, 3, self.width() * fraction, 17), 4, 4)
            if self.shield and self.maximum:
                painter.setBrush(QColor("#8bc4f2"))
                painter.drawRoundedRect(
                    QRectF(0, 0, self.width() * min(1, self.shield / self.maximum), 3), 1, 1
                )
            painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{value} / {maximum}")
            if self.shield:
                painter.setPen(QColor("#b8dcff"))
                painter.drawText(
                    rect.adjusted(4, 0, -5, 0),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"+{self.shield} shield",
                )
        else:
            slots = min(12, max(1, self.maximum or 0, self.current or 0))
            right = min(self.width() * 0.60, 180)
            step = right / slots
            for index in range(slots):
                painter.setBrush(
                    QColor(
                        "#baa6ef"
                        if self.current is not None and index < self.current
                        else "#303243"
                    )
                )
                painter.drawRoundedRect(QRectF(index * step, 5, max(2, step - 4), 8), 2, 2)
            painter.setPen(QColor("#d7cafa"))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value} / {maximum} Will",
            )


class BindingTile(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = {}
        self.setMinimumWidth(0)
        self.setFixedHeight(48)

    def set_data(self, data):
        self.data = data
        cd = data.get("cooldown")
        status = (
            "Cooldown unknown"
            if cd is None
            else ("Off cooldown" if cd == 0 else f"{cd} turn{'s' if cd != 1 else ''} remaining")
        )
        tip = f"{data['name']}\n{status}"
        if data.get("will_crit"):
            tip += "\nGuaranteed critical hit"
        if cd == 0:
            tip += "\nWill, range and other casting requirements still apply."
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cd = self.data.get("cooldown")
        color = QColor("#86d7c0" if cd == 0 else "#e2b574" if cd else "#8997aa")
        painter.setBrush(QColor("#1d3333" if cd == 0 else "#273040"))
        painter.setPen(QPen(QColor("#d9c28d") if self.data.get("will_crit") else color.darker(190)))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 5, 5)
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QColor("#dbe3ee"))
        title = self.data.get("name", "Binding")
        title = painter.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, self.width() - 6
        )
        painter.drawText(QRectF(3, 2, self.width() - 6, 16), Qt.AlignmentFlag.AlignCenter, title)
        painter.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        painter.setPen(color)
        painter.drawText(
            QRectF(0, 17, self.width(), 27),
            Qt.AlignmentFlag.AlignCenter,
            "✓" if cd == 0 else "—" if cd is None else str(cd),
        )


class PartyRow(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("partyRow")
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(3)
        heading = QHBoxLayout()
        self.name = text_label(name="partyName")
        self.name.setStyleSheet("font-weight:600;font-size:13px;")
        self.character = text_label(name="muted")
        self.ready = text_label(name="partyReady")
        heading.addWidget(self.name, 1)
        heading.addWidget(self.character)
        heading.addWidget(self.ready)
        box.addLayout(heading)
        self.health = ResourceBar("health")
        self.will = ResourceBar("will")
        box.addWidget(self.health)
        box.addWidget(self.will)
        self.bindings = QHBoxLayout()
        self.bindings.setSpacing(4)
        box.addLayout(self.bindings)
        self.unavailable = text_label("Cooldowns unavailable", "muted")
        box.addWidget(self.unavailable)
        self.tiles = []

    def set_data(self, data, phase):
        full_name = data.get("name") or f"Player {data['id']}"
        # Names cannot force the fixed-width window wider or obscure resources.
        self.name.setMinimumWidth(0)
        self.name.setMaximumWidth(max(70, self.window().width() - 170))
        self.name.setText(
            self.name.fontMetrics().elidedText(
                full_name, Qt.TextElideMode.ElideRight, self.name.maximumWidth()
            )
        )
        self.name.setToolTip(full_name)
        self.character.setText(data.get("class", ""))
        active = phase in ("PlayerTurn", "StartPlayerTurn", "EndPlayerTurn")
        downed = data.get("hp") == 0
        self.ready.setText("✕" if downed else "✓" if active and data.get("ended_turn") else "")
        self.ready.setToolTip("Downed" if downed else "Turn ended")
        self.ready.setStyleSheet(f"color: {'#ee8998' if downed else '#86d7c0'};")
        self.health.set_values(data.get("hp"), data.get("max_hp"), data.get("shield"))
        self.will.set_values(data.get("will"), data.get("max_will"))
        abilities = data.get("abilities")
        self.unavailable.setVisible(abilities is None)
        count = len(abilities or [])
        if count != len(self.tiles):
            for tile in self.tiles:
                self.bindings.removeWidget(tile)
                tile.deleteLater()
            self.tiles = []
            for _ in range(count):
                tile = BindingTile()
                self.bindings.addWidget(tile, 1)
                self.tiles.append(tile)
        for tile, ability in zip(self.tiles, abilities or [], strict=True):
            tile.set_data(ability)


class PartyOverlay(QWidget):
    changed = Signal()

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.drag_offset = None
        self.rows = {}
        self.log_data = {"source": "log", "players": [], "phase": "", "status": "Waiting for party"}
        self.memory_data = None
        self.memory_at = 0.0
        self.data = self.log_data
        self._rendered = None
        self.setWindowTitle("Inkbound Party")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setStyleSheet(owner.styleSheet())
        self.setWindowIcon(owner.windowIcon())
        self.setWindowOpacity(owner.settings.opacity)
        self.setFixedWidth(owner.settings.width)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        root.addWidget(self.panel)
        box = QVBoxLayout(self.panel)
        box.setContentsMargins(12, 9, 12, 9)
        box.setSpacing(9)
        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.indicator = text_label("●", "partyStatus")
        self.heading = text_label("Party", "partyHeading")
        self.phase = text_label(name="muted")
        hide = QPushButton("×")
        hide.setObjectName("small")
        hide.setToolTip("Hide party overlay (restore from the meter or tray menu)")
        hide.setAccessibleName("Hide party overlay")
        hide.clicked.connect(self.close)
        header.addWidget(self.indicator)
        header.addWidget(self.heading)
        header.addStretch()
        header.addWidget(self.phase)
        header.addWidget(hide)
        box.addWidget(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        box.addLayout(self.body)
        self.empty = text_label("Waiting for party…", "muted")
        self.empty.setWordWrap(True)
        box.addWidget(self.empty)
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self.render)
        self.watchdog.start(1000)
        self.render()

    def update_log(self, data):
        self.log_data = data
        self.render()

    def update_memory(self, data):
        self.memory_data, self.memory_at = data, time.monotonic()
        self.render()

    def render(self):
        memory = self.memory_data
        fresh = time.monotonic() - self.memory_at < 2
        data = memory if memory and memory.get("source") == "memory" and fresh else self.log_data
        status = data.get("status", "")
        if memory and data is not memory:
            status = memory.get("status", "") if fresh else "Party reader paused"
            if status == "Waiting for Inkbound":
                data = {**data, "players": [], "phase": ""}
            status += " · Last logged values; cooldowns unavailable"
        elif self.owner.mode in ("demo", "replay"):
            status = f"{self.owner.mode.title()} · Recorded health and will; cooldowns unavailable"
        self.data = data
        signature = (data, status, self.width(), self.owner.interactive)
        if self._rendered == signature:
            return
        self._rendered = signature
        live = data.get("source") == "memory"
        self.indicator.setStyleSheet(f"color: {'#7fd2b7' if live else '#deb575'};")
        self.indicator.setToolTip(
            status + f"\n{self.owner.settings.interaction_key}: lock/unlock both"
            f" · {self.owner.settings.visibility_key}: hide/show both"
        )
        phase = data.get("phase", "")
        caption = (
            "Your turn"
            if phase in ("PlayerTurn", "StartPlayerTurn", "EndPlayerTurn")
            else "Enemy turn"
            if "Enemy" in phase
            else "Board effects"
            if phase
            else ""
        )
        if data.get("turn") and caption:
            caption = f"{data['turn']} · {caption}"
        self.phase.setText(caption)
        players = data.get("players", [])
        identities = [p["id"] for p in players]
        if list(self.rows) != identities:
            for row in self.rows.values():
                self.body.removeWidget(row)
                row.deleteLater()
            self.rows = {}
            for entity in identities:
                row = PartyRow()
                self.body.addWidget(row)
                self.rows[entity] = row
        for player in players:
            self.rows[player["id"]].set_data(player, phase)
        self.empty.setVisible(not players)
        self.empty.setText("Waiting for party…" if live or not memory else memory.get("status", ""))
        self.panel.layout().invalidate()
        self.panel.layout().activate()
        self.layout().invalidate()
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())
        self.changed.emit()

    def restore_position(self):
        settings = self.owner.settings
        point = QPoint(settings.party_x or 0, settings.party_y or 0)
        screens = QApplication.screens()
        screen = next((s for s in screens if s.availableGeometry().contains(point)), None)
        if screen and settings.party_x is not None and settings.party_y is not None:
            bounds = screen.availableGeometry()
        elif screens:
            screen = self.owner.screen() or screens[0]
            bounds = screen.availableGeometry()
            point = self.owner.pos() + QPoint(-self.width() - 12, 0)
            if point.x() < bounds.left():
                point = self.owner.pos() + QPoint(self.owner.width() + 12, 0)
        else:
            return
        self.move(
            min(max(bounds.left(), point.x()), bounds.right() - self.width() + 1),
            min(max(bounds.top(), point.y()), bounds.bottom() - self.height() + 1),
        )

    def set_interactive(self, enabled):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, not enabled)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, not enabled)
        if visible:
            self.show()
        self.render()

    def mousePressEvent(self, event):
        header_rect = self.header.rect().translated(self.header.mapTo(self, QPoint()))
        if (
            self.owner.interactive
            and event.button() == Qt.MouseButton.LeftButton
            and (header_rect.contains(event.position().toPoint()))
        ):
            self.drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag_offset is not None:
            self.drag_offset = None
            self.owner.save_settings()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        self.owner.set_party_enabled(False)
        event.accept()
