from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QMessageBox, QPushButton, QTreeWidget


@pytest.fixture
def image_folder(tmp_path):
    for index, color in enumerate(("red", "blue", "green")):
        Image.new("RGB", (40, 20), color).save(tmp_path / f"{index}.png")
        (tmp_path / f"{index}.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 20">'
            '<circle id="point" cx="10" cy="10" r="2" fill="yellow"/></svg>'
        )
    return tmp_path


def test_qt_widgets_paint_and_navigate_preserving_view(app, qt_application, image_folder):
    app.show()
    qt_application.processEvents()
    app.open_folder(image_folder)
    app.zoom_factor = 5
    app.offset_x, app.offset_y = 50, -25
    app.update_canvas()
    snapshot = app.canvas.grab().toImage()
    assert not snapshot.isNull()
    assert app.photo.width() <= app.canvas.width()
    assert app.overlay_photo.height() <= app.canvas.height()
    next_button = next(button for button in app.findChildren(QPushButton) if button.text() == "Next ▶")
    QTest.mouseClick(next_button, Qt.MouseButton.LeftButton)
    assert app.current_index == 1
    assert app.filename_label.text() == "1.png"
    assert (app.zoom_factor, app.offset_x, app.offset_y) == (5, 50, -25)
    app.canvas.setFocus()
    QTest.keyClick(app.canvas, Qt.Key.Key_Left)
    assert app.current_index == 0


def test_real_wheel_drag_and_middle_double_click(app, qt_application, image_folder):
    app.show()
    qt_application.processEvents()
    app.open_folder(image_folder)
    point = QPoint(123, 101)
    left, top, width, height = app.image_bounds
    before = ((point.x() - left) / width, (point.y() - top) / height)
    event = QWheelEvent(
        QPointF(point),
        QPointF(app.canvas.mapToGlobal(point)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(app.canvas, event)
    left, top, width, height = app.image_bounds
    assert app.zoom_factor == pytest.approx(1.1)
    assert ((point.x() - left) / width, (point.y() - top) / height) == pytest.approx(before, abs=0.002)
    previous_offset = (app.offset_x, app.offset_y)
    QTest.mousePress(app.canvas, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(app.canvas, point + QPoint(20, 30))
    QTest.mouseRelease(app.canvas, Qt.MouseButton.LeftButton, pos=point + QPoint(20, 30))
    assert (app.offset_x, app.offset_y) == pytest.approx((previous_offset[0] + 20, previous_offset[1] + 30))
    QTest.mouseDClick(app.canvas, Qt.MouseButton.MiddleButton, pos=point)
    assert (app.zoom_factor, app.offset_x, app.offset_y) == (1, 0, 0)


def test_shortcuts_classify_but_allow_typing_in_inputs(app, qt_application, image_folder):
    app.classifications = ["Accepted"]
    app.create_classification_buttons()
    app.show()
    qt_application.processEvents()
    app.open_folder(image_folder)
    app.index_entry.setFocus()
    app.index_entry.selectAll()
    QTest.keyClicks(app.index_entry, "1")
    assert app.index_entry.text() == "1"
    assert app.classified_map == {}
    app.canvas.setFocus()
    QTest.keyClick(app.canvas, Qt.Key.Key_1)
    assert app.classified_map == {"0.png": "Accepted"}
    assert app.current_index == 1


def test_playback_uses_qt_timer_and_stops_at_end(app, image_folder):
    app.open_folder(image_folder)
    app.zoom_factor = 3
    app.offset_x, app.offset_y = 22, -33
    app.delay_entry.setValue(10)
    app.toggle_play()
    QTest.qWait(80)
    assert app.current_index == 2
    assert not app.is_playing
    assert not app.play_timer.isActive()
    assert (app.zoom_factor, app.offset_x, app.offset_y) == (3, 22, -33)


def test_metadata_and_clipboard_follow_navigation(app, image_folder):
    app.open_folder(image_folder)
    app.toggle_image_details()
    assert app.details_tree.topLevelItemCount() >= 2
    file_section = app.details_tree.topLevelItem(0)
    name = next(
        file_section.child(i) for i in range(file_section.childCount()) if file_section.child(i).text(0) == "Name"
    )
    app.details_tree.setCurrentItem(name)
    app.copy_selected_detail()
    assert QApplication.clipboard().text() == "0.png"
    app.show_next_image()
    assert app.details_tree.topLevelItem(0).child(0).text(1) == "1.png"


def test_define_classes_dialog_saves_on_enter(app):
    def fill():
        dialog = QApplication.activeModalWidget()
        entries = dialog.findChildren(QLineEdit)
        entries[0].setText("Accepted")
        entries[1].setText("Rejected")
        QTest.keyClick(entries[1], Qt.Key.Key_Return)

    QTimer.singleShot(0, fill)
    app.define_classes()
    assert app.classifications == ["Accepted", "Rejected"]
    assert len(app.class_shortcuts) == 2


def test_anchor_dialog_locks_without_moving_point(app, image_folder):
    app.open_folder(image_folder)
    anchor = app.anchors[0]
    left, top, width, height = app.image_bounds
    expected = (left + anchor.x * width, top + anchor.y * height)

    def select():
        dialog = QApplication.activeModalWidget()
        tree = dialog.findChild(QTreeWidget)
        tree.setCurrentItem(tree.topLevelItem(0))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, select)
    app.select_anchor()
    assert app.anchor_key == anchor.key
    left, top, width, height = app.image_bounds
    assert (left + anchor.x * width, top + anchor.y * height) == pytest.approx(expected, abs=1)
    app.show_next_image()
    assert app.anchor_key == anchor.key


def test_elements_dialog_hides_overlay_and_carries_selection(app, image_folder):
    app.open_folder(image_folder)

    def hide():
        dialog = QApplication.activeModalWidget()
        tree = dialog.findChild(QTreeWidget)
        tree.setCurrentItem(tree.topLevelItem(0))
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        buttons["Hide selected"].click()
        buttons["Close"].click()

    QTimer.singleShot(0, hide)
    app.select_svg_elements()
    assert app.hidden_svg_elements == {"id:point"}
    assert app.overlay_photo.pixelColor(app.overlay_photo.width() // 2, app.overlay_photo.height() // 2).alpha() == 0
    app.show_next_image()
    assert app.hidden_svg_elements == {"id:point"}
    app.overlay_enabled.setChecked(False)
    assert app.overlay_photo is None
    assert not app.elements_button.isEnabled()


@pytest.mark.parametrize("operation", ["Move", "Copy"])
def test_background_transfer_completes_and_restores_controls(app, image_folder, monkeypatch, operation):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    app.open_folder(image_folder)
    app.classified_map = {"0.png": "Accepted"}
    app.transfer_mode_selector.setCurrentText(operation)
    app.move_classified_images()
    assert not app.transfer_button.isEnabled()
    assert app.transfer_timer.isActive()
    future = app.transfer_future
    assert future.result(timeout=5) == (1, [])
    app._poll_transfer()
    assert (image_folder / "Accepted" / "0.png").exists()
    assert (image_folder / "Accepted" / "0.svg").exists()
    assert (image_folder / "0.png").exists() == (operation == "Copy")
    assert app.transfer_future is None
    assert not app.transfer_timer.isActive()
    assert app.transfer_button.isEnabled()


def test_transfer_worker_failure_restores_controls(app, monkeypatch):
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    future = Future()
    future.set_exception(OSError("Transfer failed"))
    app.transfer_future = future
    app.transfer_operation = "Copy"
    app.transfer_button.setEnabled(False)
    app._poll_transfer()
    assert app.transfer_future is None
    assert app.transfer_button.isEnabled()
    assert "Transfer failed" in messages[0]


def test_empty_folder_clears_previous_display(app, image_folder, tmp_path):
    app.open_folder(image_folder)
    empty = tmp_path / "empty"
    empty.mkdir()
    app.open_folder(empty)
    assert app.images == []
    assert app.image is None
    assert app.photo is None
    assert app.overlay_photo is None
    assert app.total_label.text() == "/ 0"


def test_sample_overlays_render_during_zoomed_navigation(app):
    app.open_folder(Path(__file__).parents[1] / "examples" / "images")
    app.zoom_image(SimpleNamespace(x=100, y=100, delta=120))
    app.show_next_image()
    assert app.overlay_photo is not None
    assert not app.overlay_photo.isNull()
