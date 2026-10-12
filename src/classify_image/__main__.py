import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .app import ImageClassifierApp
from .icons import application_icon


def main() -> None:
    application = QApplication(sys.argv)
    application.setApplicationName("ClassifyImage")
    application.setWindowIcon(application_icon())
    application.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#252525"),
        (QPalette.ColorRole.WindowText, "#eeeeee"),
        (QPalette.ColorRole.Base, "#181818"),
        (QPalette.ColorRole.AlternateBase, "#303030"),
        (QPalette.ColorRole.Text, "#eeeeee"),
        (QPalette.ColorRole.Button, "#353535"),
        (QPalette.ColorRole.ButtonText, "#eeeeee"),
        (QPalette.ColorRole.Highlight, "#287bcc"),
        (QPalette.ColorRole.HighlightedText, "white"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#888888"))
    application.setPalette(palette)
    window = ImageClassifierApp()
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
