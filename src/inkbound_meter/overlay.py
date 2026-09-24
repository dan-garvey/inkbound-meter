"""Windows overlay. All widgets live on the Qt thread; IO owns its SQLite connection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .charts import (
    ComponentBar,
    EncounterTrend,
    component_color,
    component_entries,
    component_number,
)
from .config import Settings
from .hotkeys import Hotkeys, parse_hotkey
from .model import Meter
from .party_overlay import PartyCollector, PartyOverlay
from .service import Session
from .support import METRICS

STYLE = """
QWidget { color: #e4e9ef; font-family: 'Segoe UI'; font-size: 12px; }
QFrame#panel { background: #141b25; border: 1px solid #384555; border-radius: 12px; }
QFrame#componentDetail { background: #1b2634; border: 1px solid #304052; border-radius: 7px; }
QFrame#hitStats { background: #15202d; border: none; border-radius: 5px; }
QLabel { background: transparent; border: none; }
QLabel#muted { color: #92a1b3; font-size: 11px; }
QLabel#title { color: #f4f6f8; font-size: 18px; font-weight: 600; }
QPushButton { background: #202c3b; border: 1px solid #334153; border-radius: 5px;
              padding: 5px 9px; color: #c4cfdb; }
QPushButton:hover { background: #303f51; color: white; }
QPushButton:checked { background: #304b4a; border-color: #578c80; color: #bceadc; }
QPushButton#name { background: transparent; border: none; text-align: left;
                   padding: 0px; color: #f1f4f7; font-weight: 600; }
QPushButton#small { padding: 3px 7px; font-size: 11px; }
QPushButton#source { background: transparent; border: none; text-align: left;
                     padding: 2px 0px; color: #b5c8d9; font-size: 11px; }
QPushButton#disclosure { background: transparent; border: none; text-align: left;
                         padding: 3px 0px; color: #a8bfd6; font-size: 11px; }
QPushButton#disclosure:hover { color: #eff5ff; }
QPushButton#metricSelector { background: transparent; border: none; padding: 0px;
                            text-align: left; color: #f4f6f8; font-size: 16px; font-weight: 600; }
QPushButton#metricSelector:hover { color: #bceadc; }
QToolTip { background: #233246; color: #f2f5fa; border: 1px solid #6c8198;
           padding: 7px; font-size: 12px; }
QProgressBar { background: #273242; border: none; border-radius: 2px; }
QProgressBar::chunk { background: #78cdbc; border-radius: 2px; }
QScrollArea { background: transparent; border: none; }
QWidget#rows { background: #141b25; }
QScrollBar:vertical { background: #1b2532; width: 5px; border-radius: 2px; }
QScrollBar::handle:vertical { background: #506174; min-height: 20px; border-radius: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QMenu, QDialog { background: #1b2532; }
QMenu::item:selected { background: #334657; }
QLineEdit, QSpinBox, QComboBox { background: #253346; padding: 5px; border: 1px solid #4b6075; }
/* Reserve a separate arrow column so the native Windows editor cannot cover it. */
QSpinBox { padding-right: 24px; }
QSpinBox QLineEdit { border: none; padding: 0px; background: transparent; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 24px; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 24px; }
"""


def label(text="", *, role="", parent=None):
    widget = QLabel(str(text), parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


def compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}m"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,}"


def efficiency_label(data: dict) -> str:
    """A compact, explicit denominator line for a player's captured output."""
    values = []
    if data.get("per_round") is not None:
        values.append(f"{component_number(data['per_round'])} / round")
    elif data.get("rounds"):
        values.append("— / round")
    if data.get("per_will") is not None:
        values.append(f"{component_number(data['per_will'])} / Will")
    elif data.get("will_spent"):
        values.append("— / Will")
    return "  ·  ".join(values)


def trend_points(snapshot: dict, metric: str) -> list[dict]:
    """Chart-ready party output rates for every recorded encounter in this run."""
    points = []
    for encounter in snapshot.get("encounter_history", []):
        value = encounter["damage"] if metric == "damage" else encounter["metrics"].get(metric, 0)
        points.append(
            {
                **encounter,
                "per_round": value / encounter["rounds"] if encounter["rounds"] else None,
            }
        )
    return points


class EncounterTrendsDialog(QDialog):
    def __init__(self, snapshot: dict, metric: str, parent=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.setWindowTitle("Encounter trends")
        self.setMinimumWidth(560)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 14, 15, 14)
        layout.setSpacing(9)
        controls = QHBoxLayout()
        controls.addWidget(label("Per round", role="muted"))
        self.metric = QComboBox()
        for key, caption in [("damage", "Damage dealt"), *[(k, v[1]) for k, v in METRICS.items()]]:
            self.metric.addItem(caption, key)
        self.metric.setCurrentIndex(max(0, self.metric.findData(metric)))
        self.metric.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.metric, 1)
        layout.addLayout(controls)
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.chart_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.chart_scroll)
        self.changes = QWidget()
        self.change_layout = QVBoxLayout(self.changes)
        self.change_layout.setContentsMargins(0, 4, 0, 0)
        self.change_layout.setSpacing(4)
        layout.addWidget(self.changes)
        self.refresh()

    def refresh(self):
        metric = self.metric.currentData()
        chart = EncounterTrend(
            trend_points(self.snapshot, metric),
            unit={
                "damage": "damage",
                "healing": "HP",
                "shielding": "shield",
                "pressure": "hits",
            }.get(metric, "damage"),
        )
        old = self.chart_scroll.takeWidget()
        if old:
            old.deleteLater()
        self.chart_scroll.setWidget(chart)
        while self.change_layout.count():
            item = self.change_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        heading = label("Observed changes before each encounter", role="muted")
        self.change_layout.addWidget(heading)
        changes = [
            (encounter["number"], change)
            for encounter in self.snapshot.get("encounter_history", [])
            for change in encounter["changes"]
        ]
        if not changes:
            self.change_layout.addWidget(
                label("No gear, ability, or aspect draft events were captured yet.", role="muted")
            )
            return
        for number, change in changes:
            kind = {"gear": "Gear", "ability": "Ability", "aspect": "Aspect"}[change["kind"]]
            line = label(f"E{number}  ·  {change['player']}  ·  {kind}: {change['label']}")
            line.setWordWrap(True)
            self.change_layout.addWidget(line)


def payload(meter: Meter, status: str) -> dict:
    return {
        "encounter": meter.snapshot(),
        "run": meter.snapshot(scope="run"),
        "report": meter.report(),
        "status": status,
        "party": meter.party.snapshot(meter.support_context.health),
        "connected": meter.in_run,
    }


class Collector(QThread):
    updated = Signal(dict)
    failed = Signal(str)

    def __init__(self, log: Path, database: Path):
        super().__init__()
        self.log = log
        self.database = database

    def run(self):
        session = None
        try:
            session = Session(self.log, self.database)
            last_status = ""
            while not self.isInterruptionRequested():
                try:
                    result = session.poll()
                    if result.events or result.rebuild or session.status != last_status:
                        self.updated.emit(payload(session.meter, session.status))
                        last_status = session.status
                    if not result.catching_up:
                        self.msleep(200)
                except (OSError, ValueError) as exc:
                    message = f"Log unavailable: {exc}"
                    if message != last_status:
                        self.failed.emit(message)
                        last_status = message
                    self.msleep(1000)
        except Exception as exc:
            self.failed.emit(f"Capture stopped: {exc}")
        finally:
            if session:
                session.close()


class SourceRow(QWidget):
    def __init__(
        self,
        data,
        expanded,
        latest_mode,
        toggle,
        toggle_mode,
        parent=None,
        *,
        stats_expanded=False,
        toggle_stats=None,
    ):
        super().__init__(parent)
        self.setProperty("sourceId", data["id"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        top = QHBoxLayout()
        caption = QPushButton(("▾ " if expanded else "▸ ") + data["name"])
        caption.setObjectName("source")
        caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        caption.setToolTip(data["name"] + " · click for damage components")
        caption.clicked.connect(toggle)
        top.addWidget(caption, 1)
        top.addWidget(label(f"{data['damage']:,} · {data['share']:.0%}", role="muted"))
        layout.addLayout(top)
        detail = data["breakdown"]
        unresolved = max(detail["unresolved_damage"], data["damage"] - detail["matched_damage"])
        entries = component_entries(detail["components"], unresolved)
        if not expanded:
            layout.addWidget(ComponentBar(entries, compact=True, fraction=data["share"]))
            return

        card = QFrame()
        card.setObjectName("componentDetail")
        content = QVBoxLayout(card)
        content.setContentsMargins(10, 10, 10, 8)
        content.setSpacing(7)
        layout.addWidget(card)
        latest = detail["latest"]
        controls = QHBoxLayout()
        for is_latest, text in ((False, "Total components"), (True, "Last hit")):
            button = QPushButton(text)
            button.setObjectName("small")
            button.setCheckable(True)
            button.setChecked(latest_mode == is_latest)
            button.clicked.connect(lambda checked=False, v=is_latest: toggle_mode(v))
            controls.addWidget(button)
        content.addLayout(controls)
        summary = QHBoxLayout()
        prediction = latest_mode and latest.get("status") == "mismatch"
        if latest_mode:
            observed = latest.get("observed", 0)
            matched = latest.get("status") == "matched"
            status_text = "Matched hit" if matched else "Unresolved hit"
            amount = label(f"{observed:,}")
            amount.setObjectName("hitDamage")
            amount.setStyleSheet("font-size: 22px; font-weight: 600;")
            amount.setToolTip(f"{observed:,} damage logged by Inkbound")
            entries = component_entries(
                latest.get("components", []),
                observed if latest.get("status") not in ("matched", "mismatch") else 0,
            )
            total = latest.get("predicted", observed) if prediction else observed
        else:
            matched = detail["hits"] > 0 and not detail["unresolved_hits"]
            status_text = f"{detail['matched_hits']} / {detail['hits']} hits matched"
            amount = label(
                f"{detail['matched_damage'] / data['damage']:.0%} explained"
                if data["damage"]
                else "No damage",
                role="muted",
            )
            total = data["damage"]
        status = label(status_text, role="muted")
        status.setStyleSheet("color: #91cdb1;" if matched else "color: #e7bd77;")
        summary.addWidget(status, 1)
        summary.addWidget(amount)
        content.addLayout(summary)
        if prediction:
            notice = label(
                f"Prediction: {latest['predicted']:,} · excluded from totals", role="muted"
            )
            notice.setObjectName("predictionNotice")
            notice.setWordWrap(True)
            content.addWidget(notice)
        elif latest_mode and not matched:
            notice = label(latest.get("reason", "No hit captured yet"), role="muted")
            notice.setWordWrap(True)
            content.addWidget(notice)

        bar = ComponentBar(entries, prediction=prediction)
        content.addWidget(bar)
        if bar.negative:
            caption = label(
                f"{component_number(bar.positive)} before reductions  −  "
                f"{component_number(bar.negative)} reduced",
                role="muted",
            )
            caption.setWordWrap(True)
            content.addWidget(caption)
        self.add_legend(content, entries, bar.positive if bar.negative else total, bar.negative > 0)

        if not latest_mode and detail["unresolved_hits"]:
            reasons = "\n".join(f"{reason}: {n} hits" for reason, n in detail["reasons"].items())
            status.setToolTip(reasons)
        footer = QHBoxLayout()
        if latest_mode and latest.get("inputs"):
            stats = QPushButton(("▾ " if stats_expanded else "▸ ") + "Stats at hit")
            stats.setObjectName("disclosure")
            stats.setAccessibleName("Stats at hit")
            stats.setCheckable(True)
            stats.setChecked(stats_expanded)
            stats.setToolTip("\n".join(f"{k}: {v}" for k, v in latest["inputs"].items()))
            if toggle_stats:
                stats.clicked.connect(toggle_stats)
            footer.addWidget(stats)
        else:
            footer.addWidget(label("Damage contribution", role="muted"))
        footer.addStretch()
        help_label = label("ⓘ", role="muted")
        help_label.setAccessibleName("How damage is credited")
        help_label.setToolTip(
            "Bonuses are credited in calculation order.\n"
            "Base and source scaling first, then Omni/Magic/Physical bonuses, then later "
            "multipliers and reductions. Interactions belong to the later stage.\n"
            "Hatched gray is unresolved damage. Red below the bar is damage removed.\n"
            "Only complete reconstructions that match a logged hit enter totals."
        )
        footer.addWidget(help_label)
        content.addLayout(footer)
        if latest_mode and stats_expanded and latest.get("inputs"):
            self.add_stats(content, latest)

    @staticmethod
    def add_legend(layout, entries, denominator, reduced):
        legend = QVBoxLayout()
        legend.setSpacing(4)
        for component in entries:
            row = QHBoxLayout()
            row.setSpacing(7)
            value = component["damage"]
            swatch = QFrame()
            swatch.setFixedSize(7, 7)
            swatch.setStyleSheet(
                f"background: {component_color(component['name'], value)}; border-radius: 2px;"
            )
            row.addWidget(swatch)
            name = label(component["name"], role="muted")
            name.setWordWrap(True)
            row.addWidget(name, 1)
            amount = label(component_number(value))
            amount.setStyleSheet("font-size: 11px;")
            row.addWidget(amount)
            percent = label(f"{value / denominator:.1%}" if denominator else "—", role="muted")
            percent.setFixedWidth(48)
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            percent.setToolTip("Share before reductions" if reduced else "Share of damage")
            row.addWidget(percent)
            legend.addLayout(row)
        layout.addLayout(legend)

    @staticmethod
    def add_stats(layout, latest):
        panel = QFrame()
        panel.setObjectName("hitStats")
        values = QVBoxLayout(panel)
        values.setContentsMargins(8, 8, 8, 8)
        values.setSpacing(5)
        if latest.get("stacks", 1) > 1:
            values.addWidget(label(f"{latest['stacks']} effect stacks", role="muted"))
        for name, value in latest["inputs"].items():
            row = QHBoxLayout()
            caption = label(name, role="muted")
            caption.setWordWrap(True)
            row.addWidget(caption, 1)
            row.addWidget(label(str(value), role="muted"))
            values.addLayout(row)
        layout.addWidget(panel)


class PlayerRow(QWidget):
    def __init__(
        self,
        data: dict,
        expanded: bool,
        toggle,
        parent=None,
        *,
        expanded_sources=None,
        latest_sources=None,
        stats_sources=None,
        toggle_source=None,
        select_source_mode=None,
        toggle_source_stats=None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 8)
        layout.setSpacing(5)
        top = QHBoxLayout()
        name = QPushButton(("▾  " if expanded else "▸  ") + data["name"])
        name.setObjectName("name")
        name.setToolTip(data["name"] + " · " + data["class"])
        name.clicked.connect(toggle)
        top.addWidget(name, 1)
        amount = label(compact(data["damage"]))
        amount.setStyleSheet("font-size: 15px; font-weight: 600;")
        amount.setToolTip(f"{data['damage']:,} logged damage")
        top.addWidget(amount)
        share = label(f"{data['share']:.1%}", role="muted")
        share.setFixedWidth(48)
        share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(share)
        layout.addLayout(top)
        bar = QProgressBar()
        bar.setFixedHeight(4)
        bar.setRange(0, 1000)
        bar.setValue(round(data["share"] * 1000))
        bar.setTextVisible(False)
        layout.addWidget(bar)
        efficiency = efficiency_label(data)
        if efficiency:
            rates = label(efficiency, role="muted")
            rates.setToolTip(
                f"{data.get('rounds', 0)} recorded party round(s) · "
                f"{data.get('will_spent', 0)} Will spent by this player"
            )
            layout.addWidget(rates)
        if expanded:
            layout.addWidget(label(data["class"] or "Class unknown", role="muted"))
            if not data["sources"]:
                layout.addWidget(label("No damage recorded yet", role="muted"))
            for source in data["sources"]:
                key = (data["id"], source["id"])
                layout.addWidget(
                    SourceRow(
                        source,
                        key in (expanded_sources or ()),
                        key in (latest_sources or ()),
                        lambda checked=False, k=key: toggle_source(k),
                        lambda latest, k=key: select_source_mode(k, latest),
                        stats_expanded=key in (stats_sources or ()),
                        toggle_stats=lambda checked=False, k=key: toggle_source_stats(k),
                    )
                )


class SupportRow(QWidget):
    def __init__(self, data, metric, expanded, toggle):
        super().__init__()
        self.setObjectName("supportRow")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 8)
        layout.setSpacing(5)
        top = QHBoxLayout()
        name = QPushButton(("▾  " if expanded else "▸  ") + data["name"])
        name.setObjectName("name")
        name.setToolTip(data["name"] + " · " + data["class"])
        name.clicked.connect(toggle)
        top.addWidget(name, 1)
        amount = label(compact(data["amount"]))
        amount.setStyleSheet("font-size: 15px; font-weight: 600;")
        amount.setToolTip(f"{data['amount']:,} · {METRICS[metric][1]}")
        top.addWidget(amount)
        top.addWidget(label(f"{data['share']:.1%}", role="muted"))
        layout.addLayout(top)
        mitigation = data.get("mitigation") if metric == "taken" else None
        components = list(data["components"])
        if mitigation:
            components += mitigation["components"]
        bar = ComponentBar(
            components,
            compact=not expanded,
            fraction=1 if mitigation else data["share"],
            unit={"healing": "HP", "shielding": "shield", "pressure": "hits"}.get(metric, "damage"),
        )
        bar.setAccessibleName(METRICS[metric][1] + " components")
        layout.addWidget(bar)
        efficiency = efficiency_label(data)
        if efficiency:
            rates = label(efficiency, role="muted")
            rates.setToolTip(
                f"{data.get('rounds', 0)} recorded party round(s) · "
                f"{data.get('will_spent', 0)} Will spent by this player"
            )
            layout.addWidget(rates)
        if mitigation and data["events"]:
            prevented = component_number(mitigation["prevented"])
            if mitigation["unresolved_hits"]:
                prevention_text = (
                    f"{prevented}+ prevented"
                    if mitigation["matched_hits"]
                    else "Prevention unknown"
                )
            else:
                prevention_text = f"{prevented} prevented"
            note = label(
                f"{prevention_text} · {component_number(mitigation['absorbed'])} absorbed",
                role="muted",
            )
            note.setWordWrap(True)
            note.setObjectName("mitigationSummary")
            note.setToolTip(
                "Prevented: damage removed before shields. Absorbed: damage spent on shields.\n"
                "The Taken total includes absorbed damage; prevented damage is additional.\n"
                f"Pre-shield mitigation reconstructed for {mitigation['matched_hits']} "
                f"of {data['events']} hits."
            )
            layout.addWidget(note)
        if not expanded:
            return
        for component in components:
            row = QHBoxLayout()
            swatch = label("●")
            swatch.setStyleSheet(f"color: {component_color(component['name'])};")
            row.addWidget(swatch)
            caption = (
                "Unused / overshield" if component["name"] == "Unused shield" else component["name"]
            )
            row.addWidget(label(caption), 1)
            row.addWidget(label(component_number(component["damage"])))
            layout.addLayout(row)
        if mitigation:
            if mitigation["unresolved_hits"]:
                note = label(
                    f"Prevention unknown for {mitigation['unresolved_hits']} hits", role="muted"
                )
                note.setWordWrap(True)
                note.setToolTip(
                    "\n".join(
                        f"{reason}: {count}" for reason, count in mitigation["reasons"].items()
                    )
                )
                layout.addWidget(note)
            for provider in mitigation["shield_providers"]:
                row = QHBoxLayout()
                name = label("Shield from " + provider["name"], role="muted")
                name.setWordWrap(True)
                name.setToolTip("Shared shield spending is attributed oldest-first.")
                row.addWidget(name, 1)
                row.addWidget(label(compact(provider["amount"]), role="muted"))
                layout.addLayout(row)
        if data["unknown_overheal"]:
            note = label(f"Overheal unavailable for {data['unknown_overheal']} heals", role="muted")
            note.setWordWrap(True)
            layout.addWidget(note)
        if not data["events"]:
            layout.addWidget(label("No events recorded yet", role="muted"))
        for source in data["sources"]:
            row = QHBoxLayout()
            caption = label(source["name"], role="muted")
            caption.setWordWrap(True)
            row.addWidget(caption, 1)
            row.addWidget(label(compact(source["amount"]), role="muted"))
            layout.addLayout(row)
        if metric in ("shielding", "healing"):
            for recipient in data["recipients"]:
                row = QHBoxLayout()
                row.addWidget(label("→ " + recipient["name"], role="muted"), 1)
                row.addWidget(label(compact(recipient["amount"]), role="muted"))
                layout.addLayout(row)


class Overlay(QWidget):
    def __init__(self, settings_path: Path, *, mode="live", register_hotkeys=True):
        super().__init__()
        self.settings_path = settings_path
        self.settings = Settings.load(settings_path)
        self.mode = mode
        self.interactive = True
        self.expanded: set[int] = set()
        self.expanded_sources: set[tuple[int, str]] = set()
        self.latest_sources: set[tuple[int, str]] = set()
        self.stats_sources: set[tuple[int, str]] = set()
        self._rows_state = None
        self._rows_height = 65
        self.last_run = None
        self.data = payload(Meter(), "starting")
        self.drag_offset = None
        self.hotkeys = None
        self.tray = None
        self.collector = None
        self.party_collector = None
        self.trends_dialog = None
        self.hotkey_error = ""
        self.setWindowTitle("Inkbound Meter")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Inkbound retains focus while the unlocked overlay is being inspected.
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setWindowOpacity(self.settings.opacity)
        self.setFixedWidth(self.settings.width)
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        root.addWidget(self.panel)
        box = QVBoxLayout(self.panel)
        box.setContentsMargins(12, 9, 12, 9)
        box.setSpacing(6)
        self.header = QWidget()
        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(7)
        self.status = label("●")
        self.status.setObjectName("captureStatus")
        self.status.setFixedWidth(12)
        header_row.addWidget(self.status)
        self.heading = QPushButton()
        self.heading.setObjectName("metricSelector")
        self.heading.setAccessibleName("Choose meter statistic")
        self.heading.clicked.connect(self.open_metric_menu)
        header_row.addWidget(self.heading, 1)
        self.total = label("—", role="title")
        header_row.addWidget(self.total)
        self.menu_button = QPushButton("···")
        self.menu_button.setObjectName("small")
        self.menu_button.setAccessibleName("Meter menu and settings")
        self.menu_button.setFixedWidth(28)
        self.menu_button.clicked.connect(self.open_menu)
        header_row.addWidget(self.menu_button)
        box.addWidget(self.header)
        tabs = QHBoxLayout()
        self.tabs = {}
        for key, caption in (("encounter", "Current encounter"), ("run", "Whole run")):
            button = QPushButton(caption)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, s=key: self.select_scope(s))
            tabs.addWidget(button)
            self.tabs[key] = button
        box.addLayout(tabs)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        box.addWidget(self.scroll)
        self.note = label("", role="muted")
        self.note.setWordWrap(True)
        box.addWidget(self.note)
        self.setWindowIcon(self.make_icon())
        self.party = PartyOverlay(self)
        self.party.changed.connect(self.render)
        self._make_tray()
        if register_hotkeys:
            self.register_hotkeys()
        self.render()
        self.restore_position()
        self.party.restore_position()

    @staticmethod
    def make_icon():
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("#17212d"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#f0cc88"))
        painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "I")
        painter.end()
        return QIcon(pixmap)

    def _make_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Inkbound Meter")
        self.tray.setContextMenu(self.make_menu())
        self.tray.activated.connect(
            lambda reason: (
                self.recover() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
            )
        )
        self.tray.show()

    def make_menu(self):
        menu = QMenu(self)
        party = menu.addAction("Party overlay")
        party.setCheckable(True)
        party.setChecked(self.settings.party_enabled)
        party.triggered.connect(self.set_party_enabled)
        menu.aboutToShow.connect(lambda: party.setChecked(self.settings.party_enabled))
        menu.addSeparator()
        for text, callback in (
            ("Show and unlock", self.recover),
            ("Toggle click-through", self.toggle_interaction),
            ("Encounter trends…", self.open_trends),
            ("Settings…", self.open_settings),
            ("Export captured runs…", self.export),
            ("Exit", self.close),
        ):
            action = QAction(text, menu)
            action.triggered.connect(callback)
            menu.addAction(action)
        return menu

    def open_menu(self):
        self.make_menu().exec(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height())))

    def open_metric_menu(self):
        menu = QMenu(self)
        for metric, caption in [
            ("damage", "Damage dealt"),
            *[(k, v[1]) for k, v in METRICS.items()],
        ]:
            action = menu.addAction(caption)
            action.setCheckable(True)
            action.setChecked(metric == self.settings.metric)
            action.triggered.connect(lambda checked=False, m=metric: self.select_metric(m))
        menu.exec(self.heading.mapToGlobal(QPoint(0, self.heading.height())))

    def open_trends(self):
        self.recover()
        if self.trends_dialog and self.trends_dialog.isVisible():
            self.trends_dialog.activateWindow()
            return
        self.trends_dialog = EncounterTrendsDialog(self.data["run"], self.settings.metric, self)
        self.trends_dialog.destroyed.connect(lambda: setattr(self, "trends_dialog", None))
        self.trends_dialog.show()

    def select_metric(self, metric):
        self.settings.metric = metric
        self.render()
        self.save_settings()

    def register_hotkeys(self):
        if self.hotkeys:
            self.hotkeys.close()
        bindings = {
            "Interaction": (self.settings.interaction_key, self.toggle_interaction),
            "Visibility": (self.settings.visibility_key, self.toggle_visibility),
        }
        self.hotkeys = Hotkeys(QApplication.instance(), bindings)
        self.hotkey_error = "; ".join(self.hotkeys.errors)
        if sys.platform != "win32":
            for sequence, callback in bindings.values():
                shortcut = QShortcut(sequence, self)
                shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
                shortcut.activated.connect(callback)

    def attach(self, collector: Collector):
        self.collector = collector
        collector.updated.connect(self.update_data)
        collector.failed.connect(self.show_error)
        collector.start()

    def update_data(self, data: dict):
        self.data = data
        self.party.update_log(data.get("party", {"source": "log", "players": [], "phase": ""}))
        self.render()

    def start_party_capture(self):
        if self.party_collector is None:
            self.party_collector = PartyCollector(self)
            self.party_collector.updated.connect(self.party.update_memory)
            self.party_collector.start()

    def set_party_enabled(self, enabled):
        self.settings.party_enabled = bool(enabled)
        self.party.setVisible(bool(enabled) and self.isVisible())
        self.save_settings()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "party") and self.settings.party_enabled:
            self.party.show()

    def hideEvent(self, event):
        if hasattr(self, "party"):
            self.party.hide()
        super().hideEvent(event)

    def show_error(self, message: str):
        self.data["status"] = message
        self.render()

    def select_scope(self, scope):
        self.settings.scope = scope
        self.render()
        self.save_settings()

    def toggle_player(self, entity):
        if entity in self.expanded:
            self.expanded.remove(entity)
        else:
            self.expanded.add(entity)
        self.render()

    def toggle_source(self, key):
        self.expanded_sources.symmetric_difference_update({key})
        self.render()

    def select_source_mode(self, key, latest):
        if latest:
            self.latest_sources.add(key)
        else:
            self.latest_sources.discard(key)
        self.render()

    def toggle_source_stats(self, key):
        self.stats_sources.symmetric_difference_update({key})
        self.render()

    def render(self):
        snapshot = self.data[self.settings.scope]
        metric = self.settings.metric
        damage = metric == "damage"
        view = self.display_identities(
            snapshot, snapshot if damage else snapshot["metrics"][metric]
        )
        amount = view["party_damage"] if damage else view["party_total"]
        caption = "Damage" if damage else METRICS[metric][0]
        if self.mode in ("demo", "replay"):
            caption = self.mode.title() + " " + caption.lower()
        self.heading.setText(caption + " ▾")
        description = "Logged damage includes overkill." if damage else METRICS[metric][2]
        if not damage:
            description += (
                "\nWhole run includes support between fights; encounter includes combat only."
            )
        self.heading.setToolTip("Choose statistic\n" + description)
        if snapshot["run"] != self.last_run:
            self.expanded.clear()
            self.expanded_sources.clear()
            self.latest_sources.clear()
            self.stats_sources.clear()
            self.last_run = snapshot["run"]
        for key, button in self.tabs.items():
            button.setChecked(key == self.settings.scope)
        self.total.setText(compact(amount) if snapshot["players"] else "—")
        context = "Waiting for combat"
        if snapshot["run"]:
            encounter = snapshot["encounter"] or "—"
            context = f"Run {snapshot['run']}  ·  Encounter {encounter}"
            if snapshot["outcome"] in ("victory", "defeat"):
                context += "  ·  " + snapshot["outcome"].title()
            elif not snapshot["active"] and snapshot["encounter"]:
                context += "  ·  Last fight"
        self.total.setToolTip(f"{amount:,} attributed to party members\n{context}\n{description}")
        for key, button in self.tabs.items():
            scope = "Current encounter" if key == "encounter" else "Whole run"
            button.setToolTip(f"{scope}\n{context}")
        rows_state = (
            snapshot["run"],
            metric,
            view["players"],
            self.settings.width,
            frozenset(self.expanded),
            frozenset(self.expanded_sources),
            frozenset(self.latest_sources),
            frozenset(self.stats_sources),
        )
        # Most log activity changes no visible damage. Keep existing widgets alive
        # so hover delays, tooltips, and keyboard focus survive those updates.
        if rows_state != self._rows_state:
            self.render_rows(view)
            self._rows_state = rows_state
        notes = []
        if snapshot["partial"]:
            notes.append("Partial capture · earlier events are unavailable")
        if damage and snapshot["unattributed_damage"]:
            notes.append(f"Unassigned damage: {snapshot['unattributed_damage']:,}")
        if damage and snapshot["non_party_damage"]:
            notes.append(f"Enemy / environmental damage: {snapshot['non_party_damage']:,}")
        if not damage and view["unattributed_events"]:
            role = "provider" if metric in ("shielding", "healing") else "recipient"
            notes.append(
                f"Unknown / non-party {role}: {view['unattributed']:,} "
                f"({view['unattributed_events']} events)"
            )
        if not damage and view["unknown_overheal"]:
            notes.append(f"Overheal unavailable for {view['unknown_overheal']} heals")
        if not damage and not snapshot["support_available"]:
            notes.append("Older capture · support details unavailable")
        if snapshot["parse_errors"]:
            notes.append(
                f"{snapshot['parse_errors']} unsupported records · totals may be incomplete"
            )
        status = self.data["status"]
        display = {
            "starting": "Connecting to Inkbound…",
            "live": "Following game log",
            "catching up": "Reading earlier combat…",
            "waiting for log": "Waiting for game log…",
            "waiting for game": "Waiting for Inkbound…",
            "demo": "Demo data · no game connected",
            "replay": "Saved log · live capture is off",
        }.get(status, status)
        known_statuses = {
            "starting",
            "live",
            "catching up",
            "waiting for log",
            "waiting for game",
            "demo",
            "replay",
        }
        failed = status not in known_statuses
        if failed:
            notes.append(display)
        if self.hotkey_error:
            notes.append(self.hotkey_error)
        self.note.setText("\n".join(notes))
        self.note.setVisible(bool(notes))
        color = (
            "#ef8b88"
            if failed or self.hotkey_error
            else "#78cdbc"
            if status == "live"
            else "#91afd3"
            if status in ("demo", "replay")
            else "#e7b65a"
        )
        self.status.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status.setAccessibleName(display)
        self.status.setToolTip("\n".join([display, context, *notes]))
        state = "Lock" if self.interactive else "Edit"
        hints = (
            f"{self.settings.interaction_key} {state} · "
            f"{self.settings.visibility_key} Hide\n"
            + ("Drag header to move" if self.interactive else "Clicks pass through")
        )
        self.header.setToolTip(hints)
        self.menu_button.setToolTip(f"Settings · opacity {self.settings.opacity:.0%}\n{hints}")
        self.fit_content()
        # Qt delivers child layout changes asynchronously; recalculate afterwards.
        QTimer.singleShot(0, self.fit_content)

    def display_identities(self, snapshot, view):
        """Fill names before the first cast, only for the matching live party.

        This is a presentation annotation. Captured events, report identities and
        totals remain owned by the log reducer; memory never relabels a past run.
        """
        party = self.party.data
        if (
            self.mode != "live"
            or self.data["status"] != "live"
            or not self.data.get("connected")
            or snapshot.get("outcome") != "in_progress"
            or snapshot.get("seed") is None
            or party.get("source") != "memory"
            or party.get("seed") != snapshot["seed"]
            or {p["id"] for p in party["players"]} != {p["id"] for p in snapshot["players"]}
        ):
            return view
        names = {p["id"]: p for p in party["players"]}
        return {
            **view,
            "players": [
                {**player, "name": names[player["id"]].get("name") or player["name"]}
                for player in view["players"]
            ],
        }

    def render_rows(self, snapshot):
        scroll_position = self.scroll.verticalScrollBar().value()
        old = self.scroll.takeWidget()
        if old:
            old.deleteLater()
        body = QWidget()
        body.setObjectName("rows")
        body.setAutoFillBackground(False)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 3, 0)
        layout.setSpacing(9)
        if not snapshot["players"]:
            empty = label(
                "Start a run to see your party.\nDamage appears as attacks resolve.", role="muted"
            )
            empty.setWordWrap(True)
            layout.addWidget(empty)
        for player in snapshot["players"]:
            entity = player["id"]
            if self.settings.metric != "damage":
                layout.addWidget(
                    SupportRow(
                        player,
                        self.settings.metric,
                        entity in self.expanded,
                        lambda checked=False, e=entity: self.toggle_player(e),
                    )
                )
                continue
            layout.addWidget(
                PlayerRow(
                    player,
                    entity in self.expanded,
                    lambda checked=False, e=entity: self.toggle_player(e),
                    expanded_sources=self.expanded_sources,
                    latest_sources=self.latest_sources,
                    stats_sources=self.stats_sources,
                    toggle_source=self.toggle_source,
                    select_source_mode=self.select_source_mode,
                    toggle_source_stats=self.toggle_source_stats,
                )
            )
        layout.addStretch(1)
        self.scroll.setWidget(body)
        body.setAutoFillBackground(False)
        body.ensurePolished()
        layout.activate()
        height = max(body.sizeHint().height(), layout.totalHeightForWidth(self.settings.width - 30))
        self._rows_height = min(430, max(65, height))
        self.scroll.setFixedHeight(self._rows_height)
        self.scroll.verticalScrollBar().setValue(scroll_position)

    def fit_content(self):
        # Replacing nested source rows can leave the parent size hint cached on
        # Windows. Recompute both containing layouts before sizing the window.
        self.panel.layout().invalidate()
        self.panel.layout().activate()
        self.layout().invalidate()
        self.layout().activate()
        height = max(
            self.layout().totalSizeHint().height(), self.layout().totalHeightForWidth(self.width())
        )
        screen = QApplication.screenAt(self.pos()) or self.screen()
        bounds = screen.availableGeometry() if screen else None
        if bounds:
            # Leave room for the header/footer on short or scaled displays. The
            # damage list scrolls instead of pushing controls off the desktop.
            other_height = height - self.scroll.height()
            rows_height = min(self._rows_height, max(65, bounds.height() - other_height))
            if rows_height != self.scroll.height():
                self.scroll.setFixedHeight(rows_height)
                self.panel.layout().invalidate()
                self.panel.layout().activate()
                self.layout().invalidate()
                self.layout().activate()
                height = max(
                    self.layout().totalSizeHint().height(),
                    self.layout().totalHeightForWidth(self.width()),
                )
        self.resize(self.width(), height)
        self.panel.layout().activate()
        if bounds and self.drag_offset is None:
            self.move(
                min(
                    max(bounds.left(), self.x()),
                    max(bounds.left(), bounds.right() - self.width() + 1),
                ),
                min(
                    max(bounds.top(), self.y()),
                    max(bounds.top(), bounds.bottom() - self.height() + 1),
                ),
            )

    def set_interactive(self, enabled):
        if not enabled and self.hotkey_error and not self.tray:
            self.show_error("Choose an available hotkey in Settings before locking the overlay.")
            return
        self.interactive = enabled
        if not enabled:
            QToolTip.hideText()
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, not enabled)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, not enabled)
        self.party.set_interactive(enabled)
        if was_visible:
            self.show()
        self.render()

    def toggle_interaction(self):
        self.set_interactive(not self.interactive)

    def toggle_visibility(self):
        self.setVisible(not self.isVisible())

    def recover(self):
        self.set_interactive(True)
        self.show()
        self.raise_()

    def restore_position(self):
        screens = QApplication.screens()
        point = QPoint(self.settings.x or 0, self.settings.y or 0)
        screen = next((s for s in screens if s.availableGeometry().contains(point)), None)
        if screen and self.settings.x is not None and self.settings.y is not None:
            bounds = screen.availableGeometry()
            self.move(
                min(max(bounds.left(), point.x()), bounds.right() - self.width() + 1),
                min(max(bounds.top(), point.y()), bounds.bottom() - self.height() + 1),
            )
        elif screens:
            bounds = screens[0].availableGeometry()
            self.move(bounds.right() - self.width() - 24, bounds.top() + 100)

    def mousePressEvent(self, event):
        if (
            self.interactive
            and event.button() == Qt.MouseButton.LeftButton
            and self.header.geometry().contains(event.position().toPoint())
        ):
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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
            self.fit_content()
            self.save_settings()
        super().mouseReleaseEvent(event)

    def save_settings(self):
        self.settings.x, self.settings.y = self.x(), self.y()
        self.settings.party_x, self.settings.party_y = self.party.x(), self.party.y()
        try:
            self.settings.save(self.settings_path)
        except OSError as exc:
            self.show_error(f"Could not save settings: {exc}")

    def open_settings(self):
        self.recover()
        dialog = QDialog(self)
        dialog.setWindowTitle("Inkbound Meter settings")
        dialog.setMinimumWidth(360)
        form = QFormLayout(dialog)
        width = QSpinBox()
        width.setRange(300, 700)
        width.setValue(self.settings.width)
        opacity = QSlider(Qt.Orientation.Horizontal)
        opacity.setObjectName("opacitySlider")
        opacity.setRange(35, 100)
        opacity.setValue(round(self.settings.opacity * 100))
        opacity_percent = QSpinBox()
        opacity_percent.setObjectName("opacityPercent")
        opacity_percent.setRange(35, 100)
        opacity_percent.setSuffix("%")
        opacity_percent.setValue(opacity.value())
        opacity.valueChanged.connect(opacity_percent.setValue)
        opacity_percent.valueChanged.connect(opacity.setValue)
        opacity_percent.valueChanged.connect(lambda value: self.set_opacity(value / 100))
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(opacity, 1)
        opacity_row.addWidget(opacity_percent)
        edit_key = QLineEdit(self.settings.interaction_key)
        show_key = QLineEdit(self.settings.visibility_key)
        form.addRow("Overlay width", width)
        form.addRow("Opacity", opacity_row)
        form.addRow("Lock / unlock", edit_key)
        form.addRow("Show / hide", show_key)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)
        buttons.rejected.connect(dialog.reject)

        def save():
            try:
                first, second = parse_hotkey(edit_key.text()), parse_hotkey(show_key.text())
                if first == second:
                    raise ValueError("Choose different keys for the two actions.")
            except ValueError as exc:
                QMessageBox.information(dialog, "Choose a shortcut", str(exc))
                return
            self.settings.width = width.value()
            self.settings.opacity = opacity.value() / 100
            self.settings.interaction_key = edit_key.text().upper().replace(" ", "")
            self.settings.visibility_key = show_key.text().upper().replace(" ", "")
            self.setFixedWidth(self.settings.width)
            self.party.setFixedWidth(self.settings.width)
            self.party.render()
            self.set_opacity(self.settings.opacity)
            self.register_hotkeys()
            self.render()
            self.save_settings()
            dialog.accept()

        buttons.accepted.connect(save)
        # Cancel (or closing the dialog) discards the live opacity preview.
        dialog.finished.connect(lambda _: self.set_opacity(self.settings.opacity))
        dialog.exec()

    def set_opacity(self, value):
        self.setWindowOpacity(value)
        self.party.setWindowOpacity(value)

    def export(self):
        self.recover()
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export captured runs", "inkbound-runs.json", "JSON files (*.json)"
        )
        if filename:
            try:
                Path(filename).write_text(
                    json.dumps(self.data["report"], indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                QMessageBox.warning(self, "Export failed", str(exc))

    def closeEvent(self, event):
        self.save_settings()
        if self.party_collector:
            self.party_collector.requestInterruption()
            if not self.party_collector.wait(5000):
                QTimer.singleShot(500, self.close)
                event.ignore()
                return
        if self.collector:
            self.collector.requestInterruption()
            if not self.collector.wait(5000):
                self.show_error("Finishing capture before closing…")
                QTimer.singleShot(500, self.close)
                event.ignore()
                return
        if self.hotkeys:
            self.hotkeys.close()
        if self.tray:
            self.tray.hide()
        self.party.watchdog.stop()
        self.party.hide()
        event.accept()
        QApplication.instance().quit()


def run_overlay(
    *,
    settings_path: Path,
    log: Path | None = None,
    database: Path | None = None,
    meter: Meter | None = None,
    mode="live",
    screenshot: Path | None = None,
    quit_after: int | None = None,
) -> int:
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Inkbound Meter")
    application.setQuitOnLastWindowClosed(False)
    overlay = Overlay(settings_path, mode=mode)
    if meter:
        overlay.update_data(payload(meter, mode))
    elif log and database:
        overlay.attach(Collector(log, database))
        overlay.start_party_capture()
    overlay.show()
    if screenshot:

        def capture():
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            overlay.grab().save(str(screenshot))

        QTimer.singleShot(700, capture)
    if quit_after:
        QTimer.singleShot(quit_after, overlay.close)
    return application.exec()
