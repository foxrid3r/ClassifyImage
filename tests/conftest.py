import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from classify_image.app import ImageClassifierApp


@pytest.fixture(scope="session")
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(qt_application):
    window = ImageClassifierApp()
    yield window
    window.close()
    window.deleteLater()
    qt_application.processEvents()
