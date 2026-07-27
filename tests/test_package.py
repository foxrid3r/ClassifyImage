from pathlib import Path

from classify_image import __version__
from classify_image.app import MAX_CLASSES, SUPPORTED_EXTENSIONS, fitted_size, matching_svg_path


def test_version() -> None:
    assert __version__ == "0.1.0"


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
