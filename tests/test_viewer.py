from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
import resvg_py
from PIL import Image

from classify_image.app import ImageClassifierApp, svg_graphics, svg_with_line_width, visible_svg_graphics


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


def test_lock_anchor_keeps_current_position():
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


def test_visibility_list_uses_anchor_names_and_only_rendered_viewbox_content(tmp_path):
    path = tmp_path / "overlay.svg"
    path.write_text("""<svg xmlns="http://www.w3.org/2000/svg" viewBox="100 200 100 100">
      <defs><circle id="definition" r="4" fill="blue"/></defs>
      <g data-c="F218" transform="translate(100 200)"><circle cx="30" cy="40" r="5"/></g>
      <rect id="outside" x="250" y="250" width="10" height="10"/>
      <line id="crossing" x1="50" y1="260" x2="250" y2="260" stroke="red"/>
      <use id="reference" href="#definition" x="180" y="280"/>
      <rect id="hidden-in-source" x="120" y="220" width="10" height="10" display="none"/>
    </svg>""")
    rows = visible_svg_graphics(path, (100, 100))
    assert [label for _, label, _ in rows] == ["F218 — circle", "crossing — line", "reference — use"]
    assert rows[0][2] == pytest.approx((0.25, 0.35, 0.35, 0.45))
    # Anonymous-element keys still target the source element after filtering.
    rendered = svg_with_line_width(path, 1, hidden_elements={rows[0][0]})
    with Image.open(BytesIO(resvg_py.svg_to_bytes(svg_string=rendered.decode(), width=100, height=100))) as image:
        assert image.getpixel((30, 40))[3] == 0


def test_visibility_list_clips_curves_and_text_and_keeps_hidden_choices_available(tmp_path):
    path = tmp_path / "overlay.svg"
    path.write_text("""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs><clipPath id="clip"><rect width="50" height="50"/></clipPath></defs>
      <g data-c="curve" clip-path="url(#clip)"><path d="M10 10 Q30 70 70 10" stroke="blue" fill="none"/></g>
      <text id="outside-label" x="150" y="50">Outside</text>
      <rect id="clipped-out" x="60" y="60" width="10" height="10" clip-path="url(#clip)"/>
    </svg>""")
    rows = visible_svg_graphics(path, (100, 100))
    assert [label for _, label, _ in rows] == ["curve — path"]
    assert rows[0][2][2] <= 0.5
    # Toggling visibility renders a temporary copy; the picker can still offer the row.
    svg_with_line_width(path, 1, hidden_elements={rows[0][0]})
    assert visible_svg_graphics(path, (100, 100)) == rows


def test_visibility_list_keeps_use_references_to_graphics_outside_defs(tmp_path):
    path = tmp_path / "overlay.svg"
    path.write_text("""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect id="source" x="150" y="20" width="10" height="10"/>
      <use id="inside" href="#source" x="-130"/>
    </svg>""")
    rows = visible_svg_graphics(path, (100, 100))
    assert [label for _, label, _ in rows] == ["inside — use"]
    assert rows[0][2] == (0.2, 0.2, 0.3, 0.3)


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
