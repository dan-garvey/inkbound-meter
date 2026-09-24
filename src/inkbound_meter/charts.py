"""Proportional component bars shared by source summaries and hit details."""

from __future__ import annotations

from hashlib import blake2b
from html import escape

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget


def component_number(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"


def component_color(name: str, value: float = 0) -> str:
    """Stable colors across players, scopes, hit order, and application restarts."""
    if value < 0:
        return "#e77f8c"
    key = name.casefold()
    exact = {
        "base effect": "#8ea5bd",
        "omni damage": "#e7b65a",
        "magic damage": "#af93ed",
        "physical damage": "#dda47c",
        "general damage": "#83cbb5",
        "critical hit": "#f080ac",
        "rounding": "#c0c9d4",
        "near-death limit": "#b49ace",
        "unresolved": "#64758a",
        "health lost": "#ef8b88",
        "absorbed": "#82b9ed",
        "active shield": "#afcce7",
        "unused shield": "#b49ace",
        "hp restored": "#83cbb5",
        "overheal": "#e7b65a",
        "overkill": "#b49ace",
        "landed": "#ef8b88",
        "dodged": "#83cbb5",
        "blur": "#b497f3",
        "damage reduction": "#83cbb5",
        "uncapped damage reduction": "#68b7a2",
        "flat damage reduction": "#b0a4d8",
        "resistance": "#e7b65a",
        "attacker damage reduction": "#cfab91",
    }
    if key in exact:
        return exact[key]
    if key.startswith("vestige:") or " set (" in key:
        return "#e7b65a"
    for token, color in (
        ("frost", "#66d2e8"),
        ("burn", "#f29666"),
        ("poison", "#9bce76"),
        ("bleed", "#e77f8c"),
        ("smite", "#e5d78d"),
    ):
        if token in key:
            return color
    palette = ("#82b9d4", "#c3a0cb", "#92bbaa", "#bfc68b", "#cfab91", "#99a9de")
    return palette[blake2b(key.encode(), digest_size=1).digest()[0] % len(palette)]


def component_entries(components, unresolved: float = 0) -> list[dict]:
    entries = [dict(c) for c in components if c["damage"]]
    if unresolved:
        entries.append({"name": "Unresolved", "damage": unresolved})
    return entries


class ComponentBar(QWidget):
    """Positive credit above; negative credit below, using the same damage scale.

    Tiny components keep their true width. Exact values remain in the legend.
    Reductions never become positive shares or shrink other components' credit.
    """

    def __init__(
        self, entries, *, compact=False, fraction=1.0, prediction=False, unit="damage", parent=None
    ):
        super().__init__(parent)
        self.entries = entries
        self.compact = compact
        self.fraction = min(1.0, max(0.0, fraction))
        self.prediction = prediction
        self.unit = unit
        self.positive = sum(c["damage"] for c in entries if c["damage"] > 0)
        self.negative = -sum(c["damage"] for c in entries if c["damage"] < 0)
        self.setObjectName("componentBar")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(
            (10 if self.negative else 5) if compact else (44 if self.negative else 28)
        )
        self.setAccessibleName("Predicted damage components" if prediction else "Damage components")
        self.setAccessibleDescription(
            "; ".join(f"{c['name']}: {component_number(c['damage'])}" for c in entries)
            or "No component data"
        )

    def lane_rect(self, negative=False) -> QRectF:
        if self.compact:
            return QRectF(0, 7 if negative else 0, self.width(), 3 if negative else 5)
        return QRectF(0, 34 if negative else 0, self.width(), 8 if negative else 28)

    def segments(self):
        denominator = max(self.positive, self.negative)
        if not denominator:
            return []
        result = []
        for negative in (False, True):
            lane = self.lane_rect(negative)
            position = 0.0
            for entry in self.entries:
                value = entry["damage"]
                if not value or (value < 0) != negative:
                    continue
                width = lane.width() * self.fraction * abs(value) / denominator
                result.append((QRectF(position, lane.y(), width, lane.height()), entry))
                position += width
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        segments = self.segments()
        for negative in (False, True):
            if negative and not self.negative:
                continue
            lane = self.lane_rect(negative)
            clip = QPainterPath()
            clip.addRoundedRect(lane, 2 if self.compact else 4, 2 if self.compact else 4)
            painter.save()
            painter.setClipPath(clip)
            # A second full-width track adds noise to compact source summaries,
            # especially when their only negative credit is fractional rounding.
            if not (negative and self.compact):
                painter.fillRect(lane, QColor("#293545"))
            for rect, entry in segments:
                if (entry["damage"] < 0) != negative:
                    continue
                painter.fillRect(rect, QColor(component_color(entry["name"], entry["damage"])))
                if entry["name"] == "Unresolved" or negative or self.prediction:
                    painter.fillRect(rect, QBrush(QColor("#334050"), Qt.BrushStyle.BDiagPattern))
                if not self.compact and not negative and not self.prediction and rect.width() > 38:
                    painter.setPen(QColor("#121b27"))
                    painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
                    painter.drawText(
                        rect,
                        Qt.AlignmentFlag.AlignCenter,
                        f"{entry['damage'] / self.positive:.0%}",
                    )
                    painter.setPen(Qt.PenStyle.NoPen)
            painter.restore()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            for rect, entry in self.segments():
                if rect.contains(event.pos()):
                    denominator = max(self.positive, self.negative)
                    basis = "before reductions" if self.negative else "of the bar"
                    text = (
                        ("Prediction · " if self.prediction else "")
                        + f"<b>{escape(entry['name'])}</b><br>"
                        + f"{component_number(entry['damage'])} {escape(self.unit)} · "
                        + f"{abs(entry['damage']) / denominator:.1%} {basis}"
                    )
                    QToolTip.showText(event.globalPos(), text, self, rect.toAlignedRect(), 12000)
                    return True
            QToolTip.hideText()
            event.ignore()
            return True
        return super().event(event)


class EncounterTrend(QWidget):
    """Compact per-round encounter bars with values available on hover."""

    def __init__(self, points, *, unit="damage", parent=None):
        super().__init__(parent)
        self.points = points
        self.unit = unit
        self.setMinimumHeight(190)
        self.setMinimumWidth(max(320, 72 * len(points) + 24))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Encounter trend chart")
        self.setAccessibleDescription(
            "; ".join(
                f"Encounter {point['number']}: "
                + (
                    f"{component_number(point['per_round'])} per round"
                    if point["per_round"] is not None
                    else "no rounds recorded"
                )
                for point in points
            )
            or "No encounters recorded"
        )

    def bar_rects(self):
        usable = [point["per_round"] for point in self.points if point["per_round"] is not None]
        maximum = max(usable, default=0) or 1
        baseline, top = self.height() - 34, 31
        count = max(len(self.points), 1)
        slot = (self.width() - 24) / count
        width = min(44, max(14, slot - 14))
        result = []
        for index, point in enumerate(self.points):
            value = point["per_round"]
            height = 0 if value is None else max(1, (baseline - top) * value / maximum)
            x = 12 + index * slot + (slot - width) / 2
            result.append((QRectF(x, baseline - height, width, height), point))
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        baseline = self.height() - 34
        painter.setPen(QColor("#526477"))
        painter.drawLine(12, baseline, self.width() - 12, baseline)
        if not self.points:
            painter.setPen(QColor("#92a1b3"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No encounters recorded yet"
            )
            return
        for rect, point in self.bar_rects():
            if point["per_round"] is None:
                painter.setPen(QColor("#64758a"))
                painter.drawText(rect.adjusted(-8, -12, 8, 12), Qt.AlignmentFlag.AlignCenter, "—")
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#e7b65a" if point["active"] else "#78cdbc"))
                painter.drawRoundedRect(rect, 3, 3)
                if rect.width() >= 30:
                    painter.setPen(QColor("#dce6ef"))
                    painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
                    painter.drawText(
                        QRectF(rect.x() - 20, max(2, rect.y() - 20), rect.width() + 40, 17),
                        Qt.AlignmentFlag.AlignCenter,
                        component_number(point["per_round"]),
                    )
            painter.setPen(QColor("#aebdca"))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(
                QRectF(rect.x() - 16, baseline + 7, rect.width() + 32, 20),
                Qt.AlignmentFlag.AlignCenter,
                f"E{point['number']}",
            )

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            for rect, point in self.bar_rects():
                if rect.adjusted(-8, -20, 8, 24).contains(event.pos()):
                    rate = (
                        f"{component_number(point['per_round'])} {self.unit} / round"
                        if point["per_round"] is not None
                        else "No player rounds recorded"
                    )
                    changes = point["changes"]
                    change_text = (
                        "<br>".join(
                            escape(
                                f"{change['player']} · {change['kind'].title()}: {change['label']}"
                            )
                            for change in changes
                        )
                        or "No observed gear or aspect changes"
                    )
                    QToolTip.showText(
                        event.globalPos(),
                        f"<b>Encounter {point['number']}</b><br>{rate}<br>"
                        f"{point['rounds']} rounds · {point['will_spent']} Will spent"
                        f"<br><br><b>Observed changes before this encounter</b><br>{change_text}",
                        self,
                        rect.toAlignedRect(),
                        15000,
                    )
                    return True
            QToolTip.hideText()
            return True
        return super().event(event)
