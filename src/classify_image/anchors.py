"""Extract explicitly identified SVG points and line endpoints for view locking."""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(a, b):
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def transform(value):
    result = IDENTITY
    for name, arguments in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", value):
        p = [float(n) for n in re.findall(NUMBER, arguments)]
        if name == "matrix" and len(p) == 6:
            m = tuple(p)
        elif name == "translate" and len(p) in (1, 2):
            m = (1, 0, 0, 1, p[0], p[1] if len(p) == 2 else 0)
        elif name == "scale" and len(p) in (1, 2):
            m = (p[0], 0, 0, p[-1], 0, 0)
        elif name == "rotate" and len(p) in (1, 3):
            c, s = math.cos(math.radians(p[0])), math.sin(math.radians(p[0]))
            m = (c, s, -s, c, 0, 0)
            if len(p) == 3:
                m = multiply(multiply((1, 0, 0, 1, p[1], p[2]), m), (1, 0, 0, 1, -p[1], -p[2]))
        elif name in ("skewX", "skewY") and len(p) == 1:
            t = math.tan(math.radians(p[0]))
            m = (1, 0, t, 1, 0, 0) if name == "skewX" else (1, t, 0, 1, 0, 0)
        else:
            raise ValueError("Unsupported SVG transform")
        result = multiply(result, m)
    return result


@dataclass(frozen=True)
class Anchor:
    key: str
    label: str
    x: float  # Fraction of the rendered SVG viewport.
    y: float


def svg_anchors(path: Path) -> list[Anchor]:
    root = ET.parse(path).getroot()
    if root.get("viewBox"):
        vx, vy, width, height = map(float, root.get("viewBox").replace(",", " ").split())
    else:
        vx = vy = 0
        width, height = (float(root.get(k, "0").removesuffix("px")) for k in ("width", "height"))
    if width <= 0 or height <= 0:
        return []
    result = []

    def visit(element, parent_matrix=IDENTITY, identity="", trail=""):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"defs", "marker", "clipPath", "mask", "symbol"}:
            return
        if element.get("display") == "none" or "display:none" in element.get("style", "").replace(" ", ""):
            return
        # Nested SVG viewports require their own viewport mapping.
        if tag == "svg" and element is not root:
            return
        try:
            matrix = multiply(parent_matrix, transform(element.get("transform", "")))
        except ValueError:
            return
        name = element.get("id") or element.get("data-c")
        if name:
            identity, trail = name, ""
        points = []
        try:
            if tag == "line":
                points = [
                    ("start", float(element.get("x1", "0")), float(element.get("y1", "0"))),
                    ("end", float(element.get("x2", "0")), float(element.get("y2", "0"))),
                ]
            elif tag in {"circle", "ellipse"}:
                points = [("point", float(element.get("cx", "0")), float(element.get("cy", "0")))]
            elif tag == "path":
                match = re.fullmatch(
                    rf"\s*[Mm]\s*({NUMBER})[\s,]+({NUMBER})\s*[hHvV]\s*0(?:\.0*)?\s*", element.get("d", "")
                )
                if match:
                    points = [("point", float(match[1]), float(match[2]))]
        except ValueError:
            points = []
        for endpoint, x, y in points:
            x, y = matrix[0] * x + matrix[2] * y + matrix[4], matrix[1] * x + matrix[3] * y + matrix[5]
            if identity and all(map(math.isfinite, (x, y))) and vx <= x <= vx + width and vy <= y <= vy + height:
                result.append(
                    Anchor(
                        f"{identity}/{trail}/{endpoint}",
                        f"{identity} — {tag} {endpoint}",
                        (x - vx) / width,
                        (y - vy) / height,
                    )
                )
        counts = {}
        for child in element:
            kind = child.tag.rsplit("}", 1)[-1]
            index = counts.get(kind, 0)
            counts[kind] = index + 1
            visit(child, matrix, identity, f"{trail}/{kind}[{index}]")

    visit(root)
    # Ambiguous identities must never silently select the wrong feature.
    return [a for a in result if sum(b.key == a.key for b in result) == 1]
