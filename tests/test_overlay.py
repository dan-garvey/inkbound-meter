from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from inkbound_meter.config import Settings
from inkbound_meter.demo import demo_meter
from inkbound_meter.hotkeys import parse_hotkey
from inkbound_meter.overlay import Overlay, payload
from inkbound_meter.parser import Event


def test_efficiency_label_covers_rounds_and_only_applicable_will_output():
    from inkbound_meter.overlay import efficiency_label

    assert (
        efficiency_label({"rounds": 3, "per_round": 12.5, "will_spent": 2, "per_will": 18.75})
        == "12.5 / round  ·  18.75 / Will"
    )
    assert efficiency_label({"rounds": 3, "per_round": 4, "will_spent": 2, "per_will": None}) == (
        "4 / round  ·  — / Will"
    )


def test_trend_points_use_each_encounters_party_total_per_round():
    from inkbound_meter.overlay import trend_points

    snapshot = {
        "encounter_history": [
            {
                "number": 1,
                "active": False,
                "rounds": 2,
                "damage": 80,
                "metrics": {"healing": 12},
                "changes": [],
            },
            {
                "number": 2,
                "active": True,
                "rounds": 0,
                "damage": 30,
                "metrics": {"healing": 9},
                "changes": [],
            },
        ]
    }
    assert [point["per_round"] for point in trend_points(snapshot, "damage")] == [40, None]
    assert trend_points(snapshot, "healing")[0]["per_round"] == 6


def test_encounter_trends_dialog_renders_a_chart_and_observed_changes(qapp, tmp_path):
    from inkbound_meter.charts import EncounterTrend

    window = Overlay(tmp_path / "settings.json", mode="demo", register_hotkeys=False)
    meter = demo_meter()
    meter.apply(
        Event("loadout", data={"id": 1, "kind": "gear", "item": "VestigeAll_Epic_SmiteAll_Equip"})
    )
    meter.apply(Event("combat_start", data={"zone": 2}))
    window.update_data(payload(meter, "demo"))
    window.open_trends()
    qapp.processEvents()
    dialog = window.trends_dialog
    assert dialog is not None
    assert dialog.findChild(EncounterTrend) is not None
    assert "Vestige: Epic Smite All" in " ".join(
        child.text() for child in dialog.changes.findChildren(QLabel)
    )
    dialog.close()
    window.close()


def assert_content_fits(window):
    assert window.scroll.y() > window.tabs["run"].geometry().bottom()
    bottom = window.note.y() if window.note.isVisible() else window.panel.height()
    assert window.scroll.geometry().bottom() < bottom


def test_scope_expansion_and_recovery_preserve_real_totals(qapp, tmp_path):
    window = Overlay(tmp_path / "settings.json", mode="demo", register_hotkeys=False)
    meter = demo_meter()
    meter.apply(Event("combat_start", data={"zone": 2}))
    window.update_data(payload(meter, "demo"))
    window.show()
    qapp.processEvents()
    assert window.total.text() == "0"
    QTest.mouseClick(window.tabs["run"], Qt.MouseButton.LeftButton)
    assert window.total.text() == "65.2k"
    before = window.height()
    window.toggle_player(1)
    qapp.processEvents()
    assert window.height() > before
    assert_content_fits(window)
    assert 1 in window.expanded
    window.set_interactive(False)
    assert window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    window.hide()
    window.recover()
    assert window.isVisible() and window.interactive
    assert not (window.windowFlags() & Qt.WindowType.WindowTransparentForInput)
    assert window.total.text() == "65.2k"
    window.close()
    assert Settings.load(tmp_path / "settings.json").scope == "run"


def test_overlay_rows_have_dark_readable_background(qapp, tmp_path):
    window = Overlay(tmp_path / "settings.json", mode="demo", register_hotkeys=False)
    window.update_data(payload(demo_meter(), "demo"))
    window.show()
    qapp.processEvents()
    image = window.scroll.widget().grab().toImage()
    # A margin inside the scroll body must not regress to the platform's white palette.
    color = image.pixelColor(image.width() - 2, 1)
    assert max(color.red(), color.green(), color.blue()) < 90
    assert "Demo" in window.status.toolTip()
    window.close()


def test_multiplayer_growth_stays_inside_screen_and_all_players_scroll_into_view(qapp, tmp_path):
    from PySide6.QtWidgets import QPushButton

    window = Overlay(tmp_path / "settings.json", mode="demo", register_hotkeys=False)
    window.update_data(payload(demo_meter(), "demo"))
    window.show()
    qapp.processEvents()
    bounds = window.screen().availableGeometry()
    window.move(bounds.right() - window.width(), bounds.bottom() - window.height())
    before_y, before_height = window.y(), window.height()
    for entity in (1, 2, 3):
        window.toggle_player(entity)
    qapp.processEvents()
    assert window.height() > before_height
    assert window.y() < before_y
    assert bounds.contains(window.frameGeometry())
    assert_content_fits(window)
    window.scroll.verticalScrollBar().setValue(window.scroll.verticalScrollBar().maximum())
    qapp.processEvents()
    last_source = window.scroll.widget().findChildren(QPushButton, "source")[-1]
    position = last_source.mapTo(window.scroll.viewport(), last_source.rect().center())
    assert window.scroll.viewport().rect().contains(position)
    assert window.total.text() == "65.2k"
    window.close()


def test_short_display_uses_scroll_area_to_keep_controls_visible(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication

    window = Overlay(tmp_path / "settings.json", mode="demo", register_hotkeys=False)
    window.show()
    bounds = QRect(-800, 100, 800, 300)

    class ShortScreen:
        def availableGeometry(self):
            return bounds

    monkeypatch.setattr(QApplication, "screenAt", lambda point: ShortScreen())
    window.move(-500, 200)
    window.update_data(payload(demo_meter(), "demo"))
    for entity in (1, 2, 3):
        window.toggle_player(entity)
    qapp.processEvents()
    assert bounds.contains(window.geometry())
    assert window.scroll.height() < window._rows_height
    assert window.scroll.verticalScrollBar().maximum() > 0
    assert_content_fits(window)
    window.close()


def test_live_names_refresh_before_attacks_without_rewriting_history(qapp, tmp_path):
    from copy import deepcopy

    from PySide6.QtWidgets import QPushButton

    from .test_party import party_sample

    meter = demo_meter()
    meter.apply(Event("player", data={"id": 4, "class_id": "C08"}))
    original_report = deepcopy(meter.report())
    window = Overlay(tmp_path / "settings.json", register_hotkeys=False)
    window.update_data(payload(meter, "live"))
    original_data = deepcopy(window.data)
    memory = party_sample()
    for player in memory["players"]:
        player["name"] = f"Live {player['id']}"
    window.party.update_memory(memory)
    # Party-reader updates alone must refresh the meter, without another log event.
    body = window.scroll.widget()
    assert any("Live 4" in b.text() for b in body.findChildren(QPushButton))
    assert any("Chainbreaker" in b.toolTip() for b in body.findChildren(QPushButton))
    resources_changed = deepcopy(memory)
    resources_changed["players"][0]["will"] = 0
    window.party.update_memory(resources_changed)
    assert window.scroll.widget() is body  # Preserve hover while will/HP change.
    assert window.data == original_data
    assert meter.report() == original_report
    window.party.memory_at -= 3
    window.party.render()
    buttons = window.scroll.widget().findChildren(QPushButton)
    assert not any("Live 4" in b.text() for b in buttons)
    assert any("Player 4" in b.text() for b in buttons)
    assert window.total.text() == "65.2k"
    window.close()


def test_live_names_never_relabel_other_runs_replays_or_mismatched_parties(qapp, tmp_path):
    from copy import deepcopy

    from .test_party import party_sample

    meter = demo_meter()
    meter.apply(Event("player", data={"id": 4, "class_id": "C09"}))
    window = Overlay(tmp_path / "settings.json", register_hotkeys=False)
    data = payload(meter, "live")
    memory = party_sample()
    window.update_data(data)
    window.party.update_memory(memory)
    snapshot = data["run"]
    assert window.display_identities(snapshot, snapshot) is not snapshot
    assert next(p["class"] for p in snapshot["players"] if p["id"] == 4) == "Godkeeper"
    for mode, status, connected, outcome, seed, roster in (
        ("replay", "live", True, "in_progress", 123456, memory["players"]),
        ("live", "catching up", True, "in_progress", 123456, memory["players"]),
        ("live", "live", False, "in_progress", 123456, memory["players"]),
        ("live", "live", True, "defeat", 123456, memory["players"]),
        ("live", "live", True, "in_progress", 123457, memory["players"]),
        ("live", "live", True, "in_progress", 123456, memory["players"][:-1]),
    ):
        window.mode = mode
        window.data = {**data, "status": status, "connected": connected}
        window.party.data = {**deepcopy(memory), "players": roster}
        candidate = {**snapshot, "outcome": outcome, "seed": seed}
        assert window.display_identities(candidate, candidate) is candidate
    window.close()


def test_empty_state_and_capture_diagnostics_are_visible(qapp, tmp_path):
    window = Overlay(tmp_path / "settings.json", register_hotkeys=False)
    meter = demo_meter()
    meter.current.partial = True
    meter.apply(Event("parse_error", data={"message": "Unsupported field"}))
    window.update_data(payload(meter, "live"))
    assert "Partial capture" in window.note.text()
    assert "unsupported records" in window.note.text()
    window.show_error("Capture stopped: disk full")
    assert "disk full" in window.status.toolTip()
    assert "disk full" in window.note.text()
    window.close()


def test_hotkey_validation_and_distinct_modifier_combinations():
    import pytest

    assert parse_hotkey("F8") == (0x4000, 0x77)
    assert parse_hotkey("Ctrl+Shift+F9") == (0x4006, 0x78)
    assert parse_hotkey("alt+m") == (0x4001, ord("M"))
    for bad in ("F12", "F25", "M", "Meta+F8", "F8,F9", ""):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


def test_source_expands_into_components_and_last_hit_without_changing_totals(qapp, tmp_path):
    from pathlib import Path

    from PySide6.QtWidgets import QLabel, QPushButton

    from inkbound_meter.service import replay

    meter = replay(Path(__file__).parent / "fixtures/frostbite_components.txt")
    window = Overlay(tmp_path / "settings.json", mode="replay", register_hotkeys=False)
    window.update_data(payload(meter, "replay"))
    window.select_scope("run")
    window.toggle_player(17)
    window.show()
    qapp.processEvents()
    source = window.findChildren(QPushButton, "source")[0]
    QTest.mouseClick(source, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    texts = [v.text() for v in window.findChildren(QLabel)]
    assert all(
        name in texts
        for name in (
            "Base effect",
            "Frostbite damage",
            "Magic damage",
            "Omni damage",
            "Burn crossover",
            "Unresolved",
        )
    )
    assert "75 / 76 hits matched" in texts
    last = next(v for v in window.findChildren(QPushButton) if v.text() == "Last hit")
    QTest.mouseClick(last, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert window.findChild(QLabel, "hitDamage").text() == "387"
    assert "Matched hit" in [v.text() for v in window.findChildren(QLabel)]
    assert window.total.text() == "21.8k"
    window.update_data(payload(meter, "replay"))
    qapp.processEvents()
    assert window.findChild(QLabel, "hitDamage").text() == "387"
    assert_content_fits(window)
    window.close()


def test_hit_stats_are_clickable_and_survive_refresh_and_next_hit(qapp, tmp_path):
    from copy import deepcopy
    from pathlib import Path

    from PySide6.QtWidgets import QFrame, QLabel, QPushButton

    from inkbound_meter.service import replay

    meter = replay(Path(__file__).parent / "fixtures/frostbite_components.txt")
    window = Overlay(tmp_path / "settings.json", mode="replay", register_hotkeys=False)
    window.update_data(payload(meter, "replay"))
    window.select_scope("run")
    key = (17, "FrostBite_Damage_StatusEffect_Action")
    window.toggle_player(17)
    window.toggle_source(key)
    window.select_source_mode(key, True)
    window.show()
    qapp.processEvents()
    button = window.findChild(QPushButton, "disclosure")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    panel = window.findChild(QFrame, "hitStats")
    assert panel.isVisible()
    assert "207" in [w.text() for w in panel.findChildren(QLabel)]
    # Background log activity must not destroy hovered widgets or focus.
    body = window.scroll.widget()
    window.update_data(payload(meter, "live"))
    assert window.scroll.widget() is body
    assert window.findChild(QFrame, "hitStats") is panel
    # An actual new hit refreshes the inputs without collapsing their disclosure.
    data = deepcopy(window.data)
    data["run"]["players"][0]["sources"][0]["breakdown"]["latest"]["inputs"]["Omni damage"] = 259
    window.update_data(data)
    qapp.processEvents()
    panel = window.findChild(QFrame, "hitStats")
    assert panel.isVisible()
    assert "259" in [w.text() for w in panel.findChildren(QLabel)]
    assert "207" not in [w.text() for w in panel.findChildren(QLabel)]
    assert window.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
    assert_content_fits(window)
    window.close()


def test_unmatched_prediction_has_distinct_chart_and_logged_total(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from inkbound_meter.charts import ComponentBar
    from inkbound_meter.overlay import SourceRow

    data = {
        "id": "example",
        "name": "Example",
        "damage": 120,
        "share": 1,
        "breakdown": {
            "matched_damage": 0,
            "unresolved_damage": 120,
            "components": [],
            "latest": {
                "observed": 120,
                "predicted": 80,
                "status": "mismatch",
                "components": [{"name": "Base effect", "damage": 80}],
            },
        },
    }
    row = SourceRow(data, True, True, lambda: None, lambda value: None)
    row.show()
    qapp.processEvents()
    assert row.findChild(QLabel, "hitDamage").text() == "120"
    assert row.findChild(ComponentBar).prediction
    assert "80" in row.findChild(QLabel, "predictionNotice").text()
    assert "excluded" in row.findChild(QLabel, "predictionNotice").text()
    row.close()


def test_opacity_percentage_previews_cancels_and_saves(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QSlider, QSpinBox

    path = tmp_path / "settings.json"
    window = Overlay(path, register_hotkeys=False)
    monkeypatch.setattr(window, "register_hotkeys", lambda: None)
    window.show()
    observed = []

    def edit(value, action):
        dialog = QApplication.activeModalWidget()
        try:
            number = dialog.findChild(QSpinBox, "opacityPercent")
            slider = dialog.findChild(QSlider, "opacitySlider")
            observed.append((number.value(), number.suffix()))
            number.setValue(value)
            observed.append((slider.value(), window.windowOpacity()))
            buttons = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(buttons.button(action), Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, lambda: edit(75, QDialogButtonBox.StandardButton.Cancel))
    window.open_settings()
    assert observed[0] == (50, "%")
    assert observed[1][0] == 75 and abs(observed[1][1] - 0.75) < 0.005
    assert window.settings.opacity == 0.5 and abs(window.windowOpacity() - 0.5) < 0.005
    QTimer.singleShot(0, lambda: edit(65, QDialogButtonBox.StandardButton.Save))
    window.open_settings()
    assert Settings.load(path).opacity == 0.65
    assert abs(window.windowOpacity() - 0.65) < 0.005
    window.close()


def test_settings_arrow_clicks_reach_both_spinboxes(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QDialogButtonBox,
        QSlider,
        QSpinBox,
        QStyle,
        QStyleOptionSpinBox,
    )

    path = tmp_path / "settings.json"
    window = Overlay(path, register_hotkeys=False)
    monkeypatch.setattr(window, "register_hotkeys", lambda: None)
    window.show()
    clicks = []
    preview = []
    errors = []

    def edit():
        dialog = QApplication.activeModalWidget()
        try:
            for spin in dialog.findChildren(QSpinBox):
                for control, change in (
                    (QStyle.SubControl.SC_SpinBoxUp, 1),
                    (QStyle.SubControl.SC_SpinBoxDown, -1),
                    (QStyle.SubControl.SC_SpinBoxUp, 1),
                ):
                    option = QStyleOptionSpinBox()
                    spin.initStyleOption(option)
                    rect = spin.style().subControlRect(
                        QStyle.ComplexControl.CC_SpinBox, option, control, spin
                    )
                    # Clicking the spinbox directly bypasses the child editor, hiding
                    # the Windows bug where it covered the up arrow's hit area.
                    target = spin.childAt(rect.center()) or spin
                    point = target.mapFromGlobal(spin.mapToGlobal(rect.center()))
                    before = spin.value()
                    QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
                    clicks.append((spin.value(), before + change))
            preview.append(dialog.findChild(QSlider, "opacitySlider").value())
            preview.append(window.windowOpacity())
            buttons = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(
                buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, edit)
    window.open_settings()
    window.close()
    assert not errors
    assert len(clicks) == 6 and all(actual == expected for actual, expected in clicks), clicks
    assert preview[0] == 51 and abs(preview[1] - 0.51) < 0.005
    saved = Settings.load(path)
    assert saved.width == 361 and saved.opacity == 0.51


def test_support_views_switch_and_keep_compact_component_bars(qapp, tmp_path):
    from inkbound_meter.charts import ComponentBar

    from .test_support import apply_log, support_log

    path = tmp_path / "settings.json"
    Settings(width=300, opacity=0.6).save(path)
    window = Overlay(path, mode="replay", register_hotkeys=False)
    window.update_data(payload(apply_log(support_log()), "replay"))
    window.show()
    window.toggle_player(1)
    for metric, amount, expected in (
        ("shielding", "15", {"Absorbed": 10}),
        ("healing", "4", {"HP restored": 1, "Overheal": 3}),
        ("taken", "22", {}),
        ("pressure", "2", {}),
    ):
        window.select_metric(metric)
        qapp.processEvents()
        assert window.total.text() == amount
        assert window.settings.metric == metric
        assert window.width() == 300
        assert_content_fits(window)
        if expected:
            bar = next(b for b in window.findChildren(ComponentBar) if not b.compact)
            assert {c["name"]: c["damage"] for c in bar.entries} == expected
        body = window.scroll.widget()
        window.update_data(payload(apply_log(support_log()), "replay"))
        assert window.scroll.widget() is body
    assert "pressure proxy" in window.heading.toolTip()
    assert Settings.load(path).metric == "pressure"
    assert Settings.load(path).opacity == 0.6
    window.select_metric("damage")
    assert window.total.text() == "0"
    window.close()


def test_healed_view_credits_multiplayer_caster_with_restored_and_overheal_bar(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from inkbound_meter.charts import ComponentBar

    from .test_healing import cast, healed
    from .test_support import apply_log, setup_log

    meter = apply_log(setup_log())
    cast(meter)
    healed(meter)
    path = tmp_path / "settings.json"
    Settings(width=300, metric="healing", scope="run").save(path)
    window = Overlay(path, mode="replay", register_hotkeys=False)
    window.update_data(payload(meter, "replay"))
    window.toggle_player(1)
    window.show()
    qapp.processEvents()
    assert window.total.text() == "4"
    assert window.width() == 300
    assert_content_fits(window)
    bars = [b for b in window.findChildren(ComponentBar) if not b.compact]
    assert len(bars) == 1
    assert {v["name"]: v["damage"] for v in bars[0].entries} == {
        "HP restored": 1,
        "Overheal": 3,
    }
    captions = [label.text() for label in window.findChildren(QLabel)]
    assert "Restoration Heal" in captions
    assert "→ Tank" in captions
    assert not any("Overheal unavailable" in caption for caption in captions)
    window.close()


def test_taken_bar_shows_blur_set_and_shield_without_inflating_total(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from inkbound_meter.charts import ComponentBar, component_color

    from .test_mitigation import supported_meter

    path = tmp_path / "settings.json"
    Settings(width=300, metric="taken", scope="run").save(path)
    window = Overlay(path, mode="replay", register_hotkeys=False)
    window.update_data(payload(supported_meter(), "replay"))
    window.show()
    qapp.processEvents()
    assert window.total.text() == "14"
    bar = next(b for b in window.findChildren(ComponentBar) if len(b.entries) == 4)
    assert {v["name"]: v["damage"] for v in bar.entries} == {
        "Health lost": 8,
        "Absorbed": 6,
        "Warden set (5)": 4,
        "Blur": 2,
    }
    assert bar.compact and bar.positive == 20
    assert len({component_color(c["name"]) for c in bar.entries}) == 4
    assert window.findChild(QLabel, "mitigationSummary").text() == "6 prevented · 6 absorbed"
    window.toggle_player(2)
    qapp.processEvents()
    assert window.total.text() == "14" and window.width() == 300
    assert_content_fits(window)
    assert any(caption.text() == "Shield from Healer" for caption in window.findChildren(QLabel))
    assert any(not b.compact and b.positive == 20 for b in window.findChildren(ComponentBar))
    window.close()


def test_fully_prevented_hit_has_a_visible_bar_and_unknown_prevention_is_explicit(qapp):
    from PySide6.QtWidgets import QLabel

    from inkbound_meter.charts import ComponentBar
    from inkbound_meter.overlay import SupportRow

    from .test_mitigation import add_status, hit, mitigation_meter, set_stat
    from .test_support import player

    meter = mitigation_meter()
    set_stat(meter, "TWM45OpA", 30)
    add_status(meter, "Blur_StatusEffect", 30)
    hit(meter, 0)
    row = SupportRow(player(meter, "taken", 2), "taken", False, lambda: None)
    row.resize(300, 100)
    row.show()
    qapp.processEvents()
    bar = row.findChild(ComponentBar)
    assert bar.positive == 20 and bar.fraction == 1 and bar.segments()
    assert row.findChild(QLabel, "mitigationSummary").text() == "20 prevented · 0 absorbed"
    row.close()
    meter = mitigation_meter()
    hit(meter, 10, action="UnknownAttack")
    row = SupportRow(player(meter, "taken", 2), "taken", True, lambda: None)
    assert row.findChild(QLabel, "mitigationSummary").text() == "Prevention unknown · 0 absorbed"
    assert any("unknown for 1 hits" in caption.text() for caption in row.findChildren(QLabel))
    row.close()
