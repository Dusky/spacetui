import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui.charts import block_sparkline, braille_chart, hbar


def test_block_sparkline_levels():
    s = block_sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert len(s) == 8
    assert s[0] == "▁"    # lowest maps to the bottom block
    assert s[-1] == "█"   # highest maps to the full block


def test_block_sparkline_flat_and_empty():
    assert block_sparkline([]) == ""
    flat = block_sparkline([5, 5, 5])
    assert len(flat) == 3 and set(flat) <= set(" ▁▂▃▄▅▆▇█")


def test_block_sparkline_resamples_to_width():
    assert len(block_sparkline(list(range(100)), width=20)) == 20


def test_braille_chart_dimensions_and_glyphs():
    lines = braille_chart([1, 2, 3, 4, 5], width_cells=10, height_cells=4)
    assert len(lines) == 4
    assert all(len(ln) == 10 for ln in lines)
    # every glyph is in the braille block (U+2800..U+28FF)
    assert all(0x2800 <= ord(ch) <= 0x28FF for ln in lines for ch in ln)


def test_braille_chart_empty():
    lines = braille_chart([], width_cells=10, height_cells=3)
    assert len(lines) == 3


def test_braille_rising_series_puts_ink_bottom_left_top_right():
    # a monotonically rising line: left column low, right column high
    lines = braille_chart(list(range(1, 33)), width_cells=16, height_cells=4)
    blank = chr(0x2800)
    assert lines[-1][0] != blank    # bottom row has ink on the left
    assert lines[0][-1] != blank    # top row has ink on the right


def test_hbar():
    assert hbar(5, 10, 20) == "█" * 10
    assert hbar(0, 10, 20) == ""
    assert hbar(10, 0, 20) == ""    # zero peak -> empty, no crash
    assert len(hbar(999, 10, 20)) == 20  # clamped to width
