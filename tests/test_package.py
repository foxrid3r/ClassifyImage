from classify_image import __version__
from classify_image.app import MAX_CLASSES, SUPPORTED_EXTENSIONS


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_supported_extensions() -> None:
    assert ".png" in SUPPORTED_EXTENSIONS
    assert MAX_CLASSES == 10
