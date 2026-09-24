from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import resvg_py
from PIL import Image

from classify_image import __version__
from classify_image.app import (
    MAX_CLASSES,
    SUPPORTED_EXTENSIONS,
    fitted_size,
    image_metadata,
    matching_svg_path,
    svg_with_line_width,
    transfer_classified_files,
)


def test_version() -> None:
    assert __version__ == "0.3.5"


def test_supported_extensions() -> None:
    assert ".png" in SUPPORTED_EXTENSIONS
    assert MAX_CLASSES == 10


def test_fitted_size_uses_largest_size_inside_viewport() -> None:
    assert fitted_size((640, 480), (1000, 500)) == (666, 500)
    assert fitted_size((640, 480), (500, 1000)) == (500, 375)


def test_matching_svg_path_is_case_insensitive(tmp_path: Path) -> None:
    image_path = tmp_path / "example.PNG"
    image_path.touch()
    overlay_path = tmp_path / "example.SVG"
    overlay_path.touch()

    assert matching_svg_path(image_path) == overlay_path


def test_matching_svg_path_returns_none_when_absent(tmp_path: Path) -> None:
    assert matching_svg_path(tmp_path / "example.png") is None


def test_transfer_classified_files_reports_progress_and_copies_overlay(tmp_path: Path) -> None:
    (tmp_path / "example.png").write_bytes(b"image")
    (tmp_path / "example.svg").write_text("<svg/>", encoding="utf-8")
    progress: list[int] = []

    completed, failures = transfer_classified_files(
        tmp_path, [("example.png", "accepted")], copying=True, progress_callback=progress.append
    )

    assert completed == 1
    assert failures == []
    assert progress == [1]
    assert (tmp_path / "example.png").exists()
    assert (tmp_path / "accepted" / "example.png").read_bytes() == b"image"
    assert (tmp_path / "accepted" / "example.svg").read_text(encoding="utf-8") == "<svg/>"


def test_transfer_classified_files_moves_source_and_reports_existing_destination(tmp_path: Path) -> None:
    (tmp_path / "first.png").write_bytes(b"first")
    (tmp_path / "second.png").write_bytes(b"second")
    destination = tmp_path / "accepted"
    destination.mkdir()
    (destination / "second.png").write_bytes(b"existing")
    progress: list[int] = []

    completed, failures = transfer_classified_files(
        tmp_path,
        [("first.png", "accepted"), ("second.png", "accepted")],
        copying=False,
        progress_callback=progress.append,
    )

    assert completed == 1
    assert failures == ["second.png: destination already exists"]
    assert progress == [1, 2]
    assert not (tmp_path / "first.png").exists()
    assert (destination / "first.png").read_bytes() == b"first"
    assert (tmp_path / "second.png").exists()


def test_svg_with_line_width_overrides_all_vector_geometry(tmp_path: Path) -> None:
    svg_path = tmp_path / "example.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><defs><marker id="arrow" markerUnits="userSpaceOnUse"/>'
        '</defs><path d="M0 0L1 1"/><line x2="1" y2="1" marker-end="url(#arrow)"/></svg>',
        encoding="utf-8",
    )

    result_bytes = svg_with_line_width(svg_path, 2.5)
    result = result_bytes.decode("utf-8")

    assert "path, line, polyline, polygon, rect, circle, ellipse" in result
    assert "stroke-width: 2.5 !important" in result
    root = ElementTree.fromstring(result_bytes)
    marker = next(element for element in root.iter() if element.tag.endswith("marker"))
    assert marker.get("markerUnits") == "strokeWidth"


def test_svg_line_width_is_converted_from_screen_pixels(tmp_path: Path) -> None:
    svg_path = tmp_path / "example.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><path d="M0 0L1 1"/></svg>',
        encoding="utf-8",
    )

    result = svg_with_line_width(svg_path, 4, (200, 100)).decode("utf-8")

    assert "stroke-width: 2 !important" in result


def test_svg_text_size_is_converted_from_screen_pixels(tmp_path: Path) -> None:
    svg_path = tmp_path / "example.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><text x="1" y="10">Label</text></svg>',
        encoding="utf-8",
    )

    result = svg_with_line_width(svg_path, 1, (200, 100), text_size=12)
    root = ElementTree.fromstring(result)
    text_element = next(element for element in root.iter() if element.tag.endswith("text"))

    assert text_element.get("font-size") == "6px"


def test_resvg_renders_svg_to_png_at_matching_target_aspect_ratio(tmp_path: Path) -> None:
    svg_path = tmp_path / "example.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="red"/></svg>',
        encoding="utf-8",
    )

    png_bytes = resvg_py.svg_to_bytes(
        svg_string=svg_with_line_width(svg_path, 2, (20, 20)).decode("utf-8"),
        width=20,
        height=20,
        resources_dir=str(tmp_path),
    )

    with Image.open(BytesIO(png_bytes)) as rendered:
        assert rendered.format == "PNG"
        assert rendered.size == (20, 20)


def test_resvg_renders_inherited_svg_text(tmp_path: Path) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="2840" height="2840" viewBox="0 0 2840 2840" '
        'font-family="arial"><style>.label { font-size:64pt; font-family:Arial; }</style>'
        '<g class="label"><text x="1420" y="1420" fill="#32CD32">Empty</text></g></svg>'
    )
    svg_path = tmp_path / "text.svg"
    svg_path.write_text(svg, encoding="utf-8")

    normalized_svg = svg_with_line_width(svg_path, 2, (710, 710))
    normalized_root = ElementTree.fromstring(normalized_svg)
    text_element = next(element for element in normalized_root.iter() if element.tag.endswith("text"))
    assert text_element.get("font-size") == "85.3333px"
    assert text_element.get("font-family") == "Arial"

    png_bytes = resvg_py.svg_to_bytes(
        svg_string=normalized_svg.decode("utf-8"),
        width=710,
        height=710,
    )

    with Image.open(BytesIO(png_bytes)) as rendered:
        assert rendered.getbbox() is not None


def test_image_metadata_includes_file_image_and_exif_details(tmp_path: Path) -> None:
    image_path = tmp_path / "example.jpg"
    overlay_path = tmp_path / "example.svg"
    overlay_path.touch()
    exif = Image.Exif()
    exif[271] = "Example Camera"
    Image.new("RGB", (40, 20), "red").save(image_path, exif=exif, dpi=(300, 300))

    sections = dict(image_metadata(image_path, overlay_path))
    file_details = dict(sections["File"])
    image_details = dict(sections["Image"])
    exif_details = dict(sections["EXIF"])

    assert file_details["Name"] == "example.jpg"
    assert file_details["SVG overlay"] == "example.svg"
    assert image_details["Format"] == "JPEG"
    assert image_details["Dimensions"] == "40 × 20 pixels"
    assert exif_details["Make"] == "Example Camera"
