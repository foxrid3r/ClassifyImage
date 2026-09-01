from __future__ import annotations

import os
import re
import shutil
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from xml.etree import ElementTree

from PIL import ExifTags, Image, ImageTk

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
MAX_CLASSES = 10
MIN_PLAY_DELAY_MS = 10
INHERITED_FONT_PROPERTIES = ("font-family", "font-size", "font-style", "font-weight")


def _display_value(value: object, limit: int = 300) -> str:
    """Convert image metadata to a compact, safe display string."""
    if isinstance(value, bytes):
        return f"{len(value):,} bytes"
    if isinstance(value, tuple):
        text = " × ".join(str(item) for item in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def image_metadata(image_path: Path, overlay_path: Path | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Collect grouped file, raster, embedded, and EXIF metadata."""
    stat = image_path.stat()
    file_details = [
        ("Name", image_path.name),
        ("Folder", os.fspath(image_path.parent)),
        ("File size", f"{stat.st_size:,} bytes"),
        ("Modified", datetime.fromtimestamp(stat.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("SVG overlay", overlay_path.name if overlay_path is not None else "None"),
    ]

    with Image.open(image_path) as source:
        image_details = [
            ("Format", source.format or image_path.suffix.removeprefix(".").upper()),
            ("Dimensions", f"{source.width:,} × {source.height:,} pixels"),
            ("Megapixels", f"{source.width * source.height / 1_000_000:.2f}"),
            ("Color mode", source.mode),
            ("Bands", ", ".join(source.getbands())),
            ("Frames", str(getattr(source, "n_frames", 1))),
            ("Animated", "Yes" if getattr(source, "is_animated", False) else "No"),
        ]

        embedded_details: list[tuple[str, str]] = []
        for key, value in sorted(source.info.items()):
            label = key.replace("_", " ").title()
            embedded_details.append((label, _display_value(value)))

        exif_details: list[tuple[str, str]] = []
        try:
            for tag_id, value in source.getexif().items():
                tag = ExifTags.TAGS.get(tag_id, f"Tag {tag_id}")
                exif_details.append((str(tag), _display_value(value)))
        except (AttributeError, OSError, ValueError):
            pass
        exif_details.sort(key=lambda item: item[0].casefold())

    sections = [("File", file_details), ("Image", image_details)]
    if embedded_details:
        sections.append(("Embedded metadata", embedded_details))
    if exif_details:
        sections.append(("EXIF", exif_details))
    return sections


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


def _svg_viewport_size(root: ElementTree.Element) -> tuple[float, float] | None:
    """Return the SVG coordinate viewport when it can be determined."""
    view_box = root.get("viewBox")
    if view_box:
        try:
            _, _, width, height = (float(value) for value in view_box.replace(",", " ").split())
            if width > 0 and height > 0:
                return width, height
        except (TypeError, ValueError):
            pass
    try:
        width = float(root.get("width", "").removesuffix("px"))
        height = float(root.get("height", "").removesuffix("px"))
        return (width, height) if width > 0 and height > 0 else None
    except (TypeError, ValueError):
        return None


def _style_declarations(style_text: str) -> dict[str, str]:
    return {
        name.strip(): value.strip()
        for declaration in style_text.split(";")
        if ":" in declaration
        for name, value in (declaration.split(":", 1),)
    }


def _normalized_font_size(value: str) -> str:
    """Convert point font sizes to pixels for resvg's text layout."""
    if value.casefold().endswith("pt"):
        try:
            return f"{float(value[:-2]) * 96 / 72:g}px"
        except ValueError:
            pass
    return value


def _materialize_svg_font_styles(root: ElementTree.Element) -> None:
    """Put inherited CSS font properties directly on text for renderer compatibility."""
    class_styles: dict[str, dict[str, str]] = {}
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "style" and element.text:
            for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", element.text):
                for class_name in re.findall(r"\.([\w-]+)", selector):
                    class_styles.setdefault(class_name, {}).update(_style_declarations(declarations))

    def visit(element: ElementTree.Element, inherited: dict[str, str]) -> None:
        effective = inherited.copy()
        for class_name in element.get("class", "").split():
            effective.update(class_styles.get(class_name, {}))
        effective.update(_style_declarations(element.get("style", "")))
        for property_name in INHERITED_FONT_PROPERTIES:
            if property_name in element.attrib:
                effective[property_name] = element.attrib[property_name]

        tag_name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if tag_name in {"text", "tspan"}:
            for property_name in INHERITED_FONT_PROPERTIES:
                if property_name in effective and (property_name == "font-size" or property_name not in element.attrib):
                    value = effective[property_name]
                    element.set(property_name, _normalized_font_size(value) if property_name == "font-size" else value)
        for child in element:
            visit(child, effective)

    visit(root, {})


def svg_with_line_width(
    svg_path: Path,
    line_width: float,
    render_size: tuple[int, int] | None = None,
    text_size: float | None = None,
) -> bytes:
    """Return SVG data with uniform screen-pixel stroke and text sizes."""
    root = ElementTree.parse(svg_path).getroot()
    _materialize_svg_font_styles(root)
    source_line_width = line_width
    source_text_size = text_size
    viewport_size = _svg_viewport_size(root)
    if render_size is not None and viewport_size is not None:
        scale = min(render_size[0] / viewport_size[0], render_size[1] / viewport_size[1])
        if scale > 0:
            source_line_width = line_width / scale
            if text_size is not None:
                source_text_size = text_size / scale
    namespace = root.tag.partition("}")[0].removeprefix("{") if "}" in root.tag else ""
    style_tag = f"{{{namespace}}}style" if namespace else "style"
    style = ElementTree.Element(style_tag, {"type": "text/css"})
    style.text = (
        f"path, line, polyline, polygon, rect, circle, ellipse {{ stroke-width: {source_line_width:g} !important; }}"
    )
    root.insert(0, style)
    marker_tag = f"{{{namespace}}}marker" if namespace else "marker"
    for marker in root.iter(marker_tag):
        marker.set("markerUnits", "strokeWidth")
    if source_text_size is not None:
        for element in root.iter():
            tag_name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
            if tag_name in {"text", "tspan"}:
                element.set("font-size", f"{source_text_size:g}px")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


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
        self.overlay_paths: dict[str, Path] = {}

        self.zoom_factor = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.start_x = 0
        self.start_y = 0

        self.image: Image.Image | None = None
        self.rendered_image_cache: tuple[tuple[int, int], ImageTk.PhotoImage] | None = None
        self.overlay_path: Path | None = None
        self.overlay_cache: OrderedDict[tuple[int, int], ImageTk.PhotoImage] = OrderedDict()
        self.photo: ImageTk.PhotoImage | None = None
        self.overlay_enabled = tk.BooleanVar(value=True)
        self.overlay_line_width = 1.0
        self.overlay_line_width_var = tk.StringVar(value="1")
        self.overlay_text_size = 16.0
        self.overlay_text_size_var = tk.StringVar(value="16")
        self.transfer_mode = tk.StringVar(value="Move")
        self.details_visible = False
        self.is_playing = False
        self.play_delay_ms = 1000
        self.play_after_id: str | None = None
        self.image_loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-loader")
        self.prefetched_images: dict[Path, Future[Image.Image]] = {}

        self._create_widgets()

    def _create_widgets(self) -> None:
        file_manage_frame = ttk.Frame(self.root)
        file_manage_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        file_action_frame = ttk.Frame(file_manage_frame)
        file_action_frame.pack(anchor=tk.CENTER)
        overlay_control_frame = ttk.Frame(file_manage_frame)
        overlay_control_frame.pack(anchor=tk.CENTER, pady=(5, 0))

        ttk.Button(file_action_frame, text="Select Folder", command=self.select_folder).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(file_action_frame, text="Define Classes", command=self.define_classes).pack(side=tk.LEFT, padx=5)
        self.details_button = ttk.Button(file_action_frame, text="Show Details", command=self.toggle_image_details)
        self.details_button.pack(side=tk.LEFT, padx=5)
        ttk.Separator(file_action_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Label(file_action_frame, text="Classified files:").pack(side=tk.LEFT, padx=(0, 5))
        self.transfer_mode_selector = ttk.Combobox(
            file_action_frame,
            width=6,
            state="readonly",
            textvariable=self.transfer_mode,
            values=("Move", "Copy"),
        )
        self.transfer_mode_selector.pack(side=tk.LEFT, padx=(0, 5))
        self.transfer_mode_selector.bind("<<ComboboxSelected>>", self._update_transfer_button)
        self.transfer_button = ttk.Button(
            file_action_frame,
            text="Move Classified",
            command=self.move_classified_images,
        )
        self.transfer_button.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Checkbutton(
            overlay_control_frame,
            text="Show SVG Overlay",
            variable=self.overlay_enabled,
            command=self.toggle_svg_overlay,
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Label(overlay_control_frame, text="Line width").pack(side=tk.LEFT, padx=(10, 5))
        self.overlay_line_width_spinbox = ttk.Spinbox(
            overlay_control_frame,
            from_=0.1,
            to=100.0,
            increment=0.5,
            width=5,
            textvariable=self.overlay_line_width_var,
            command=self.set_overlay_line_width,
        )
        self.overlay_line_width_spinbox.pack(side=tk.LEFT)
        self.overlay_line_width_spinbox.bind("<Return>", self.set_overlay_line_width)
        self.overlay_line_width_spinbox.bind("<FocusOut>", self.set_overlay_line_width)
        ttk.Label(overlay_control_frame, text="Text size").pack(side=tk.LEFT, padx=(15, 5))
        self.overlay_text_size_spinbox = ttk.Spinbox(
            overlay_control_frame,
            from_=1.0,
            to=200.0,
            increment=1.0,
            width=5,
            textvariable=self.overlay_text_size_var,
            command=self.set_overlay_text_size,
        )
        self.overlay_text_size_spinbox.pack(side=tk.LEFT)
        self.overlay_text_size_spinbox.bind("<Return>", self.set_overlay_text_size)
        self.overlay_text_size_spinbox.bind("<FocusOut>", self.set_overlay_text_size)

        self.content_pane = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        self.content_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        canvas_frame = ttk.Frame(self.content_pane)
        self.content_pane.add(canvas_frame, weight=4)
        self.canvas = tk.Canvas(canvas_frame, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<MouseWheel>", self.zoom_image)
        self.canvas.bind("<Button-4>", self.zoom_image)
        self.canvas.bind("<Button-5>", self.zoom_image)
        self.canvas.bind("<Double-Button-2>", self.fit_image_to_window)
        self.canvas.bind("<ButtonPress-1>", self.start_pan)
        self.canvas.bind("<B1-Motion>", self.pan_image)
        self.canvas.bind("<Configure>", lambda _event: self.update_canvas())

        self.details_frame = ttk.Frame(self.content_pane, padding=(8, 0, 0, 0))
        details_header = ttk.Frame(self.details_frame)
        details_header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(details_header, text="Image Inspector", font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(details_header, text="Close", command=self.toggle_image_details).pack(side=tk.RIGHT)

        details_tree_frame = ttk.Frame(self.details_frame)
        details_tree_frame.pack(fill=tk.BOTH, expand=True)
        self.details_tree = ttk.Treeview(
            details_tree_frame, columns=("value",), show="tree headings", selectmode="browse"
        )
        self.details_tree.heading("#0", text="Property", anchor=tk.W)
        self.details_tree.heading("value", text="Value", anchor=tk.W)
        self.details_tree.column("#0", width=135, minwidth=90, stretch=False)
        self.details_tree.column("value", width=240, minwidth=140, stretch=True)
        details_scrollbar = ttk.Scrollbar(details_tree_frame, orient=tk.VERTICAL, command=self.details_tree.yview)
        self.details_tree.configure(yscrollcommand=details_scrollbar.set)
        self.details_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        details_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Button(self.details_frame, text="Copy Selected Value", command=self.copy_selected_detail).pack(
            fill=tk.X, pady=(6, 0)
        )

        self.directory_label = ttk.Label(self.root, text="No folder selected", anchor=tk.W)
        self.directory_label.pack(fill=tk.X, padx=10, pady=(0, 5))

        self.classification_frame = ttk.Frame(self.root)
        self.classification_frame.pack(padx=10, pady=5)

        self.btn_remove_classification = ttk.Button(
            self.classification_frame,
            text="Remove Classification",
            command=self.remove_classification,
        )

        file_and_class_frame = ttk.Frame(self.root)
        file_and_class_frame.pack(padx=10, pady=5)

        self.filename_label = ttk.Label(file_and_class_frame, text="", font=("Segoe UI", 11))
        self.filename_label.pack(side=tk.LEFT, padx=(0, 10))

        self.status_label = ttk.Label(file_and_class_frame, text="", font=("Segoe UI", 11))
        self.status_label.pack(side=tk.LEFT)

        self.transfer_progress = ttk.Progressbar(self.root, mode="determinate")

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
        self._clear_prefetch()
        self.folder_path = Path(folder_selected)
        self.directory_label.config(text=f"{self.folder_path}")
        self.load_images()
        self.reset_view()
        self.show_image()

    def load_images(self) -> None:
        if self.folder_path is None:
            self.images = []
            return

        paths = [path for path in self.folder_path.iterdir() if path.is_file()]
        self.images = sorted(path.name for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS)
        self.overlay_paths = {path.stem.casefold(): path for path in paths if path.suffix.casefold() == ".svg"}
        self.current_index = 0
        self.classified_map.clear()

    @staticmethod
    def _open_image(image_path: Path) -> Image.Image:
        with Image.open(image_path) as source_image:
            return source_image.copy()

    def _clear_prefetch(self, *, wait: bool = False) -> None:
        futures = list(self.prefetched_images.values())
        for future in futures:
            future.cancel()
        if wait:
            wait_for_futures(futures)
        self.prefetched_images.clear()

    def _prefetch_next_image(self) -> None:
        if self.folder_path is None or self.current_index >= len(self.images) - 1:
            return
        next_path = self.folder_path / self.images[self.current_index + 1]
        if next_path not in self.prefetched_images:
            self.prefetched_images[next_path] = self.image_loader.submit(self._open_image, next_path)

    def show_image(self) -> None:
        if not self.images or self.folder_path is None:
            self._clear_image_display()
            return

        image_path = self.folder_path / self.images[self.current_index]
        try:
            future = self.prefetched_images.pop(image_path, None)
            self.image = future.result() if future is not None else self._open_image(image_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Image Error", f"Failed to open image:\n{image_path}\n\n{exc}")
            return

        self.rendered_image_cache = None
        self._load_overlay(image_path)
        self._update_image_details(image_path)
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
        self._clear_unneeded_prefetch()
        self._prefetch_next_image()

    def _clear_unneeded_prefetch(self) -> None:
        if self.folder_path is None:
            self._clear_prefetch()
            return
        wanted = (
            self.folder_path / self.images[self.current_index + 1]
            if self.current_index < len(self.images) - 1
            else None
        )
        for path, future in list(self.prefetched_images.items()):
            if path != wanted:
                future.cancel()
                del self.prefetched_images[path]

    def _clear_image_display(self) -> None:
        self.image = None
        self.rendered_image_cache = None
        self.overlay_path = None
        self.overlay_cache.clear()
        self.photo = None
        self.canvas.delete("all")
        self.filename_label.config(text="")
        self.status_label.config(text="")
        self.index_var.set("0")
        self.total_label.config(text="/ 0")
        self._clear_image_details()

    def _load_overlay(self, image_path: Path) -> None:
        self.overlay_path = self.overlay_paths.get(image_path.stem.casefold())
        self.overlay_cache.clear()

    def _get_rendered_overlay(self, size: tuple[int, int]) -> ImageTk.PhotoImage | None:
        if self.overlay_path is None:
            return None
        if size in self.overlay_cache:
            self.overlay_cache.move_to_end(size)
            return self.overlay_cache[size]

        assert self.image is not None
        try:
            import resvg_py

            svg_png = resvg_py.svg_to_bytes(
                svg_string=svg_with_line_width(
                    self.overlay_path,
                    self.overlay_line_width,
                    size,
                    self.overlay_text_size,
                ).decode("utf-8"),
                width=size[0],
                height=size[1],
                resources_dir=os.fspath(self.overlay_path.parent),
            )
            with Image.open(BytesIO(svg_png)) as overlay_image:
                rendered_overlay = ImageTk.PhotoImage(overlay_image.convert("RGBA"), master=self.root)
        except Exception as exc:  # noqa: BLE001 -- SVG renderers can surface backend-specific exceptions.
            messagebox.showwarning(
                "SVG Overlay Error",
                f"Failed to load overlay:\n{self.overlay_path}\n\n{exc}",
            )
            self.overlay_path = None
            self.overlay_cache.clear()
            return None

        self.overlay_cache[size] = rendered_overlay
        if len(self.overlay_cache) > 8:
            self.overlay_cache.popitem(last=False)
        return rendered_overlay

    def set_overlay_line_width(self, _event: tk.Event | None = None) -> None:
        try:
            line_width = float(self.overlay_line_width_var.get())
            if line_width <= 0:
                raise ValueError
        except ValueError:
            self.overlay_line_width_var.set(f"{self.overlay_line_width:g}")
            return

        if line_width == self.overlay_line_width:
            return
        self.overlay_line_width = line_width
        self.overlay_cache.clear()
        self.update_canvas()

    def toggle_svg_overlay(self) -> None:
        """Toggle the overlay and its related controls as one UI state."""
        state = "normal" if self.overlay_enabled.get() else "disabled"
        self.overlay_line_width_spinbox.config(state=state)
        self.overlay_text_size_spinbox.config(state=state)
        self.update_canvas()

    def set_overlay_text_size(self, _event: tk.Event | None = None) -> None:
        try:
            text_size = float(self.overlay_text_size_var.get())
            if text_size <= 0:
                raise ValueError
        except ValueError:
            self.overlay_text_size_var.set(f"{self.overlay_text_size:g}")
            return

        if text_size == self.overlay_text_size:
            return
        self.overlay_text_size = text_size
        self.overlay_cache.clear()
        self.update_canvas()

    def update_canvas(self) -> None:
        if self.image is None:
            return

        canvas_width = max(self.canvas.winfo_width(), 1)
        canvas_height = max(self.canvas.winfo_height(), 1)
        fit_width, fit_height = fitted_size(self.image.size, (canvas_width, canvas_height))
        new_width = max(1, int(fit_width * self.zoom_factor))
        new_height = max(1, int(fit_height * self.zoom_factor))

        # Nearest-neighbor scaling keeps source pixels as hard-edged blocks.
        render_size = (new_width, new_height)
        if self.rendered_image_cache is None or self.rendered_image_cache[0] != render_size:
            resized_image = self.image.resize(render_size, Image.Resampling.NEAREST)
            self.rendered_image_cache = (render_size, ImageTk.PhotoImage(resized_image, master=self.root))
        self.photo = self.rendered_image_cache[1]

        center_x = self.offset_x + canvas_width // 2
        center_y = self.offset_y + canvas_height // 2

        self.canvas.delete("all")
        self.canvas.create_image(center_x, center_y, image=self.photo, anchor=tk.CENTER)
        if self.overlay_enabled.get():
            rendered_overlay = self._get_rendered_overlay((new_width, new_height))
            if rendered_overlay is not None:
                self.canvas.create_image(center_x, center_y, image=rendered_overlay, anchor=tk.CENTER)

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
        delta_x = event.x - self.start_x
        delta_y = event.y - self.start_y
        self.offset_x += delta_x
        self.offset_y += delta_y
        self.start_x = event.x
        self.start_y = event.y
        self.canvas.move("all", delta_x, delta_y)

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

    def fit_image_to_window(self, _event: tk.Event | None = None) -> None:
        """Reset zoom and pan so the image fits inside the canvas."""
        self.reset_view()
        self.update_canvas()

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

        def save_classes(_event: tk.Event | None = None) -> None:
            class_names = [entry.get().strip() for entry in entries if entry.get().strip()]
            if len(set(class_names)) != len(class_names):
                messagebox.showwarning(
                    "Duplicate Classes", "Each classification name must be unique.", parent=class_window
                )
                return

            self.classifications = class_names
            class_window.destroy()
            self.create_classification_buttons()

        button_frame = ttk.Frame(class_window)
        button_frame.pack(pady=10)
        ttk.Button(button_frame, text="Save", command=save_classes).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=class_window.destroy).pack(side=tk.LEFT, padx=5)
        class_window.bind("<Return>", save_classes)

        class_window.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - class_window.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - class_window.winfo_height()) // 2
        class_window.geometry(f"+{x}+{y}")
        entries[0].focus_set()

    def create_classification_buttons(self) -> None:
        for widget in self.classification_frame.winfo_children():
            widget.destroy()
        for index in range(1, 10):
            self.root.unbind(str(index))

        for index, class_name in enumerate(self.classifications, start=1):
            button = ttk.Button(
                self.classification_frame,
                text=f"{index}: {class_name}",
                command=lambda name=class_name: self.classify_image(name),
            )
            button.pack(side=tk.LEFT, padx=5, pady=5)
            if index <= 9:
                self.root.bind(str(index), lambda event, name=class_name: self._classification_shortcut(event, name))

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

    def _classification_shortcut(self, event: tk.Event, class_name: str) -> None:
        """Classify only when a number key was not typed into an input control."""
        if isinstance(event.widget, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Spinbox)):
            return
        self.classify_image(class_name)

    def _update_transfer_button(self, _event: tk.Event | None = None) -> None:
        self.transfer_button.config(text=f"{self.transfer_mode.get()} Classified")

    def toggle_image_details(self) -> None:
        """Show or hide the persistent image metadata inspector."""
        self.details_visible = not self.details_visible
        if self.details_visible:
            self.content_pane.add(self.details_frame, weight=2)
            self.details_button.config(text="Hide Details")
            if self.folder_path is not None and self.images:
                self._update_image_details(self.folder_path / self.images[self.current_index])
        else:
            self.content_pane.forget(self.details_frame)
            self.details_button.config(text="Show Details")
        self.root.after_idle(self.update_canvas)

    def _clear_image_details(self) -> None:
        self.details_tree.delete(*self.details_tree.get_children())

    def _update_image_details(self, image_path: Path) -> None:
        if not self.details_visible:
            return
        self._clear_image_details()
        try:
            sections = image_metadata(image_path, self.overlay_path)
        except (OSError, ValueError) as exc:
            self.details_tree.insert("", tk.END, text="Metadata unavailable", values=(str(exc),))
            return

        for section, details in sections:
            parent = self.details_tree.insert("", tk.END, text=section, open=True)
            for name, value in details:
                self.details_tree.insert(parent, tk.END, text=name, values=(value,))

    def copy_selected_detail(self) -> None:
        selection = self.details_tree.selection()
        if not selection:
            return
        values = self.details_tree.item(selection[0], "values")
        if not values:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(values[0])

    def remove_classification(self) -> None:
        if not self.images:
            return
        self.classified_map.pop(self.images[self.current_index], None)
        self.show_image()

    def move_classified_images(self) -> None:
        if not self.classified_map or self.folder_path is None:
            messagebox.showinfo("No Classifications", "No images have been classified yet.")
            return

        operation = self.transfer_mode.get()
        copying = operation == "Copy"
        if not messagebox.askyesno(
            f"{operation} Images",
            f"{operation} {len(self.classified_map)} classified image(s) into class subfolders?",
        ):
            return

        self.stop_playback()
        self._clear_prefetch(wait=True)
        if not copying:
            self._clear_image_display()

        self.transfer_progress.config(maximum=len(self.classified_map), value=0)
        self.transfer_progress.pack(fill=tk.X, padx=10, pady=(0, 5))
        self.root.update_idletasks()

        completed_count = 0
        failures: list[str] = []
        transfer = shutil.copy2 if copying else shutil.move
        for progress, (filename, classification) in enumerate(list(self.classified_map.items()), start=1):
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
                transfer(os.fspath(source), os.fspath(destination))
                if overlay_source is not None and overlay_destination is not None:
                    transfer(os.fspath(overlay_source), os.fspath(overlay_destination))
                completed_count += 1
            except OSError as exc:
                failures.append(f"{filename}: {exc}")
            finally:
                self.transfer_progress.config(value=progress)
                self.root.update_idletasks()

        self.transfer_progress.pack_forget()

        if not copying:
            self.load_images()
            self.show_image()

        action = "copied" if copying else "moved"
        message = f"{completed_count} image(s) {action}."
        if failures:
            message += f"\n\nNot {action}:\n" + "\n".join(failures[:10])
            if len(failures) > 10:
                message += f"\n...and {len(failures) - 10} more."
            messagebox.showwarning(f"{operation} Complete", message)
        else:
            messagebox.showinfo(f"{operation} Complete", message)

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
        self._clear_prefetch()
        self.image_loader.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
