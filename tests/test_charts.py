import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolTip

from inkbound_meter.charts import ComponentBar, component_color, component_entries


def test_segment_geometry_matches_real_frostbite_components(qapp):
    entries = component_entries(
        [
            {"name": "Base effect", "damage": 50},
            {"name": "Frostbite damage", "damage": 50},
            {"name": "Burn crossover", "damage": 13},
            {"name": "Magic damage", "damage": 39.55},
            {"name": "Omni damage", "damage": 233.91},
            {"name": "Rounding", "damage": 0.54},
        ]
    )
    bar = ComponentBar(entries)
    bar.resize(387, bar.height())
    bar.show()
    qapp.processEvents()
    segments = bar.segments()
    assert [rect.width() for rect, _ in segments] == pytest.approx(
        [50, 50, 13, 39.55, 233.91, 0.54]
    )
    assert segments[-1][0].right() == pytest.approx(387)
    # Check painted pixels as well as the layout: each major component has its color.
    picture = bar.grab().toImage()
    for rect, entry in segments[:-1]:
        assert picture.pixelColor(int(rect.center().x()), 3).name() == component_color(
            entry["name"]
        )
    bar.close()


def test_reductions_use_separate_lane_and_unresolved_remains_in_total(qapp):
    entries = component_entries(
        [
            {"name": "Base effect", "damage": 100},
            {"name": "Magic damage", "damage": 50},
            {"name": "Resistance", "damage": -30},
        ],
        unresolved=50,
    )
    bar = ComponentBar(entries)
    bar.resize(400, bar.height())
    segments = bar.segments()
    positives = [r for r, c in segments if c["damage"] > 0]
    negative = next(r for r, c in segments if c["damage"] < 0)
    assert sum(r.width() for r in positives) == pytest.approx(400)
    assert negative.width() == pytest.approx(60)
    assert negative.top() > positives[0].bottom()
    assert sum(c["damage"] for c in entries) == 170  # The net is never charted as 230.
    assert component_color("Resistance", -30) != component_color("Magic damage")
    bar.close()


def test_compact_share_and_zero_damage_are_well_defined(qapp):
    bar = ComponentBar(component_entries([], unresolved=100), compact=True, fraction=0.25)
    bar.resize(400, bar.height())
    assert bar.segments()[0][0].width() == pytest.approx(100)
    assert bar.segments()[0][1]["name"] == "Unresolved"
    bar.close()
    empty = ComponentBar([])
    empty.show()
    qapp.processEvents()
    assert empty.segments() == []
    assert not empty.grab().isNull()
    empty.close()


def test_component_tooltip_reports_the_hovered_segment(qapp):
    bar = ComponentBar([{"name": "Omni damage", "damage": 233.91}])
    bar.resize(300, bar.height())
    bar.show()
    qapp.processEvents()
    point = QPoint(30, 10)
    event = QHelpEvent(QEvent.Type.ToolTip, point, bar.mapToGlobal(point))
    QApplication.sendEvent(bar, event)
    QTest.qWait(150)  # Native Windows tooltip windows finish showing asynchronously.
    assert QToolTip.isVisible()
    assert "Omni damage" in QToolTip.text() and "233.91" in QToolTip.text()
    QToolTip.hideText()
    bar.close()
