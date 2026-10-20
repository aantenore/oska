"""Render a terminal transcript as a self-contained animated SVG.

The transcript is a plain text file. A line that starts with ``$ `` is a typed shell command, a line
that starts with ``> `` is a typed prompt answer, and every other line is output that appears whole.
The SVG has no script and no external font: the animation is CSS ``@keyframes``, it loops with a
pause at the end, and ``prefers-reduced-motion`` shows every line at once. The same transcript
always produces the same bytes.

Usage: ``python tools/render_demo.py [transcript] [svg]``
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRANSCRIPT = ROOT / "docs" / "assets" / "demo.transcript.txt"
DEFAULT_SVG = ROOT / "docs" / "assets" / "demo.svg"

WIDTH = 900
PADDING = 20
TITLE_HEIGHT = 36
LINE_HEIGHT = 20
FONT_SIZE = 12
CHAR_WIDTH = 7.2
TITLE = "oska run"

START_DELAY = 0.6
OUTPUT_GAP = 0.22
TYPING_RATE = 32.0
BEFORE_TYPING = 0.45
AFTER_TYPING = 0.75
END_HOLD = 5.0

BACKGROUND = "#0d1117"
BAR = "#161b22"
FONT_FAMILY = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"
DOTS = (("#ff5f57", 20), ("#febc2e", 40), ("#28c840", 60))

# First match wins. Each rule maps a line to a colour class.
STYLES = (
    (r"^\$ ", "cmd"),
    (r"^> ", "typed"),
    (r"^(awaiting approval|approval_pending)\b", "hold"),
    (r"^\s+", "hold"),
    (r"^receipt\b", "done"),
    (r"^ledger_hit\b", "hit"),
    (r"^read\b", "dim"),
    (r"", "out"),
)
COLORS = {
    "cmd": "#e6edf3",
    "typed": "#79c0ff",
    "hold": "#e3b341",
    "done": "#3fb950",
    "hit": "#56d4dd",
    "dim": "#8b949e",
    "out": "#e6edf3",
}
TYPED_PREFIXES = ("$ ", "> ")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Row:
    """One transcript line with its colour class and when it appears."""

    text: str
    style: str
    typed: bool
    start: float
    duration: float


def _style(line: str) -> str:
    return next(name for pattern, name in STYLES if re.match(pattern, line))


def _schedule(lines: list[str]) -> tuple[list[Row], float]:
    """Give every line a start time; return the rows and the length of one loop in seconds."""
    rows: list[Row] = []
    clock = START_DELAY
    max_chars = (WIDTH - 2 * PADDING) / CHAR_WIDTH
    for line in lines:
        if _CONTROL.search(line):
            raise ValueError(f"control character in transcript line: {line!r}")
        if len(line) > max_chars:
            raise ValueError(
                f"transcript line is {len(line)} characters, the window fits {max_chars:.0f}"
            )
        typed = line.startswith(TYPED_PREFIXES)
        if typed:
            clock += BEFORE_TYPING
            duration = len(line) / TYPING_RATE
            rows.append(Row(line, _style(line), True, clock, duration))
            clock += duration + AFTER_TYPING
        else:
            rows.append(Row(line, _style(line), False, clock, 0.0))
            clock += OUTPUT_GAP
    return rows, clock + END_HOLD


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _pct(seconds: float, total: float) -> str:
    return _num(seconds / total * 100)


def _keyframes(index: int, row: Row, total: float) -> str:
    start = _pct(row.start, total)
    if not row.typed:
        return f"@keyframes k{index}{{0%{{opacity:0}}{start}%{{opacity:1}}100%{{opacity:1}}}}"
    end = _pct(row.start + row.duration, total)
    reach = _num(len(row.text) * CHAR_WIDTH + 2)
    return (
        f"@keyframes k{index}{{0%{{transform:translateX(0)}}{start}%{{transform:translateX(0)}}"
        f"{end}%{{transform:translateX({reach}px)}}100%{{transform:translateX({reach}px)}}}}"
    )


def _css(rows: list[Row], total: float) -> str:
    out = [
        f"text{{font-family:{FONT_FAMILY};font-size:{FONT_SIZE}px;white-space:pre}}",
        *(f".{name}{{fill:{color}}}" for name, color in COLORS.items()),
        f".a{{animation-duration:{_num(total)}s;animation-iteration-count:infinite;"
        "animation-timing-function:step-end}",
    ]
    for index, row in enumerate(rows):
        out.append(_keyframes(index, row, total))
        rule = f"#e{index}{{animation-name:k{index}"
        if row.typed:
            rule += f";animation-timing-function:steps({len(row.text)},end)"
        out.append(rule + "}")
    out.append(
        "@media (prefers-reduced-motion:reduce){.a{animation:none!important}.m{display:none}}"
    )
    return "\n".join(out)


def _row_svg(index: int, row: Row) -> list[str]:
    top = TITLE_HEIGHT + 12 + index * LINE_HEIGHT
    base = top + 15
    if not row.typed:
        return [
            f'<text id="e{index}" x="{PADDING}" y="{base}" class="{row.style} a">'
            f"{escape(row.text)}</text>"
        ]
    text = f'<text x="{PADDING}" y="{base}" class="{row.style}"'
    length = _num(len(row.text) * CHAR_WIDTH)
    cover = (
        f'<rect id="e{index}" class="a m" x="{PADDING}" y="{top}" '
        f'width="{_num(len(row.text) * CHAR_WIDTH + 2)}" height="{LINE_HEIGHT}" '
        f'fill="{BACKGROUND}"/>'
    )
    return [f'{text} textLength="{length}" lengthAdjust="spacing">{escape(row.text)}</text>', cover]


def render(transcript: str) -> str:
    """Return the SVG for a transcript, byte for byte the same for the same text."""
    lines = [line.rstrip("\r") for line in transcript.split("\n") if line.strip()]
    if not lines:
        raise ValueError("the transcript is empty")
    rows, total = _schedule(lines)
    height = TITLE_HEIGHT + 12 + len(rows) * LINE_HEIGHT + 12
    body: list[str] = []
    for index, row in enumerate(rows):
        body.extend(_row_svg(index, row))
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" xml:space="preserve" role="img" aria-labelledby="t d">',
        f"<title id=\"t\">{escape(TITLE)}</title>",
        '<desc id="d">A terminal session: a restart request, the approval it waits for, '
        "the receipt, and the ledger answering the repeat.</desc>",
        f"<style>\n{_css(rows, total)}\n</style>",
        f'<rect width="{WIDTH}" height="{height}" rx="8" fill="{BACKGROUND}"/>',
        f'<path d="M0 {TITLE_HEIGHT}V8a8 8 0 0 1 8-8h{WIDTH - 16}a8 8 0 0 1 8 8'
        f'v{TITLE_HEIGHT - 8}z" fill="{BAR}"/>',
        *(f'<circle cx="{x}" cy="{TITLE_HEIGHT // 2}" r="6" fill="{c}"/>' for c, x in DOTS),
        f'<text x="{WIDTH // 2}" y="{TITLE_HEIGHT // 2 + 4}" text-anchor="middle" class="dim">'
        f"{escape(TITLE)}</text>",
    ]
    return "\n".join([*head, *body, "</svg>"]) + "\n"


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else DEFAULT_TRANSCRIPT
    target = Path(argv[2]) if len(argv) > 2 else DEFAULT_SVG
    target.write_text(render(source.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
