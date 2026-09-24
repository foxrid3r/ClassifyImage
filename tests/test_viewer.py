from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
import resvg_py
from PIL import Image

from classify_image.app import ImageClassifierApp, svg_graphics, svg_with_line_width


def test_hidden_graphics_render_without_changing_source_or_definitions(tmp_path):
    source = """<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
      <defs><rect id="template" width="10" height="10" fill="blue"/></defs>
      <rect id="hide" width="10" height="10" fill="red"/>
      <use id="keep" href="#template" x="10"/>
    </svg>"""
    path = tmp_path / "overlay.svg"
    path.write_text(source)
    assert [key for key, _, _ in svg_graphics(ElementTree.fromstring(source))] == ["id:hide", "id:keep"]
    data = svg_with_line_width(path, 1, hidden_elements={"id:hide"})
    with Image.open(BytesIO(resvg_py.svg_to_bytes(svg_string=data.decode()))) as rendered:
        assert rendered.getpixel((5, 5))[3] == 0
        assert rendered.getpixel((15, 5)) == (0, 0, 255, 255)
    assert path.read_text() == source


@pytest.mark.parametrize("mode, value", [("RGB", (12, 34, 56)), ("L", 123), ("I;16", 12345), ("P", 7)])
def test_pixel_values_use_original_image_and_transformed_bounds(mode, value):
    app = ImageClassifierApp.__new__(ImageClassifierApp)
    app.image = Image.new(mode, (4, 2))
    app.image.putpixel((2, 1), value)
    app.image_bounds = (-10, 20, 40, 20)
    app.pixel_status = Mock()
    app.inspect_pixel(SimpleNamespace(x=15, y=35))
    assert f"Pixel (2, 1) · {mode}" in app.pixel_status.set.call_args.args[0]
    assert app.pixel_status.set.call_args.args[0].endswith(str(value))
    app.inspect_pixel(SimpleNamespace(x=30, y=35))
    assert app.pixel_status.set.call_args.args[0] == "Hover over the image to inspect pixels"
    app.clear_pixel()
    assert app.pixel_pointer is None


def test_lock_anchor_defaults_to_current_position_and_can_center():
    app = ImageClassifierApp.__new__(ImageClassifierApp)
    app.canvas = Mock()
    app.canvas.winfo_width.return_value = 800
    app.canvas.winfo_height.return_value = 600
    app.image_bounds = (-100, 50, 1200, 800)
    anchor = SimpleNamespace(key="point", x=0.25, y=0.75)
    app.lock_anchor(anchor)
    assert app.anchor_key == "point"
    assert app.offset_x + 400 == 200
    assert app.offset_y + 300 == 650
    app.lock_anchor(anchor, stay_in_place=False)
    assert (app.offset_x, app.offset_y) == (0, 0)


def test_playback_preserves_user_view_without_anchor():
    app = ImageClassifierApp.__new__(ImageClassifierApp)
    app.root = Mock()
    app.is_playing = True
    app.images = ["first.png", "second.png"]
    app.current_index = 0
    app.anchor_key = None
    app.zoom_factor = 3.5
    app.offset_x, app.offset_y = 100, -50
    app.play_delay_ms = 125
    app.show_image = Mock()
    app.play_images()
    assert app.current_index == 1
    assert (app.zoom_factor, app.offset_x, app.offset_y) == (3.5, 100, -50)
    app.show_image.assert_called_once()
    app.root.after.assert_called_once_with(125, app.play_images)


def test_pan_updates_pixel_mapping():
    app = ImageClassifierApp.__new__(ImageClassifierApp)
    app.image = Image.new("L", (10, 10), 42)
    app.image_bounds = (0, 0, 100, 100)
    app.start_x = app.start_y = app.offset_x = app.offset_y = 0
    app.canvas = Mock()
    app.pixel_status = Mock()
    app.pan_image(SimpleNamespace(x=25, y=30))
    assert app.image_bounds == (25, 30, 100, 100)
    assert app.pixel_status.set.call_args.args[0] == "Pixel (0, 0) · L [L]: 42"
