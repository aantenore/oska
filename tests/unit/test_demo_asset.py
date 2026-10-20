"""The animated terminal demo in the README header, checked against its own transcript.

`tools/render_demo.py` turns `docs/assets/demo.transcript.txt` into `docs/assets/demo.svg`. The
image is committed, so it must be exactly what the script makes from the committed transcript, it
must carry no script (the host strips it), and it must show every line of the transcript.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPT = REPO_ROOT / "docs" / "assets" / "demo.transcript.txt"
SVG = REPO_ROOT / "docs" / "assets" / "demo.svg"
SVG_LIMIT = 60 * 1024


def _renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "render_demo", REPO_ROOT / "tools" / "render_demo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _texts(svg: str) -> list[str]:
    root = ET.fromstring(svg)
    return ["".join(node.itertext()) for node in root.iter("{http://www.w3.org/2000/svg}text")]


def test_committed_svg_is_the_render_of_the_committed_transcript() -> None:
    rendered = _renderer().render(TRANSCRIPT.read_text(encoding="utf-8"))
    assert SVG.read_bytes() == rendered.encode("utf-8")


def test_render_is_deterministic() -> None:
    render = _renderer().render
    text = TRANSCRIPT.read_text(encoding="utf-8")
    assert render(text) == render(text)


def test_svg_has_no_script_and_stays_small() -> None:
    svg = SVG.read_text(encoding="utf-8")
    ET.fromstring(svg)
    assert not re.search(r"<script|javascript:|\son\w+=|<foreignObject|href=", svg, re.IGNORECASE)
    assert SVG.stat().st_size < SVG_LIMIT


def test_svg_respects_reduced_motion() -> None:
    assert "prefers-reduced-motion:reduce" in SVG.read_text(encoding="utf-8")


def test_every_transcript_line_is_in_the_svg() -> None:
    shown = _texts(SVG.read_text(encoding="utf-8"))
    lines = [line for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    missing = [line for line in lines if line not in shown]
    assert missing == []


def test_markup_characters_are_escaped() -> None:
    line = '> a <b> & "c" </text><script>x</script>'
    svg = _renderer().render(f"$ run\n{line}\nout & <done>\n")
    assert "<script" not in svg
    assert "&lt;script&gt;" in svg
    assert "&amp;" in svg
    assert _texts(svg)[2:] == [line, "out & <done>"]


def test_a_line_wider_than_the_window_is_refused() -> None:
    with pytest.raises(ValueError, match="window fits"):
        _renderer().render("x" * 200)
