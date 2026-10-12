from pathlib import Path

from PySide6.QtGui import QIcon


def application_icon() -> QIcon:
    return QIcon(str(Path(__file__).with_name("assets") / "classify-image.svg"))
