from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    bg: str = "#0a0e1a"
    bg_panel: str = "#11182b"
    bg_panel_2: str = "#161f38"
    bg_inset: str = "#0c1322"
    border: str = "#283358"
    border_strong: str = "#3a4a78"
    border_hot: str = "#22d3ee"

    primary: str = "#22d3ee"
    primary_dim: str = "#0e7490"
    secondary: str = "#fbbf24"
    accent: str = "#e879f9"
    success: str = "#34d399"
    warning: str = "#fb923c"
    danger: str = "#f87171"

    text: str = "#e6ebff"
    text_dim: str = "#8590b8"
    text_muted: str = "#5b6488"


PAL = Palette()


def ratio_color(ratio: float, *, invert: bool = False) -> str:
    r = 1 - ratio if invert else ratio
    if r > 0.5:
        return PAL.success
    if r > 0.25:
        return PAL.secondary
    return PAL.danger


NAV_STATUS = {
    "DOCKED": ("DOCK", PAL.primary, "--docked"),
    "IN_ORBIT": ("ORBIT", PAL.accent, "--orbit"),
    "IN_TRANSIT": ("TRANSIT", PAL.secondary, "--transit"),
}
