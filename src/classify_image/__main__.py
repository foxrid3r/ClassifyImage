import tkinter as tk

import sv_ttk

from .app import ImageClassifierApp


def main() -> None:
    root = tk.Tk()
    sv_ttk.use_dark_theme()
    ImageClassifierApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
