from __future__ import annotations

from rich.text import Text

from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static

from .theme import PAL, NAV_STATUS, ratio_color

NAV_LABELS = [
    ("agent", "1", "Agent", "◈"),
    ("fleet", "2", "Fleet", "⊳"),
    ("contracts", "3", "Contracts", "§"),
    ("markets", "4", "Markets", "$"),
    ("automation", "5", "Automate", "⚙"),
]


class Panel(Container):
    def __init__(self, title: str = "", *children, subtitle: str = "", classes: str = "", **kw):
        merged = ("panel " + classes).strip()
        super().__init__(*children, classes=merged, **kw)
        if title:
            self.border_title = title
        if subtitle:
            self.border_subtitle = subtitle


class Pill(Static):
    def __init__(self, text: str, variant: str = "idle", **kw):
        super().__init__(text, classes=f"pill --{variant}", **kw)
        self._variant = variant

    def set(self, text: str, variant: str = "idle") -> None:
        self.update(text)
        self.remove_class(f"--{self._variant}")
        self._variant = variant
        self.add_class(f"--{variant}")


class Gauge(Widget, can_focus=False):
    def __init__(
        self,
        label: str,
        value: float = 0.0,
        maximum: float = 100.0,
        color: str | None = None,
        bar_width: int = 16,
        **kw,
    ):
        super().__init__(classes="gauge", **kw)
        self.label = label
        self.value = float(value)
        self.maximum = float(maximum)
        self.color = color
        self.bar_width = bar_width

    def set(self, value: float, maximum: float | None = None) -> None:
        self.value = float(value)
        if maximum is not None:
            self.maximum = float(maximum)
        self.refresh()

    def render(self) -> Text:
        cap = max(self.maximum, 1.0)
        ratio = max(0.0, min(1.0, self.value / cap))
        col = self.color or ratio_color(ratio)
        filled = int(round(ratio * self.bar_width))
        t = Text()
        t.append(f"{self.label:<6}", style=PAL.text_muted)
        t.append("█" * filled, style=col)
        t.append("░" * (self.bar_width - filled), style=PAL.border)
        t.append(f" {int(self.value):>4}/{int(self.maximum):<4}", style=PAL.text)
        return t


class Stat(Container):
    def __init__(self, label: str, value: str = "", sub: str = "", accent: str = "", **kw):
        super().__init__(classes=("stat " + accent).strip(), **kw)
        self._label = label
        self._value = value
        self._sub = sub

    def compose(self):
        yield Static(self._label, classes="stat-label")
        self.value_w = Static(self._value, classes="stat-value")
        yield self.value_w
        if self._sub:
            yield Static(self._sub, classes="stat-sub")

    def set_value(self, v) -> None:
        self.value_w.update(str(v))


def _ship_meta(ship: dict) -> Text:
    nav = ship.get("nav", {})
    cd = ship.get("cooldown") or {}
    cargo = ship.get("cargo", {})
    inv = cargo.get("inventory", [])
    t = Text()
    wrote = False
    if nav.get("status") == "IN_TRANSIT":
        dest = nav.get("route", {}).get("destination", {}).get("symbol", "")
        secs = _eta_secs(nav.get("route", {}).get("arrival"))
        t.append(f"→ {dest}", style=PAL.secondary)
        if secs:
            t.append(f" ({secs}s)", style=PAL.text_muted)
        wrote = True
    if cd.get("remainingSeconds"):
        if wrote:
            t.append("   ")
        t.append(f"⚆ cd {cd['remainingSeconds']}s", style=PAL.warning)
        wrote = True
    if inv:
        if wrote:
            t.append("   ")
        parts = [f"{i['units']} {i['symbol'].replace('_', ' ').title()}" for i in inv[:3]]
        more = f" +{len(inv) - 3}" if len(inv) > 3 else ""
        t.append(" · ".join(parts) + more, style=PAL.text_dim)
        wrote = True
    if not wrote:
        t.append("ready", style=PAL.text_muted)
    return t


def _eta_secs(ts: str | None) -> int:
    import datetime as dt

    if not ts:
        return 0
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0, int((t - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except ValueError:
        return 0


class ShipCard(Container):
    class Selected(Message):
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            super().__init__()

    def __init__(self, ship: dict, **kw):
        super().__init__(classes="ship-card", **kw)
        self.symbol = ship["symbol"]

    def compose(self):
        with Horizontal(classes="ship-head"):
            yield Static(self.symbol, classes="ship-name")
            self.pill = Pill("—", "idle")
            yield self.pill
        self.frame_w = Static("", classes="ship-frame")
        yield self.frame_w
        self.fuel_g = Gauge("FUEL", 0, 100)
        yield self.fuel_g
        self.cargo_g = Gauge("CARGO", 0, 100, color=PAL.primary)
        yield self.cargo_g
        self.meta_w = Static("", classes="meta-line")
        yield self.meta_w

    def on_click(self, event) -> None:
        self.post_message(self.Selected(self.symbol))

    def update(self, ship: dict) -> None:
        nav = ship.get("nav", {})
        frame = ship.get("frame", {})
        reg = ship.get("registration", {})
        status = nav.get("status", "")
        label, _col, variant = NAV_STATUS.get(status, (status, PAL.text_muted, "idle"))
        dest = ""
        if status == "IN_TRANSIT":
            dest = "→" + nav.get("route", {}).get("destination", {}).get("symbol", "")
        self.pill.set(f"{label}{dest}", variant)
        self.frame_w.update(
            Text.assemble(
                (frame.get("name", "?"), PAL.text_dim),
                ("  ·  ", PAL.text_muted),
                (reg.get("role", "?").title(), PAL.accent),
            )
        )
        fuel = ship.get("fuel", {})
        self.fuel_g.set(fuel.get("current", 0), fuel.get("capacity", 1))
        cargo = ship.get("cargo", {})
        self.cargo_g.set(cargo.get("units", 0), cargo.get("capacity", 1))
        self.meta_w.update(_ship_meta(ship))

    def select(self, on: bool) -> None:
        self.set_class(on, "--selected")


class ContractCard(Container):
    def __init__(self, contract: dict, **kw):
        super().__init__(classes="contract-card", **kw)
        self.cid = contract["id"]
        self._delivers = contract.get("terms", {}).get("deliver", [])

    def compose(self):
        with Horizontal(classes="contract-head"):
            self.faction_w = Static("", classes="contract-faction")
            yield self.faction_w
            self.type_w = Static("", classes="contract-type")
            yield self.type_w
        self.pay_w = Static("", classes="pay")
        yield self.pay_w
        self._deliver_widgets = []
        for d in self._delivers:
            line = Static("", classes="deliver")
            g = Gauge(
                d["tradeSymbol"].replace("_ORE", "").replace("_", " "),
                0,
                d["unitsRequired"],
                color=PAL.secondary,
                bar_width=26,
            )
            self._deliver_widgets.append((line, g, d))
            yield line
            yield g

    def update(self, c: dict) -> None:
        if not hasattr(self, "faction_w"):
            return
        self.border_title = c.get("id", "")[:18]
        self.faction_w.update(Text.assemble((c.get("factionSymbol", ""), PAL.accent)))
        self.type_w.update(
            Text.assemble(
                (c.get("type", ""), PAL.text_dim),
                ("  ", PAL.text_muted),
                (
                    "✓ DONE"
                    if c.get("fulfilled")
                    else ("ACCEPTED" if c.get("accepted") else "PENDING"),
                    PAL.success if c.get("fulfilled") else PAL.secondary,
                ),
            )
        )
        terms = c.get("terms", {})
        pay = terms.get("payment", {})
        self.pay_w.update(
            Text.assemble(
                ("pay  ", PAL.text_muted),
                ("accept ", PAL.text_muted),
                (f"{pay.get('onAccepted', 0):,}c", PAL.secondary),
                ("   fulfill ", PAL.text_muted),
                (f"{pay.get('onFulfilled', 0):,}c", PAL.success),
            )
        )
        for line, g, d in self._deliver_widgets:
            cur = d["unitsFulfilled"]
            req = d["unitsRequired"]
            for dd in c.get("terms", {}).get("deliver", []):
                if dd["tradeSymbol"] == d["tradeSymbol"] and dd["destinationSymbol"] == d["destinationSymbol"]:
                    cur = dd["unitsFulfilled"]
                    req = dd["unitsRequired"]
                    break
            line.update(
                Text.assemble(
                    (f"{cur:>4}/{req:<4} ", PAL.text),
                    (d["tradeSymbol"], PAL.primary),
                    (" → ", PAL.text_muted),
                    (d["destinationSymbol"], PAL.text_dim),
                )
            )
            g.set(cur, req)


class BotRow(Container):
    class Toggle(Message):
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            super().__init__()

    def __init__(self, ship: dict, **kw):
        super().__init__(classes="bot-row", **kw)
        self.symbol = ship["symbol"]

    def compose(self):
        with Horizontal(classes="bot-head"):
            yield Static(self.symbol, classes="bot-name")
            self.status = Pill("IDLE", "idle")
            yield self.status
        self.mode_w = Static("", classes="bot-mode")
        yield self.mode_w
        self.last_w = Static("idle", classes="bot-last")
        yield self.last_w
        with Horizontal(classes="bot-controls"):
            yield Button("Start", id=f"bot-start-{self.symbol}", classes="btn --primary")
            yield Button("Stop", id=f"bot-stop-{self.symbol}", classes="btn --danger", disabled=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and (
            event.button.id.startswith("bot-start-") or event.button.id.startswith("bot-stop-")
        ):
            self.post_message(self.Toggle(self.symbol))

    def set_state(self, running: bool, last: str = "", mode: str = "") -> None:
        self.set_class(running, "--running")
        self.status.set("RUNNING" if running else "IDLE", "running" if running else "idle")
        try:
            self.query_one("#bot-start-" + self.symbol, Button).disabled = running
            self.query_one("#bot-stop-" + self.symbol, Button).disabled = not running
        except Exception:
            pass
        if last:
            self.last_w.update(last[:60])
        if mode:
            self.mode_w.update(mode)
