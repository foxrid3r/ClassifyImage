from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterable
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from PIL import ExifTags, Image

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
MAX_CLASSES = 10
MIN_PLAY_DELAY_MS = 10
INHERITED_FONT_PROPERTIES = ("font-family", "font-size", "font-style", "font-weight")
SVG_GRAPHICS = {"path", "line", "polyline", "polygon", "rect", "circle", "ellipse", "text", "image", "use"}


def svg_graphics(root: ElementTree.Element) -> list[tuple[str, str, ElementTree.Element]]:
    """Identify visible-tree graphics, excluding reusable definitions and text children."""
    result = []

    def visit(element: ElementTree.Element, path: str, identity: str = "") -> None:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"defs", "clipPath", "mask", "marker", "pattern", "symbol"}:
            return
        identity = element.get("id") or element.get("data-c") or identity
        if tag in SVG_GRAPHICS:
            identifier = element.get("id")
            key = f"id:{identifier}" if identifier else f"path:{path}"
            label = f"{identity or path} — {tag}"
            result.append((key, label, element))
            return
        for index, child in enumerate(element):
            visit(child, f"{path}/{index}", identity)

    visit(root, "0")
    return result


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


def transfer_classified_files(
    folder_path: Path,
    classified_items: Iterable[tuple[str, str]],
    *,
    copying: bool,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[int, list[str]]:
    """Transfer classified files without depending on the Tk event thread."""
    completed_count = 0
    failures: list[str] = []
    transfer = shutil.copy2 if copying else shutil.move
    for progress, (filename, classification) in enumerate(classified_items, start=1):
        source = folder_path / filename
        destination_dir = folder_path / classification
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
            if progress_callback is not None:
                progress_callback(progress)

    return completed_count, failures


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
    hidden_elements: set[str] | None = None,
) -> bytes:
    """Return SVG data with uniform screen-pixel stroke and text sizes."""
    root = ElementTree.parse(svg_path).getroot()
    for key, _, element in svg_graphics(root):
        if hidden_elements and key in hidden_elements:
            element.set("display", "none")
            element.set("style", element.get("style", "") + ";display:none !important")
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


def visible_svg_graphics(
    svg_path: Path, size: tuple[int, int], line_width: float = 1, text_size: float = 16
) -> list[tuple[str, str, tuple[float, float, float, float]]]:
    """Find rendered bounds within the viewport, including transforms, clipping and markers.

    Inspect source visibility, not the user's hidden-element choices, so hidden rows
    remain available to show again. Bounds are fractions of the rendered viewport.
    """
    import resvg_py

    # Preserve document-position keys before svg_with_line_width inserts its style.
    originals = svg_graphics(ElementTree.parse(svg_path).getroot())
    root = ElementTree.fromstring(svg_with_line_width(svg_path, line_width, size, text_size))
    graphics = svg_graphics(root)
    parents = {child: (parent, index) for parent in root.iter() for index, child in enumerate(parent)}
    namespace = root.tag.partition("}")[0] + "}" if "}" in root.tag else ""
    wrappers = []
    # Keep other graphics available as references (e.g. <use href="#shape">),
    # while preventing them from painting directly during each isolated render.
    for _, _, element in graphics:
        parent, index = parents[element]
        wrapper = ElementTree.Element(f"{namespace}defs")
        wrapper.append(element)
        parent[index] = wrapper
        wrappers.append(wrapper)
    result = []
    for (key, label, _), (_, _, element), wrapper in zip(originals, graphics, wrappers):
        parent, index = parents[element]
        parent[index] = element
        png = resvg_py.svg_to_bytes(
            svg_string=ElementTree.tostring(root, encoding="unicode"),
            width=size[0],
            height=size[1],
            resources_dir=os.fspath(svg_path.parent),
        )
        with Image.open(BytesIO(png)) as rendered:
            bounds = rendered.convert("RGBA").getchannel("A").getbbox()
            if bounds is not None:
                left, top, right, bottom = bounds
                result.append((key, label, (left / size[0], top / size[1], right / size[0], bottom / size[1])))
        parent[index] = wrapper
    return result
