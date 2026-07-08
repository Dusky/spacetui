"""Terminal charting for the analytics pane.

Braille gives us 2x4 sub-cell resolution — eight dots per character — so a line
chart drawn with braille is far crisper than one made of block glyphs. The
rendering functions here are pure (list[float] -> list[str]) so they can be
unit-tested; `BrailleChart` wraps one in a themed Textual widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .theme import PAL

# Braille dot bit layout within a cell (col, row_from_top) -> bit.
_DOT_BITS = {
    (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
    (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
}
_BRAILLE_BASE = 0x2800
_BLOCKS = "▁▂▃▄▅▆▇█"


def block_sparkline(values, width: int = 0) -> str:
    """A compact one-line sparkline using block glyphs."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    if width and len(vals) > width:
        vals = _resample(vals, width)
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    out = []
    for v in vals:
        level = int(round((v - lo) / span * (len(_BLOCKS) - 1)))
        out.append(_BLOCKS[level])
    return "".join(out)


def _resample(values, n: int) -> list[float]:
    """Nearest-neighbour resample to exactly ``n`` points."""
    if n <= 0 or not values:
        return []
    if len(values) == 1:
        return [values[0]] * n
    m = len(values)
    return [values[min(int(round(i * (m - 1) / (n - 1))), m - 1)] for i in range(n)]


def braille_chart(values, width_cells: int = 48, height_cells: int = 8) -> list[str]:
    """Render a connected line chart as ``height_cells`` braille strings."""
    vals = [float(v) for v in values if v is not None]
    if not vals or width_cells <= 0 or height_cells <= 0:
        return [""] * max(1, height_cells)
    dot_w, dot_h = width_cells * 2, height_cells * 4
    cols = _resample(vals, dot_w) if len(vals) != dot_w else vals
    lo, hi = min(cols), max(cols)
    span = (hi - lo) or 1.0

    grid = [[0] * width_cells for _ in range(height_cells)]

    def _set(dx: int, dy_from_bottom: int) -> None:
        dy_from_bottom = max(0, min(dot_h - 1, dy_from_bottom))
        row_from_top = (dot_h - 1) - dy_from_bottom
        cell_col, sub_col = divmod(dx, 2)
        cell_row, sub_row = divmod(row_from_top, 4)
        grid[cell_row][cell_col] |= _DOT_BITS[(sub_col, sub_row)]

    prev_dy = None
    for dx, v in enumerate(cols):
        dy = int(round((v - lo) / span * (dot_h - 1)))
        if prev_dy is None:
            _set(dx, dy)
        else:  # connect to the previous point so it reads as a line
            for yy in range(min(prev_dy, dy), max(prev_dy, dy) + 1):
                _set(dx, yy)
        prev_dy = dy

    return ["".join(chr(_BRAILLE_BASE + grid[r][c]) for c in range(width_cells))
            for r in range(height_cells)]


def hbar(value: float, peak: float, width: int = 20) -> str:
    """A horizontal bar sized to ``value`` relative to ``peak`` (both may be 0)."""
    if peak <= 0:
        return ""
    filled = int(round(abs(value) / peak * width))
    return "█" * max(0, min(width, filled))


class BrailleChart(Widget, can_focus=False):
    """A themed braille line chart that adapts to its width."""

    DEFAULT_CSS = "BrailleChart { height: auto; min-height: 6; }"

    def __init__(self, *, height_cells: int = 7, unit: str = "", **kw):
        super().__init__(**kw)
        self.values: list[float] = []
        self.height_cells = height_cells
        self.unit = unit

    def set_data(self, values) -> None:
        self.values = [float(v) for v in values if v is not None]
        self.refresh()

    def _fmt(self, v: float) -> str:
        return f"{int(v):,}{self.unit}"

    def render(self) -> Text:
        width = max(12, (self.content_size.width or 48) - 10)
        if not self.values:
            t = Text("no data yet", style=PAL.text_muted)
            return t
        lo, hi = min(self.values), max(self.values)
        first, last = self.values[0], self.values[-1]
        rising = last >= first
        col = PAL.success if rising else PAL.danger
        lines = braille_chart(self.values, width_cells=width, height_cells=self.height_cells)

        out = Text()
        out.append(f"{self._fmt(hi):>{width + 8}}\n", style=PAL.text_dim)
        for i, ln in enumerate(lines):
            out.append(f"  {ln}", style=col)
            if i == 0:
                arrow = "▲" if rising else "▼"
                delta = last - first
                out.append(f"   {arrow} {self._fmt(delta)}", style=col)
            elif i == len(lines) - 1:
                out.append(f"   now {self._fmt(last)}", style=PAL.text)
            out.append("\n")
        out.append(f"{self._fmt(lo):>{width + 8}}", style=PAL.text_dim)
        return out
