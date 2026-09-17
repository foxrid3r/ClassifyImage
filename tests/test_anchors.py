from classify_image.anchors import svg_anchors


def test_points_endpoints_transforms_and_viewbox(tmp_path):
    svg = tmp_path / "anchors.svg"
    svg.write_text("""<svg viewBox="636 280 108 80">
      <defs><circle id="decoration" cx="640" cy="290"/></defs>
      <g data-c="F218"><path d="M 648.157715 294.112122 h 0"/></g>
      <g data-c="F219"><line x1="648.2" y1="294.1" x2="698.2" y2="293.9"/></g>
      <g transform="translate(636 280) scale(2)"><circle id="translated" cx="5" cy="6"/></g>
      <circle id="outside" cx="0" cy="0"/>
    </svg>""")
    anchors = svg_anchors(svg)
    assert len(anchors) == 4
    assert abs(anchors[0].x * 108 - 12.157715) < 1e-8
    assert abs(anchors[0].y * 80 - 14.112122) < 1e-8
    assert anchors[1].key != anchors[2].key
    assert anchors[3].x == 10 / 108
    assert anchors[3].y == 12 / 80


def test_identity_survives_coordinate_changes_and_ignores_duplicates(tmp_path):
    svg = tmp_path / "anchors.svg"
    svg.write_text('<svg viewBox="0 0 100 100"><g data-c="F218"><path d="M 1 2 h 0"/></g></svg>')
    first = svg_anchors(svg)[0]
    svg.write_text('<svg viewBox="0 0 100 100"><g data-c="F218"><path d="M 3 4 h 0"/></g></svg>')
    second = svg_anchors(svg)[0]
    assert first.key == second.key
    assert first.y != second.y
    svg.write_text('<svg viewBox="0 0 100 100"><circle id="same"/><circle id="same"/></svg>')
    assert svg_anchors(svg) == []
