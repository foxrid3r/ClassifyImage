from __future__ import annotations

import os
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from queue import Empty, SimpleQueue
from types import SimpleNamespace
from xml.etree import ElementTree

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .anchors import svg_anchors
from .core import (  # noqa: F401 -- retain the existing public helper imports.
    MAX_CLASSES,
    MIN_PLAY_DELAY_MS,
    SUPPORTED_EXTENSIONS,
    _svg_viewport_size,
    fitted_size,
    image_metadata,
    matching_svg_path,
    svg_graphics,
    svg_with_line_width,
    transfer_classified_files,
    visible_svg_graphics,
)
from .icons import application_icon


def qt_image(image: Image.Image) -> QImage:
    """Own a Qt copy so pixel storage outlives the temporary Pillow buffer."""
    rgba = image.convert("RGBA")
    return QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()


class ImageCanvas(QWidget):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(1, 1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("black"))
        app = self.app
        if app.image_bounds is None:
            return
        left, top, width, height = app.image_bounds
        if app.photo is not None:
            painter.drawImage(QPointF(*app.photo_position), app.photo)
        if app.overlay_photo is not None:
            painter.drawImage(QPointF(*app.photo_position), app.overlay_photo)
        painter.setPen(QPen(QColor("yellow"), 2))
        if app.anchor_preview is not None:
            self._cross(painter, left + app.anchor_preview.x * width, top + app.anchor_preview.y * height)
        for x0, y0, x1, y1 in app.element_previews:
            painter.drawRect(
                QRectF(left + x0 * width - 3, top + y0 * height - 3, (x1 - x0) * width + 6, (y1 - y0) * height + 6)
            )
            self._cross(painter, left + (x0 + x1) * width / 2, top + (y0 + y1) * height / 2)
        if app.images and app.images[app.current_index] in app.classified_map:
            painter.setPen(QPen(QColor("#32cd32"), 3))
            painter.drawRect(QRectF(left, top, width, height))

    @staticmethod
    def _cross(painter, x, y):
        painter.drawEllipse(QPointF(x, y), 8, 8)
        painter.drawLine(QPointF(x - 14, y), QPointF(x + 14, y))
        painter.drawLine(QPointF(x, y - 14), QPointF(x, y + 14))

    def resizeEvent(self, event):
        self.app.update_canvas()
        super().resizeEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.app.zoom_image(SimpleNamespace(x=event.position().x(), y=event.position().y(), delta=delta))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.app.start_pan(SimpleNamespace(x=event.position().x(), y=event.position().y()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        position = SimpleNamespace(x=event.position().x(), y=event.position().y())
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.app.pan_image(position)
        else:
            self.app.inspect_pixel(position)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.app.fit_image_to_window()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        self.app.clear_pixel()
        super().leaveEvent(event)


class ImageClassifierApp(QMainWindow):
    """Qt desktop viewer for interactively sorting images into class folders."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ClassifyImage")
        self.setWindowIcon(application_icon())
        self.resize(1100, 750)
        self.setMinimumSize(700, 500)
        self.folder_path: Path | None = None
        self.images = []
        self.current_index = 0
        self.classifications = []
        self.classified_map = {}
        self.overlay_paths = {}
        self.zoom_factor = 1.0
        self.offset_x = self.offset_y = 0.0
        self.start_x = self.start_y = 0.0
        self.anchor_key = None
        self.anchors = []
        self.anchor_preview = None
        self.element_previews = []
        self.image = None
        self.image_bounds = None
        self.pixel_pointer = None
        self.photo = self.overlay_photo = None
        self.photo_position = (0, 0)
        self.rendered_image_cache = None
        self.overlay_path = None
        self.overlay_cache = OrderedDict()
        self.hidden_svg_elements = set()
        self.overlay_line_width = 1.0
        self.overlay_text_size = 16.0
        self.details_visible = False
        self.is_playing = False
        self.play_delay_ms = 1000
        self.image_loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-loader")
        self.prefetched_images: dict[Path, Future] = {}
        self.transfer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-transfer")
        self.transfer_future = None
        self.transfer_updates = SimpleQueue()
        self.transfer_folder = None
        self.transfer_operation = ""
        self.class_shortcuts = []
        self.play_timer = QTimer(self)
        self.play_timer.setSingleShot(True)
        self.play_timer.timeout.connect(self.play_images)
        self.transfer_timer = QTimer(self)
        self.transfer_timer.setInterval(50)
        self.transfer_timer.timeout.connect(self._poll_transfer)
        self._create_widgets()

    @staticmethod
    def _button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _create_widgets(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.select_folder_button = self._button(actions, "Select Folder", self.select_folder)
        self._button(actions, "Define Classes", self.define_classes)
        self.details_button = self._button(actions, "Show Details", self.toggle_image_details)
        actions.addStretch()
        actions.addWidget(QLabel("Classified files:"))
        self.transfer_mode_selector = QComboBox()
        self.transfer_mode_selector.addItems(["Move", "Copy"])
        actions.addWidget(self.transfer_mode_selector)
        self.transfer_button = self._button(actions, "Move Classified", self.move_classified_images)
        self.transfer_mode_selector.currentTextChanged.connect(self._update_transfer_button)
        overlays = QHBoxLayout()
        layout.addLayout(overlays)
        self.overlay_enabled = QCheckBox("Show SVG Overlay")
        self.overlay_enabled.setChecked(True)
        overlays.addWidget(self.overlay_enabled)
        self.overlay_enabled.toggled.connect(self.toggle_svg_overlay)
        overlays.addWidget(QLabel("Line width"))
        self.overlay_line_width_spinbox = QDoubleSpinBox()
        self.overlay_line_width_spinbox.setRange(0.1, 100)
        self.overlay_line_width_spinbox.setSingleStep(0.5)
        self.overlay_line_width_spinbox.setValue(1)
        self.overlay_line_width_spinbox.valueChanged.connect(self.set_overlay_line_width)
        overlays.addWidget(self.overlay_line_width_spinbox)
        overlays.addWidget(QLabel("Text size"))
        self.overlay_text_size_spinbox = QDoubleSpinBox()
        self.overlay_text_size_spinbox.setRange(1, 200)
        self.overlay_text_size_spinbox.setValue(16)
        self.overlay_text_size_spinbox.valueChanged.connect(self.set_overlay_text_size)
        overlays.addWidget(self.overlay_text_size_spinbox)
        self._button(overlays, "Select Anchor", self.select_anchor)
        self._button(overlays, "Unlock", self.unlock_anchor)
        self.elements_button = self._button(overlays, "Elements…", self.select_svg_elements)
        overlays.addStretch()
        self.anchor_status = QLabel("Anchor off")
        self.pixel_status = QLabel("Hover over the image to inspect pixels")
        layout.addWidget(self.anchor_status)
        layout.addWidget(self.pixel_status)
        self.content_pane = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = ImageCanvas(self)
        self.content_pane.addWidget(self.canvas)
        self.details_frame = QWidget()
        details = QVBoxLayout(self.details_frame)
        header = QHBoxLayout()
        header.addWidget(QLabel("Image Inspector"))
        self._button(header, "Close", self.toggle_image_details)
        details.addLayout(header)
        self.details_tree = QTreeWidget()
        self.details_tree.setHeaderLabels(["Property", "Value"])
        self.details_tree.setColumnWidth(0, 160)
        details.addWidget(self.details_tree)
        self._button(details, "Copy Selected Value", self.copy_selected_detail)
        self.content_pane.addWidget(self.details_frame)
        self.content_pane.setStretchFactor(0, 4)
        self.content_pane.setStretchFactor(1, 2)
        self.details_frame.hide()
        layout.addWidget(self.content_pane, 1)
        self.directory_label = QLabel("No folder selected")
        self.directory_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.directory_label)
        self.classification_layout = QHBoxLayout()
        layout.addLayout(self.classification_layout)
        self.create_classification_buttons()
        status = QHBoxLayout()
        self.filename_label = QLabel()
        self.status_label = QLabel()
        status.addWidget(self.filename_label)
        status.addWidget(self.status_label)
        status.addStretch()
        layout.addLayout(status)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.hide()
        layout.addWidget(self.transfer_progress)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        controls.addStretch()
        controls.addWidget(QLabel("Delay (ms)"))
        self.delay_entry = QSpinBox()
        self.delay_entry.setRange(MIN_PLAY_DELAY_MS, 2147483647)
        self.delay_entry.setValue(self.play_delay_ms)
        controls.addWidget(self.delay_entry)
        self.play_button = self._button(controls, "Play", self.toggle_play)
        self._button(controls, "◀ Previous", self.show_previous_image)
        self._button(controls, "Next ▶", self.show_next_image)
        self.index_entry = QLineEdit("0")
        self.index_entry.setFixedWidth(65)
        self.index_entry.returnPressed.connect(self.jump_to_index)
        controls.addWidget(self.index_entry)
        self.total_label = QLabel("/ 0")
        controls.addWidget(self.total_label)
        controls.addStretch()
        for key, action in (("Left", self.show_previous_image), ("Right", self.show_next_image)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda action=action: self._navigation_shortcut(action))

    @staticmethod
    def _editing():
        return isinstance(QApplication.focusWidget(), (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox))

    def _navigation_shortcut(self, action):
        if not self._editing() and self.transfer_future is None:
            action()

    def select_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "Select image folder")
        if selected:
            self.open_folder(Path(selected))

    def open_folder(self, folder: Path):
        self.stop_playback()
        self._clear_prefetch()
        self.folder_path = folder
        self.hidden_svg_elements.clear()
        self.directory_label.setText(str(folder))
        self.load_images()
        self.anchor_key = None
        self.reset_view()
        self.show_image()

    def load_images(self):
        if self.folder_path is None:
            self.images = []
            return
        paths = [path for path in self.folder_path.iterdir() if path.is_file()]
        self.images = sorted(path.name for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS)
        self.overlay_paths = {path.stem.casefold(): path for path in paths if path.suffix.casefold() == ".svg"}
        self.current_index = 0
        self.classified_map.clear()

    @staticmethod
    def _open_image(path):
        with Image.open(path) as source:
            return source.copy()

    def _clear_prefetch(self):
        for future in self.prefetched_images.values():
            future.cancel()
        self.prefetched_images.clear()

    def _prefetch_next_image(self):
        wanted = None
        if self.folder_path is not None and self.current_index < len(self.images) - 1:
            wanted = self.folder_path / self.images[self.current_index + 1]
        for path in list(self.prefetched_images):
            if path != wanted:
                self.prefetched_images.pop(path).cancel()
        if wanted is not None and wanted not in self.prefetched_images:
            self.prefetched_images[wanted] = self.image_loader.submit(self._open_image, wanted)

    def show_image(self):
        if not self.images or self.folder_path is None:
            self._clear_image_display()
            return
        path = self.folder_path / self.images[self.current_index]
        try:
            future = self.prefetched_images.pop(path, None)
            self.image = future.result() if future else self._open_image(path)
        except (OSError, ValueError) as exc:
            self.stop_playback()
            QMessageBox.critical(self, "Image Error", f"Failed to open image:\n{path}\n\n{exc}")
            return
        self.rendered_image_cache = None
        self._load_overlay(path)
        self._update_image_details(path)
        self.update_canvas()
        self.filename_label.setText(path.name)
        classification = self.classified_map.get(path.name)
        self.status_label.setText(f"Classified as: {classification}" if classification else "Unclassified")
        self.status_label.setStyleSheet("color: #32cd32; font-weight: bold" if classification else "color: #ee5555")
        self.index_entry.setText(str(self.current_index + 1))
        self.total_label.setText(f"/ {len(self.images)}")
        self._prefetch_next_image()

    def _clear_image_display(self):
        self.image = self.image_bounds = self.photo = self.overlay_photo = None
        self.rendered_image_cache = None
        self.overlay_path = None
        self.overlay_cache.clear()
        self.anchors = []
        self.clear_pixel()
        self.canvas.update()
        self.filename_label.clear()
        self.status_label.clear()
        self.index_entry.setText("0")
        self.total_label.setText("/ 0")
        self.details_tree.clear()

    def _load_overlay(self, image_path):
        self.overlay_path = self.overlay_paths.get(image_path.stem.casefold())
        self.overlay_cache.clear()
        self.anchors = []
        if self.overlay_path is not None:
            try:
                self.anchors = svg_anchors(self.overlay_path)
            except (OSError, ValueError, ElementTree.ParseError):
                pass

    def _view_bounds(self):
        canvas_width, canvas_height = max(1, self.canvas.width()), max(1, self.canvas.height())
        fit_width, fit_height = fitted_size(self.image.size, (canvas_width, canvas_height))
        width = max(1, int(fit_width * self.zoom_factor))
        height = max(1, int(fit_height * self.zoom_factor))
        center_x = self.offset_x + canvas_width // 2
        center_y = self.offset_y + canvas_height // 2
        anchor = next((a for a in self.anchors if a.key == self.anchor_key), None)
        if anchor is not None:
            center_x += (0.5 - anchor.x) * width
            center_y += (0.5 - anchor.y) * height
        return round(center_x) - width // 2, round(center_y) - height // 2, width, height

    def update_canvas(self):
        if self.image is None:
            self.canvas.update()
            return
        self.image_bounds = self._view_bounds()
        left, top, width, height = self.image_bounds
        anchor = next((a for a in self.anchors if a.key == self.anchor_key), None)
        if anchor is not None:
            self.anchor_status.setText(f"Locked: {anchor.label} — drag to reposition")
        elif self.anchor_key is not None:
            self.anchor_status.setText("Anchor missing — playback stopped; showing centered image")
            self.stop_playback()
        else:
            self.anchor_status.setText("Anchor off")
        crop = (
            max(0, -left),
            max(0, -top),
            min(width, self.canvas.width() - left),
            min(height, self.canvas.height() - top),
        )
        x0, y0, x1, y1 = crop
        self.photo = self.overlay_photo = None
        if x1 > x0 and y1 > y0:
            cache_key = (width, height, *crop)
            if self.rendered_image_cache is None or self.rendered_image_cache[0] != cache_key:
                source_box = (
                    x0 * self.image.width / width,
                    y0 * self.image.height / height,
                    x1 * self.image.width / width,
                    y1 * self.image.height / height,
                )
                resized = self.image.resize((x1 - x0, y1 - y0), Image.Resampling.NEAREST, box=source_box)
                self.rendered_image_cache = (cache_key, qt_image(resized))
            self.photo = self.rendered_image_cache[1]
            self.photo_position = (left + x0, top + y0)
            if self.overlay_enabled.isChecked():
                self.overlay_photo = self._get_rendered_overlay((width, height), crop)
        self.inspect_pixel()
        self.canvas.update()

    def _get_rendered_overlay(self, size, crop):
        if self.overlay_path is None:
            return None
        key = (*size, *crop)
        if key in self.overlay_cache:
            self.overlay_cache.move_to_end(key)
            return self.overlay_cache[key]
        try:
            import resvg_py

            root = ElementTree.fromstring(
                svg_with_line_width(
                    self.overlay_path, self.overlay_line_width, size, self.overlay_text_size, self.hidden_svg_elements
                )
            )
            left, top, right, bottom = crop
            root.attrib.update(width=str(size[0]), height=str(size[1]), x=str(-left), y=str(-top))
            viewport = ElementTree.Element(
                "{http://www.w3.org/2000/svg}svg", {"width": str(right - left), "height": str(bottom - top)}
            )
            viewport.append(root)
            png = resvg_py.svg_to_bytes(
                svg_string=ElementTree.tostring(viewport, encoding="unicode"),
                width=right - left,
                height=bottom - top,
                resources_dir=os.fspath(self.overlay_path.parent),
            )
            result = QImage.fromData(png)
            if result.isNull():
                raise ValueError("SVG renderer produced an invalid image")
        except Exception as exc:  # noqa: BLE001 -- renderer backend exceptions vary.
            self.overlay_path = None
            self.overlay_cache.clear()
            QMessageBox.warning(self, "SVG Overlay Error", str(exc))
            return None
        self.overlay_cache[key] = result
        if len(self.overlay_cache) > 8:
            self.overlay_cache.popitem(last=False)
        return result

    def set_overlay_line_width(self, value):
        self.overlay_line_width = value
        self.overlay_cache.clear()
        self.update_canvas()

    def set_overlay_text_size(self, value):
        self.overlay_text_size = value
        self.overlay_cache.clear()
        self.update_canvas()

    def toggle_svg_overlay(self, checked):
        for widget in (self.overlay_line_width_spinbox, self.overlay_text_size_spinbox, self.elements_button):
            widget.setEnabled(checked)
        self.update_canvas()

    def zoom_image(self, event):
        if self.image is None:
            return
        old_bounds = self.image_bounds
        self.zoom_factor = min(20.0, max(0.1, self.zoom_factor * (1.1 if event.delta > 0 else 1 / 1.1)))
        if old_bounds is not None:
            old_left, old_top, old_width, old_height = old_bounds
            left, top, width, height = self._view_bounds()
            self.offset_x += event.x - (left + (event.x - old_left) * width / old_width)
            self.offset_y += event.y - (top + (event.y - old_top) * height / old_height)
        self.update_canvas()

    def start_pan(self, event):
        self.start_x, self.start_y = event.x, event.y

    def pan_image(self, event):
        self.offset_x += event.x - self.start_x
        self.offset_y += event.y - self.start_y
        self.start_x, self.start_y = event.x, event.y
        self.update_canvas()
        self.inspect_pixel(event)

    def clear_pixel(self):
        self.pixel_pointer = None
        self.pixel_status.setText("Hover over the image to inspect pixels")

    def inspect_pixel(self, event=None):
        if event is not None:
            self.pixel_pointer = (event.x, event.y)
        self.pixel_status.setText("Hover over the image to inspect pixels")
        if self.image is None or self.image_bounds is None or self.pixel_pointer is None:
            return
        left, top, width, height = self.image_bounds
        x, y = self.pixel_pointer
        if not (left <= x < left + width and top <= y < top + height):
            return
        px, py = int((x - left) * self.image.width / width), int((y - top) * self.image.height / height)
        value = self.image.getpixel((px, py))
        label = {"L": "Gray", "LA": "Gray+A", "1": "B/W", "P": "Index"}.get(self.image.mode, self.image.mode)
        self.pixel_status.setText(f"({px}, {py}) · {label}: {value}")

    def show_next_image(self):
        if self.transfer_future is not None:
            return
        self.stop_playback()
        if self.images and self.current_index < len(self.images) - 1:
            self.current_index += 1
            self.show_image()

    def show_previous_image(self):
        if self.transfer_future is not None:
            return
        self.stop_playback()
        if self.images and self.current_index > 0:
            self.current_index -= 1
            self.show_image()

    def reset_view(self):
        if self.anchor_key is None:
            self.zoom_factor = 1.0
            self.offset_x = self.offset_y = 0.0

    def unlock_anchor(self):
        self.anchor_key = self.anchor_preview = None
        self.reset_view()
        self.update_canvas()

    def fit_image_to_window(self):
        self.unlock_anchor()

    def lock_anchor(self, anchor):
        if self.image_bounds is not None:
            left, top, width, height = self.image_bounds
            self.offset_x = left + anchor.x * width - self.canvas.width() // 2
            self.offset_y = top + anchor.y * height - self.canvas.height() // 2
        else:
            self.offset_x = self.offset_y = 0
        self.anchor_key = anchor.key
        self.update_canvas()

    def select_anchor(self):
        self.stop_playback()
        dialog = QDialog(self)
        dialog.setWindowTitle("Select SVG Anchor")
        dialog.resize(530, 320)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Select a point or endpoint to highlight it in the image."))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Element / anchor", "Image X (%)", "Image Y (%)"])
        for index, anchor in enumerate(self.anchors):
            item = QTreeWidgetItem([anchor.label, f"{anchor.x * 100:.3f}", f"{anchor.y * 100:.3f}"])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            tree.addTopLevelItem(item)
        layout.addWidget(tree)
        if not self.anchors:
            layout.addWidget(QLabel("No supported, identified anchors inside this SVG viewBox."))

        def preview():
            selected = tree.currentItem()
            self.anchor_preview = self.anchors[selected.data(0, Qt.ItemDataRole.UserRole)] if selected else None
            self.update_canvas()

        tree.itemSelectionChanged.connect(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Lock anchor")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and self.anchor_preview is not None:
            self.lock_anchor(self.anchor_preview)
        self.anchor_preview = None
        self.update_canvas()
        dialog.deleteLater()

    def select_svg_elements(self):
        self.stop_playback()
        if self.overlay_path is None:
            QMessageBox.information(self, "SVG Elements", "The current image has no SVG overlay.")
            return
        try:
            viewport = _svg_viewport_size(ElementTree.parse(self.overlay_path).getroot())
            if viewport is None:
                raise ValueError("The SVG has no usable viewport.")
            scale = 1024 / max(viewport)
            size = (max(1, round(viewport[0] * scale)), max(1, round(viewport[1] * scale)))
            elements = visible_svg_graphics(self.overlay_path, size, self.overlay_line_width, self.overlay_text_size)
        except Exception as exc:  # noqa: BLE001 -- renderer backend exceptions vary.
            QMessageBox.warning(self, "SVG Elements", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("SVG element visibility")
        dialog.resize(650, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Select an element to highlight its location in the image."))
        tree = QTreeWidget()
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setHeaderLabels(["Graphic element", "Image X (%)", "Image Y (%)", "Visible"])
        for index, (key, label, bounds) in enumerate(elements):
            left, top, right, bottom = bounds
            item = QTreeWidgetItem(
                [
                    label,
                    f"{(left + right) * 50:.3f}",
                    f"{(top + bottom) * 50:.3f}",
                    "No" if key in self.hidden_svg_elements else "Yes",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            tree.addTopLevelItem(item)
        layout.addWidget(tree)

        def preview():
            self.element_previews = [
                elements[item.data(0, Qt.ItemDataRole.UserRole)][2] for item in tree.selectedItems()
            ]
            self.update_canvas()

        def change(visible, all_elements=False):
            items = (
                [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
                if all_elements
                else tree.selectedItems()
            )
            for item in items:
                key = elements[item.data(0, Qt.ItemDataRole.UserRole)][0]
                if visible:
                    self.hidden_svg_elements.discard(key)
                else:
                    self.hidden_svg_elements.add(key)
                item.setText(3, "Yes" if visible else "No")
            self.overlay_cache.clear()
            self.update_canvas()

        tree.itemSelectionChanged.connect(preview)
        buttons = QHBoxLayout()
        self._button(buttons, "Show selected", lambda: change(True))
        self._button(buttons, "Hide selected", lambda: change(False))
        self._button(buttons, "Show all", lambda: change(True, True))
        self._button(buttons, "Close", dialog.accept)
        layout.addLayout(buttons)
        dialog.exec()
        self.element_previews = []
        self.update_canvas()
        dialog.deleteLater()

    def define_classes(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Define Classifications")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Enter class names (up to {MAX_CLASSES}):"))
        entries = []
        for index in range(MAX_CLASSES):
            entry = QLineEdit(self.classifications[index] if index < len(self.classifications) else "")
            layout.addWidget(entry)
            entries.append(entry)

        def save():
            names = [entry.text().strip() for entry in entries if entry.text().strip()]
            if len(set(names)) != len(names):
                QMessageBox.warning(dialog, "Duplicate Classes", "Each classification name must be unique.")
                return
            self.classifications = names
            self.create_classification_buttons()
            dialog.accept()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setDefault(True)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        entries[0].setFocus()
        dialog.exec()
        dialog.deleteLater()

    def create_classification_buttons(self):
        while self.classification_layout.count():
            widget = self.classification_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        for shortcut in self.class_shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.class_shortcuts.clear()
        self.classification_layout.addStretch()
        for index, name in enumerate(self.classifications, 1):
            self._button(
                self.classification_layout,
                f"{index}: {name}",
                lambda checked=False, name=name: self.classify_image(name),
            )
            if index <= 9:
                shortcut = QShortcut(QKeySequence(str(index)), self)
                shortcut.activated.connect(lambda name=name: self._classification_shortcut(name))
                self.class_shortcuts.append(shortcut)
        self.btn_remove_classification = self._button(
            self.classification_layout, "Remove Classification", self.remove_classification
        )
        self.classification_layout.addStretch()

    def _classification_shortcut(self, class_name):
        if not self._editing():
            self.classify_image(class_name)

    def classify_image(self, class_name):
        if not self.images or self.transfer_future is not None:
            return
        self.stop_playback()
        self.classified_map[self.images[self.current_index]] = class_name
        if self.current_index < len(self.images) - 1:
            self.current_index += 1
        self.reset_view()
        self.show_image()

    def remove_classification(self):
        if self.images and self.transfer_future is None:
            self.classified_map.pop(self.images[self.current_index], None)
            self.show_image()

    def _update_transfer_button(self):
        self.transfer_button.setText(f"{self.transfer_mode_selector.currentText()} Classified")

    def toggle_image_details(self):
        self.details_visible = not self.details_visible
        self.details_frame.setVisible(self.details_visible)
        if self.details_visible:
            total_width = self.content_pane.width()
            self.content_pane.setSizes([total_width * 2 // 3, total_width // 3])
        self.details_button.setText("Hide Details" if self.details_visible else "Show Details")
        if self.details_visible and self.folder_path is not None and self.images:
            self._update_image_details(self.folder_path / self.images[self.current_index])
        QTimer.singleShot(0, self.update_canvas)

    def _update_image_details(self, path):
        if not self.details_visible:
            return
        self.details_tree.clear()
        try:
            sections = image_metadata(path, self.overlay_path)
        except (OSError, ValueError) as exc:
            self.details_tree.addTopLevelItem(QTreeWidgetItem(["Metadata unavailable", str(exc)]))
            return
        for section, details in sections:
            parent = QTreeWidgetItem([section])
            self.details_tree.addTopLevelItem(parent)
            for name, value in details:
                parent.addChild(QTreeWidgetItem([name, str(value)]))
            parent.setExpanded(True)

    def copy_selected_detail(self):
        item = self.details_tree.currentItem()
        if item and item.text(1):
            QApplication.clipboard().setText(item.text(1))

    def move_classified_images(self):
        if self.transfer_future is not None:
            return
        if not self.classified_map or self.folder_path is None:
            QMessageBox.information(self, "No Classifications", "No images have been classified yet.")
            return
        operation = self.transfer_mode_selector.currentText()
        if (
            QMessageBox.question(
                self,
                f"{operation} Images",
                f"{operation} {len(self.classified_map)} classified image(s) into class subfolders?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.stop_playback()
        pending = list(self.prefetched_images.values())
        self._clear_prefetch()
        self.transfer_folder = self.folder_path
        self.transfer_operation = operation
        self.transfer_progress.setRange(0, len(self.classified_map))
        self.transfer_progress.setValue(0)
        self.transfer_progress.show()
        for widget in (self.transfer_button, self.transfer_mode_selector, self.select_folder_button, self.canvas):
            widget.setEnabled(False)
        if operation == "Move":
            self._clear_image_display()
        items = list(self.classified_map.items())
        folder = self.transfer_folder

        def transfer():
            # Wait for in-flight reads off the UI thread before moving source files.
            for future in pending:
                if not future.cancelled():
                    try:
                        future.result()
                    except (OSError, ValueError):
                        pass
            return transfer_classified_files(
                folder, items, copying=operation == "Copy", progress_callback=self.transfer_updates.put
            )

        self.transfer_future = self.transfer_executor.submit(transfer)
        self.transfer_timer.start()

    def _poll_transfer(self):
        future = self.transfer_future
        if future is None:
            return
        try:
            while True:
                self.transfer_progress.setValue(self.transfer_updates.get_nowait())
        except Empty:
            pass
        if not future.done():
            return
        try:
            completed, failures = future.result()
        except Exception as exc:  # noqa: BLE001 -- restore UI after worker failures.
            completed, failures = 0, [str(exc)]
        self.transfer_timer.stop()
        self.transfer_future = None
        self.transfer_progress.hide()
        for widget in (self.transfer_button, self.transfer_mode_selector, self.select_folder_button, self.canvas):
            widget.setEnabled(True)
        if self.transfer_operation == "Move" and self.folder_path == self.transfer_folder:
            self.load_images()
            self.show_image()
        action = "copied" if self.transfer_operation == "Copy" else "moved"
        message = f"{completed} image(s) {action}."
        if failures:
            message += f"\n\nNot {action}:\n" + "\n".join(failures[:10])
            if len(failures) > 10:
                message += f"\n...and {len(failures) - 10} more."
        (QMessageBox.warning if failures else QMessageBox.information)(
            self, f"{self.transfer_operation} Complete", message
        )
        self.transfer_folder = None

    def jump_to_index(self):
        if self.transfer_future is not None:
            return
        try:
            index = int(self.index_entry.text()) - 1
            if not 0 <= index < len(self.images):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Invalid Index", f"Please enter a number between 1 and {len(self.images)}.")
            self.index_entry.setText(str(self.current_index + 1 if self.images else 0))
            return
        self.stop_playback()
        self.current_index = index
        self.reset_view()
        self.show_image()

    def toggle_play(self):
        if self.transfer_future is not None:
            return
        if self.is_playing:
            self.stop_playback()
        elif self.images:
            self.play_delay_ms = self.delay_entry.value()
            self.is_playing = True
            self.play_button.setText("Stop")
            self.play_images()

    def play_images(self):
        if not self.is_playing:
            return
        if self.current_index < len(self.images) - 1:
            self.current_index += 1
            self.show_image()
            if self.is_playing:
                self.play_timer.start(self.play_delay_ms)
        else:
            self.stop_playback()

    def stop_playback(self):
        self.is_playing = False
        self.play_timer.stop()
        self.play_button.setText("Play")

    def closeEvent(self, event):
        self.stop_playback()
        self.transfer_timer.stop()
        self._clear_prefetch()
        self.image_loader.shutdown(wait=False, cancel_futures=True)
        self.transfer_executor.shutdown(wait=False)
        event.accept()
