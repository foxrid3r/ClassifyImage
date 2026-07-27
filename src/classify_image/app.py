from __future__ import annotations

import os
import shutil
import tkinter as tk
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
MAX_CLASSES = 10
MIN_PLAY_DELAY_MS = 10


def fitted_size(image_size: tuple[int, int], viewport_size: tuple[int, int]) -> tuple[int, int]:
    """Return the largest aspect-ratio-preserving size contained by a viewport."""
    image_width, image_height = image_size
    viewport_width, viewport_height = viewport_size
    if viewport_width * image_height <= viewport_height * image_width:
        return viewport_width, max(1, image_height * viewport_width // image_width)
    return max(1, image_width * viewport_height // image_height), viewport_height


def matching_svg_path(image_path: Path) -> Path | None:
    """Find a same-directory SVG whose stem matches the image stem."""
    expected_name = f"{image_path.stem}.svg".casefold()
    try:
        return next(
            (
                candidate
                for candidate in image_path.parent.iterdir()
                if candidate.is_file() and candidate.name.casefold() == expected_name
            ),
            None,
        )
    except OSError:
        return None


class ImageClassifierApp:
    """Tkinter GUI for interactively sorting images into class folders."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ClassifyImage")
        self.root.geometry("1100x750")
        self.root.minsize(700, 500)

        self.folder_path: Path | None = None
        self.images: list[str] = []
        self.current_index = 0
        self.classifications: list[str] = []
        self.classified_map: dict[str, str] = {}

        self.zoom_factor = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.start_x = 0
        self.start_y = 0

        self.image: Image.Image | None = None
        self.overlay: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.overlay_enabled = tk.BooleanVar(value=True)
        self.is_playing = False
        self.play_delay_ms = 1000
        self.play_after_id: str | None = None

        self._create_widgets()

    def _create_widgets(self) -> None:
        file_manage_frame = ttk.Frame(self.root)
        file_manage_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        ttk.Button(file_manage_frame, text="Select Folder", command=self.select_folder).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(file_manage_frame, text="Define Classes", command=self.define_classes).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            file_manage_frame,
            text="Move Classified Images",
            command=self.move_classified_images,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(
            file_manage_frame,
            text="Show SVG Overlay",
            variable=self.overlay_enabled,
            command=self.update_canvas,
        ).pack(side=tk.LEFT, padx=(15, 5))

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.canvas.bind("<MouseWheel>", self.zoom_image)
        self.canvas.bind("<Button-4>", self.zoom_image)
        self.canvas.bind("<Button-5>", self.zoom_image)
        self.canvas.bind("<ButtonPress-1>", self.start_pan)
        self.canvas.bind("<B1-Motion>", self.pan_image)
        self.canvas.bind("<Configure>", lambda _event: self.update_canvas())

        self.classification_frame = ttk.Frame(self.root)
        self.classification_frame.pack(padx=10, pady=5)

        self.btn_remove_classification = ttk.Button(
            self.classification_frame,
            text="Remove Classification",
            command=self.remove_classification,
        )

        file_and_class_frame = ttk.Frame(self.root)
        file_and_class_frame.pack(fill=tk.X, padx=10, pady=5)

        self.filename_label = ttk.Label(file_and_class_frame, text="", font=("Segoe UI", 11))
        self.filename_label.pack(side=tk.LEFT, padx=(0, 10))

        self.status_label = ttk.Label(file_and_class_frame, text="", font=("Segoe UI", 11))
        self.status_label.pack(side=tk.LEFT)

        controls_frame = ttk.Frame(self.root)
        controls_frame.pack(padx=10, pady=(5, 10))

        ttk.Label(controls_frame, text="Delay (ms)").pack(side=tk.LEFT)
        self.delay_entry = ttk.Entry(controls_frame, width=7)
        self.delay_entry.insert(0, str(self.play_delay_ms))
        self.delay_entry.pack(side=tk.LEFT, padx=5)

        self.play_button = ttk.Button(controls_frame, text="Play", command=self.toggle_play)
        self.play_button.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Button(controls_frame, text="◀ Previous", command=self.show_previous_image).pack(side=tk.LEFT, padx=5)
        ttk.Button(controls_frame, text="Next ▶", command=self.show_next_image).pack(side=tk.LEFT, padx=5)

        self.index_var = tk.StringVar(value="0")
        self.index_entry = ttk.Entry(controls_frame, width=5, textvariable=self.index_var)
        self.index_entry.pack(side=tk.LEFT, padx=(20, 5))
        self.index_entry.bind("<Return>", self.jump_to_index)

        self.total_label = ttk.Label(controls_frame, text="/ 0")
        self.total_label.pack(side=tk.LEFT)

        self.root.bind("<Left>", lambda _event: self.show_previous_image())
        self.root.bind("<Right>", lambda _event: self.show_next_image())
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def select_folder(self) -> None:
        folder_selected = filedialog.askdirectory()
        if not folder_selected:
            return

        self.stop_playback()
        self.folder_path = Path(folder_selected)
        self.load_images()
        self.reset_view()
        self.show_image()

    def load_images(self) -> None:
        if self.folder_path is None:
            self.images = []
            return

        self.images = sorted(
            path.name
            for path in self.folder_path.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        self.current_index = 0
        self.classified_map.clear()

    def show_image(self) -> None:
        if not self.images or self.folder_path is None:
            self._clear_image_display()
            return

        image_path = self.folder_path / self.images[self.current_index]
        try:
            with Image.open(image_path) as source_image:
                self.image = source_image.copy()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Image Error", f"Failed to open image:\n{image_path}\n\n{exc}")
            return

        self._load_overlay(image_path)
        self.update_canvas()
        self.filename_label.config(text=image_path.name)

        current_file = self.images[self.current_index]
        if current_file in self.classified_map:
            classification = self.classified_map[current_file]
            self.status_label.config(
                text=f"Classified as: {classification}",
                foreground="green",
                font=("Segoe UI", 11, "bold"),
            )
        else:
            self.status_label.config(
                text="Unclassified",
                foreground="red",
                font=("Segoe UI", 11, "bold"),
            )

        self.index_var.set(str(self.current_index + 1))
        self.total_label.config(text=f"/ {len(self.images)}")

    def _clear_image_display(self) -> None:
        self.image = None
        self.overlay = None
        self.photo = None
        self.canvas.delete("all")
        self.filename_label.config(text="")
        self.status_label.config(text="")
        self.index_var.set("0")
        self.total_label.config(text="/ 0")

    def _load_overlay(self, image_path: Path) -> None:
        self.overlay = None
        overlay_path = matching_svg_path(image_path)
        if overlay_path is None:
            return

        assert self.image is not None
        try:
            import cairosvg

            svg_png = cairosvg.svg2png(
                url=os.fspath(overlay_path),
                output_width=self.image.width,
                output_height=self.image.height,
            )
            with Image.open(BytesIO(svg_png)) as overlay_image:
                self.overlay = overlay_image.convert("RGBA").copy()
        except Exception as exc:
            messagebox.showwarning(
                "SVG Overlay Error",
                f"Failed to load overlay:\n{overlay_path}\n\n{exc}",
            )

    def update_canvas(self) -> None:
        if self.image is None:
            return

        canvas_width = max(self.canvas.winfo_width(), 1)
        canvas_height = max(self.canvas.winfo_height(), 1)
        fit_width, fit_height = fitted_size(self.image.size, (canvas_width, canvas_height))
        new_width = max(1, int(fit_width * self.zoom_factor))
        new_height = max(1, int(fit_height * self.zoom_factor))

        # Nearest-neighbor scaling keeps source pixels as hard-edged blocks.
        resized_image = self.image.resize((new_width, new_height), Image.Resampling.NEAREST)
        if self.overlay is not None and self.overlay_enabled.get():
            resized_overlay = self.overlay.resize((new_width, new_height), Image.Resampling.NEAREST)
            resized_image = resized_image.convert("RGBA")
            resized_image.alpha_composite(resized_overlay)
        self.photo = ImageTk.PhotoImage(resized_image)

        center_x = self.offset_x + canvas_width // 2
        center_y = self.offset_y + canvas_height // 2

        self.canvas.delete("all")
        self.canvas.create_image(center_x, center_y, image=self.photo, anchor=tk.CENTER)

        if self.images[self.current_index] in self.classified_map:
            self.canvas.create_rectangle(
                center_x - new_width // 2,
                center_y - new_height // 2,
                center_x + new_width // 2,
                center_y + new_height // 2,
                outline="green",
                width=3,
            )

    def zoom_image(self, event: tk.Event) -> None:
        zoom_in = getattr(event, "delta", 0) > 0 or getattr(event, "num", None) == 4
        self.zoom_factor *= 1.1 if zoom_in else 1 / 1.1
        self.zoom_factor = min(max(self.zoom_factor, 0.1), 20.0)
        self.update_canvas()

    def start_pan(self, event: tk.Event) -> None:
        self.start_x = event.x
        self.start_y = event.y

    def pan_image(self, event: tk.Event) -> None:
        self.offset_x += event.x - self.start_x
        self.offset_y += event.y - self.start_y
        self.start_x = event.x
        self.start_y = event.y
        self.update_canvas()

    def show_next_image(self) -> None:
        self.stop_playback()
        if self.images and self.current_index < len(self.images) - 1:
            self.current_index += 1
            self.reset_view()
            self.show_image()

    def show_previous_image(self) -> None:
        self.stop_playback()
        if self.images and self.current_index > 0:
            self.current_index -= 1
            self.reset_view()
            self.show_image()

    def reset_view(self) -> None:
        self.zoom_factor = 1.0
        self.offset_x = 0
        self.offset_y = 0

    def define_classes(self) -> None:
        class_window = tk.Toplevel(self.root)
        class_window.title("Define Classifications")
        class_window.transient(self.root)
        class_window.grab_set()
        class_window.resizable(False, False)

        entries: list[ttk.Entry] = []
        ttk.Label(class_window, text=f"Enter class names (up to {MAX_CLASSES}):").pack(pady=(10, 5))

        for index in range(MAX_CLASSES):
            entry = ttk.Entry(class_window, width=30)
            entry.pack(padx=10, pady=2)
            if index < len(self.classifications):
                entry.insert(0, self.classifications[index])
            entries.append(entry)

        def save_classes() -> None:
            class_names = [entry.get().strip() for entry in entries if entry.get().strip()]
            if not class_names:
                messagebox.showwarning("No Classes", "Please enter at least one classification.", parent=class_window)
                return
            if len(set(class_names)) != len(class_names):
                messagebox.showwarning("Duplicate Classes", "Each classification name must be unique.", parent=class_window)
                return

            self.classifications = class_names
            class_window.destroy()
            self.create_classification_buttons()

        button_frame = ttk.Frame(class_window)
        button_frame.pack(pady=10)
        ttk.Button(button_frame, text="Save", command=save_classes).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=class_window.destroy).pack(side=tk.LEFT, padx=5)

        class_window.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - class_window.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - class_window.winfo_height()) // 2
        class_window.geometry(f"+{x}+{y}")
        entries[0].focus_set()

    def create_classification_buttons(self) -> None:
        for widget in self.classification_frame.winfo_children():
            widget.destroy()

        for index, class_name in enumerate(self.classifications, start=1):
            button = ttk.Button(
                self.classification_frame,
                text=f"{index}: {class_name}",
                command=lambda name=class_name: self.classify_image(name),
            )
            button.pack(side=tk.LEFT, padx=5, pady=5)
            if index <= 9:
                self.root.bind(str(index), lambda _event, name=class_name: self.classify_image(name))

        self.btn_remove_classification = ttk.Button(
            self.classification_frame,
            text="Remove Classification",
            command=self.remove_classification,
        )
        self.btn_remove_classification.pack(side=tk.LEFT, padx=20)

    def classify_image(self, class_name: str) -> None:
        if not self.images:
            return

        self.stop_playback()
        self.classified_map[self.images[self.current_index]] = class_name

        if self.current_index < len(self.images) - 1:
            self.current_index += 1
        self.reset_view()
        self.show_image()

    def remove_classification(self) -> None:
        if not self.images:
            return
        self.classified_map.pop(self.images[self.current_index], None)
        self.show_image()

    def move_classified_images(self) -> None:
        if not self.classified_map or self.folder_path is None:
            messagebox.showinfo("No Classifications", "No images have been classified yet.")
            return

        if not messagebox.askyesno(
            "Move Images",
            f"Move {len(self.classified_map)} classified image(s) into class subfolders?",
        ):
            return

        self.stop_playback()
        self._clear_image_display()

        moved_count = 0
        failures: list[str] = []
        for filename, classification in list(self.classified_map.items()):
            source = self.folder_path / filename
            destination_dir = self.folder_path / classification
            destination = destination_dir / filename
            overlay_source = matching_svg_path(source)
            overlay_destination = destination_dir / overlay_source.name if overlay_source is not None else None

            try:
                destination_dir.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    failures.append(f"{filename}: destination already exists")
                    continue
                if overlay_destination is not None and overlay_destination.exists():
                    failures.append(f"{filename}: SVG overlay destination already exists")
                    continue
                shutil.move(os.fspath(source), os.fspath(destination))
                if overlay_source is not None and overlay_destination is not None:
                    shutil.move(os.fspath(overlay_source), os.fspath(overlay_destination))
                moved_count += 1
            except OSError as exc:
                failures.append(f"{filename}: {exc}")

        self.load_images()
        self.show_image()

        message = f"{moved_count} image(s) moved."
        if failures:
            message += "\n\nNot moved:\n" + "\n".join(failures[:10])
            if len(failures) > 10:
                message += f"\n...and {len(failures) - 10} more."
            messagebox.showwarning("Move Complete", message)
        else:
            messagebox.showinfo("Move Complete", message)

    def jump_to_index(self, _event: tk.Event | None = None) -> None:
        try:
            new_index = int(self.index_var.get()) - 1
            if not 0 <= new_index < len(self.images):
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid Index", f"Please enter a number between 1 and {len(self.images)}.")
            self.index_var.set(str(self.current_index + 1 if self.images else 0))
            return

        self.stop_playback()
        self.current_index = new_index
        self.reset_view()
        self.show_image()

    def toggle_play(self) -> None:
        if self.is_playing:
            self.stop_playback()
            return

        try:
            self.play_delay_ms = max(MIN_PLAY_DELAY_MS, int(self.delay_entry.get()))
        except ValueError:
            messagebox.showerror("Invalid Input", "Delay must be an integer in milliseconds.")
            return

        if not self.images:
            return

        self.is_playing = True
        self.play_button.config(text="Stop")
        self.play_images()

    def play_images(self) -> None:
        if not self.is_playing:
            return

        if self.current_index < len(self.images) - 1:
            self.current_index += 1
            self.reset_view()
            self.show_image()
            self.play_after_id = self.root.after(self.play_delay_ms, self.play_images)
        else:
            self.stop_playback()

    def stop_playback(self) -> None:
        self.is_playing = False
        self.play_button.config(text="Play")
        if self.play_after_id is not None:
            self.root.after_cancel(self.play_after_id)
            self.play_after_id = None

    def close(self) -> None:
        self.stop_playback()
        self.root.destroy()
