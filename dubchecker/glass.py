"""Drawing for the macOS-style look: rounded glass panels, capsule buttons, sidebar rows, a segmented
control and a search field.

Tk can't blur or see through widgets, so the glass is suggested rather than real: a soft drop shadow, a fill
a little apart from what's behind, and a bright rim along the top edge that fades down the sides, like light
catching the edge of a pane of glass. Tk's own canvas shapes have jagged corners, so shapes are drawn into
PhotoImages with smooth edges: a small template is worked out pixel by pixel once, then stretched to any size
by repeating its middle row and column (Tk's ``image copy -to`` tiles a region for us).
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from dubchecker.gui import App

RGB = tuple[float, float, float]


def _rgb(color: str) -> RGB:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _blend(a: RGB, b: RGB, t: float) -> RGB:
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t


def _hex(c: RGB) -> str:
    return "#%02x%02x%02x" % (round(c[0]), round(c[1]), round(c[2]))


def mix(a: str, b: str, t: float) -> str:
    """Colour ``a`` moved towards ``b`` by ``t`` (0 = a, 1 = b)."""
    return _hex(_blend(_rgb(a), _rgb(b), t))


def _clamp(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


@dataclass(frozen=True)
class Look:
    """How a rounded shape is drawn. Sizes are in screen pixels."""
    radius: int
    fill: str
    backdrop: str           # the colour behind the shape
    rim_top: str = ""       # the edge's colour at the top, fading to rim_bottom at the bottom ("" = no edge)
    rim_bottom: str = ""
    sheen: str = ""         # a soft highlight just inside the top edge ("" = none)
    shadow: float = 0.0     # drop shadow opacity
    blur: int = 0           # how far the shadow spreads
    drop: int = 0           # how far the shadow falls below the shape

    @property
    def margin(self) -> int:
        """Room around the shape for its shadow."""
        return self.blur + self.drop


def _images(master: tk.Misc) -> dict:
    """Templates already drawn, kept with the Tk root (images belong to one Tk interpreter)."""
    root = master._root()  # type: ignore[attr-defined]
    if not hasattr(root, "_glass_templates"):
        root._glass_templates = {}
    return root._glass_templates


def template(master: tk.Misc, look: Look) -> tk.PhotoImage:
    """The shape at its smallest: corners, plus a middle row and column to stretch."""
    cache = _images(master)
    if look in cache:
        return cache[look]
    m, r = look.margin, look.radius
    size = 2 * (m + r) + 1
    centre, half = size / 2, r + 0.5

    def distance(x: float, y: float) -> float:
        """Signed distance to the rounded rectangle's edge: negative inside."""
        qx, qy = abs(x - centre) - half + r, abs(y - centre) - half + r
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r

    back, fill = _rgb(look.backdrop), _rgb(look.fill)
    rim_top = _rgb(look.rim_top) if look.rim_top else None
    rim_bottom = _rgb(look.rim_bottom or look.rim_top) if look.rim_top else None
    sheen = _rgb(look.sheen) if look.sheen else None
    blur = max(1, look.blur)
    rows = []
    for j in range(size):
        y = j + 0.5
        down = _clamp((y - m) / (2 * r + 1))  # 0 at the top of the shape, 1 at the bottom
        down = down * down * (3 - 2 * down)
        rim = _blend(rim_top, rim_bottom, down) if rim_top and rim_bottom else fill
        top_light = (1 - down) ** 2
        row = []
        for i in range(size):
            x = i + 0.5
            colour = back
            if look.shadow:
                d = distance(x, y - look.drop)
                strength = look.shadow if d <= 0 else look.shadow * max(0.0, 1 - d / blur) ** 2
                colour = _blend(colour, (0, 0, 0), strength)
            d = distance(x, y)
            cover = _clamp(0.5 - d)
            if cover > 0:
                inside = _clamp(0.5 - (d + 1)) if rim_top else cover  # within the 1 px edge
                body = fill
                if sheen and inside > 0:
                    band = inside - _clamp(0.5 - (d + 2))
                    body = _blend(fill, sheen, band * top_light)
                colour = tuple(colour[k] * (1 - cover) + rim[k] * (cover - inside) + body[k] * inside
                               for k in range(3))  # type: ignore[assignment]
            row.append(_hex(colour))
        rows.append("{" + " ".join(row) + "}")
    image = tk.PhotoImage(master=master, width=size, height=size)
    image.put(" ".join(rows))
    cache[look] = image
    return image


def stretched(master: tk.Misc, look: Look, width: int, height: int) -> tk.PhotoImage:
    """The shape filling ``width`` x ``height`` (shadow included)."""
    source = template(master, look)
    m = look.margin + look.radius
    size = 2 * m + 1
    w, h = max(width, size), max(height, size)
    image = tk.PhotoImage(master=master, width=w, height=h)

    def copy(src: tuple[int, int, int, int], dst: tuple[int, int, int, int]) -> None:
        image.tk.call(image, "copy", source, "-from", *src, "-to", *dst)

    copy((0, 0, m, m), (0, 0, m, m))                                   # corners
    copy((m + 1, 0, size, m), (w - m, 0, w, m))
    copy((0, m + 1, m, size), (0, h - m, m, h))
    copy((m + 1, m + 1, size, size), (w - m, h - m, w, h))
    copy((m, 0, m + 1, m), (m, 0, w - m, m))                           # edges
    copy((m, m + 1, m + 1, size), (m, h - m, w - m, h))
    copy((0, m, m, m + 1), (0, m, m, h - m))
    copy((m + 1, m, size, m + 1), (w - m, m, w, h - m))
    copy((m, m, m + 1, m + 1), (m, m, w - m, h - m))                   # the middle
    return image


def dot(master: tk.Misc, color: str, backdrop: str, diameter: int) -> tk.PhotoImage:
    """A round coloured dot, e.g. a sidebar tab's symbol."""
    radius = max(1, (diameter - 1) // 2)
    return template(master, Look(radius, color, backdrop))


def magnifier(master: tk.Misc, color: str, backdrop: str, size: int) -> tk.PhotoImage:
    """The search field's magnifying glass."""
    cache = _images(master)
    key = ("magnifier", color, backdrop, size)
    if key in cache:
        return cache[key]
    stroke = max(1.2, size / 9)
    radius = size * 0.3
    cx = cy = size * 0.42
    reach = (cx + radius * 0.72, cy + radius * 0.72)
    end = (size * 0.92, size * 0.92)
    back, ink = _rgb(backdrop), _rgb(color)
    rows = []
    for j in range(size):
        row = []
        for i in range(size):
            x, y = i + 0.5, j + 0.5
            ring = abs(math.hypot(x - cx, y - cy) - radius) - stroke / 2
            # Distance to the handle: a line segment from the ring outwards.
            vx, vy = end[0] - reach[0], end[1] - reach[1]
            t = _clamp(((x - reach[0]) * vx + (y - reach[1]) * vy) / (vx * vx + vy * vy))
            handle = math.hypot(x - reach[0] - vx * t, y - reach[1] - vy * t) - stroke * 0.65
            row.append(_hex(_blend(back, ink, _clamp(0.5 - min(ring, handle)))))
        rows.append("{" + " ".join(row) + "}")
    image = tk.PhotoImage(master=master, width=size, height=size)
    image.put(" ".join(rows))
    cache[key] = image
    return image


# ---------------------------------------------------------------- the looks, by palette

def panel_look(app: App, kind: str, backdrop: str) -> Look:
    """The look of each kind of shape in the current palette. ``backdrop`` is a palette key."""
    pal, px = app.palette, app.px
    dark = pal["mode"] == "dark"
    behind = pal[backdrop]
    if kind == "sidebar":
        return Look(px(14), pal["card"], behind, pal["rim_top"], pal["rim_bottom"], pal["sheen"],
                    0.42 if dark else 0.075, px(14), px(3))
    if kind == "card":
        return Look(px(12), pal["card"], behind, pal["rim_top"], pal["rim_bottom"], pal["sheen"],
                    0.32 if dark else 0.05, px(8), px(2))
    if kind == "field":  # sunk in: darker along the top
        return Look(px(15), pal["field"], behind, pal["field_rim_top"], pal["field_rim_bottom"])
    raise ValueError(kind)


def button_look(app: App, accent: bool, backdrop: str, height: int, state: str) -> Look:
    """A capsule button; ``state`` is "", "hover", "pressed" or "disabled"."""
    pal = app.palette
    dark = pal["mode"] == "dark"
    radius = (height - 1) // 2
    if accent and state != "disabled":
        fill = {"hover": pal["accent_hover"], "pressed": pal["accent_pressed"]}.get(state, pal["accent"])
        return Look(radius, fill, pal[backdrop], mix(fill, "#ffffff", 0.35), mix(fill, "#000000", 0.2), "",
                    0.35 if dark else 0.14, app.px(3), app.px(1))
    fill = {"hover": pal["glass_hover"], "pressed": pal["glass_pressed"]}.get(state, pal["glass"])
    shadow = 0.0 if state == "disabled" else 0.3 if dark else 0.09
    return Look(radius, fill, pal[backdrop], pal["rim_top"], pal["rim_bottom"], pal["sheen"],
                shadow, app.px(3), app.px(1))


# ---------------------------------------------------------------- widgets

class GlassPanel(tk.Canvas):
    """A rounded glass panel. Put its contents in ``body`` (a frame in the panel's colour); the panel
    fits them unless it's given a fixed size with ``pack_propagate(False)``."""

    def __init__(self, parent: tk.Misc, app: App, kind: str, backdrop: str = "bg", padding: int = 12) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0)
        self.app, self.kind, self.backdrop = app, kind, backdrop
        margin = self.look().margin
        self.body = tk.Frame(self, borderwidth=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=margin + app.px(padding),
                       pady=(max(0, margin - app.px(2)) + app.px(padding), margin + app.px(padding)))
        self._image: tk.PhotoImage | None = None
        self._item = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height), add="+")
        app.surfaces.append(self)

    def look(self) -> Look:
        return panel_look(self.app, self.kind, self.backdrop)

    def recolor(self) -> None:
        pal = self.app.palette
        self.configure(background=pal[self.backdrop])
        self.body.configure(background=pal["card"])
        self._draw(self.winfo_width(), self.winfo_height())

    def _draw(self, width: int, height: int) -> None:
        if width > 1 and height > 1:
            self._image = stretched(self, self.look(), width, height)
            self.itemconfigure(self._item, image=self._image)


class _Clickable(tk.Canvas):
    """Hover, press and release handling for canvas-drawn controls, and ttk-like ``state()``."""

    def __init__(self, parent: tk.Misc, app: App, **options: Any) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0, cursor="hand2", takefocus=0, **options)
        self.app = app
        self.disabled = False
        self.hover = False
        self.pressed = False
        self.bind("<Enter>", lambda _e: self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        app.surfaces.append(self)

    def _set(self, **flags: bool) -> None:
        for name, value in flags.items():
            setattr(self, name, value)
        self.redraw()

    def _on_press(self, event: Any) -> None:
        if not self.disabled:
            self._set(pressed=True)

    def _on_release(self, event: Any) -> None:
        was = self.pressed
        self._set(pressed=False)
        if was and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.clicked(event)

    def clicked(self, event: Any) -> None:
        raise NotImplementedError

    def redraw(self) -> None:
        raise NotImplementedError

    def recolor(self) -> None:
        self.redraw()

    def state(self, spec: tuple[str, ...] | list[str] | None = None) -> tuple[str, ...]:
        """Like ttk's: ``("disabled",)`` or ``("!disabled",)`` to change it; returns the current state."""
        if spec is not None:
            for flag in spec:
                if flag in ("disabled", "!disabled"):
                    self.disabled = flag == "disabled"
            self.configure(cursor="arrow" if self.disabled else "hand2")
            self.redraw()
        return ("disabled",) if self.disabled else ()

    def instate(self, spec: list[str]) -> bool:
        return all((flag == "disabled") == self.disabled for flag in spec if flag in ("disabled", "!disabled"))


class PillButton(_Clickable):
    """A capsule-shaped button. ``accent`` is the blue one for the main action."""

    def __init__(self, parent: tk.Misc, app: App, text: str, command: Callable[[], Any] | None = None,
                 accent: bool = False, backdrop: str = "bg", small: bool = False, width: int = 0) -> None:
        self.text, self.command, self.accent, self.backdrop = text, command, accent, backdrop
        self.font = "SunValleyCaptionFont" if small else "SunValleyBodyFont"
        self.height = app.px(24 if small else 30) | 1  # odd, so the ends are exact half circles
        self.min_width = width
        super().__init__(parent, app)
        self._image: tk.PhotoImage | None = None
        self._shape = self.create_image(0, 0, anchor="nw")
        self._label = self.create_text(0, 0, text=text, font=self.font)
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        self._resize()

    def _look(self) -> Look:
        state = "disabled" if self.disabled else "pressed" if self.pressed else "hover" if self.hover else ""
        return button_look(self.app, self.accent, self.backdrop, self.height, state)

    def _resize(self) -> None:
        margin = self._look().margin
        width = max(self.min_width, tkfont.nametofont(self.font).measure(self.text) + 2 * self.app.px(14))
        super().configure(width=width + 2 * margin, height=self.height + 2 * margin)
        self.redraw()

    def redraw(self) -> None:
        pal = self.app.palette
        look = self._look()
        width = max(self.winfo_width(), int(self.cget("width")))
        height = self.height + 2 * look.margin
        super().configure(background=pal[self.backdrop])
        self._image = stretched(self, look, width, height)
        self.itemconfigure(self._shape, image=self._image)
        color = pal["muted"] if self.disabled else "#ffffff" if self.accent else pal["fg"]
        self.coords(self._label, width / 2, look.margin + self.height / 2)
        self.itemconfigure(self._label, text=self.text, fill=color)

    def clicked(self, event: Any) -> None:
        self.invoke()

    def invoke(self) -> Any:
        if not self.disabled and self.command is not None:
            return self.command()
        return None

    def cget(self, key: str) -> Any:
        return self.text if key == "text" else super().cget(key)

    def configure(self, cnf: dict | None = None, **options: Any) -> Any:  # type: ignore[override]
        if "text" in options:
            self.text = str(options.pop("text"))
            self._resize()
        if "command" in options:
            self.command = options.pop("command")
        if cnf or options:
            return super().configure(cnf, **options)
        return None

    config = configure  # type: ignore[assignment]


class TextItem:
    """A canvas text item that answers ``cget("text")`` like a label does."""

    def __init__(self, canvas: tk.Canvas, item: int) -> None:
        self.canvas, self.item = canvas, item

    def cget(self, key: str) -> Any:
        return self.canvas.itemcget(self.item, key)


class Segment:
    """One choice of a SegmentedControl, with the bits of a ttk.Radiobutton's interface the app uses."""

    def __init__(self, control: SegmentedControl, index: int) -> None:
        self.control, self.index = control, index

    def cget(self, key: str) -> Any:
        if key == "text":
            return self.control.options[self.index][1]
        if key == "value":
            return self.control.options[self.index][0]
        raise KeyError(key)

    def state(self, spec: tuple[str, ...] | None = None) -> tuple[str, ...]:
        return self.control.state(spec)

    def invoke(self) -> None:
        self.control.choose(self.index)


class SegmentedControl(_Clickable):
    """macOS-style segmented control: a sunken track with a raised thumb under the chosen option."""

    def __init__(self, parent: tk.Misc, app: App, variable: tk.StringVar, options: list[tuple[str, str]],
                 command: Callable[[], Any] | None = None, backdrop: str = "card") -> None:
        self.variable, self.options, self.command, self.backdrop = variable, options, command, backdrop
        self.height = app.px(28) | 1
        super().__init__(parent, app, height=self.height, width=1)  # as wide as it is given
        self.segments = [Segment(self, i) for i in range(len(options))]
        self._images: list[tk.PhotoImage] = []
        self._track = self.create_image(0, 0, anchor="nw")
        self._thumb = self.create_image(0, 0, anchor="nw")
        self._labels = [self.create_text(0, 0, text=label, font="SunValleyBodyFont") for _, label in options]
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        variable.trace_add("write", lambda *_: self.redraw())

    def chosen(self) -> int:
        values = [value for value, _ in self.options]
        return values.index(self.variable.get()) if self.variable.get() in values else 0

    def choose(self, index: int) -> None:
        if self.disabled:
            return
        self.variable.set(self.options[index][0])
        if self.command is not None:
            self.command()

    def clicked(self, event: Any) -> None:
        width = max(1, self.winfo_width())
        self.choose(min(len(self.options) - 1, int(event.x * len(self.options) / width)))

    def redraw(self) -> None:
        pal, px = self.app.palette, self.app.px
        dark = pal["mode"] == "dark"
        width = max(self.winfo_width(), px(160))
        super().configure(background=pal[self.backdrop])
        track = Look((self.height - 1) // 2, pal["track"], pal[self.backdrop], pal["field_rim_top"],
                     pal["field_rim_bottom"])
        inset = px(2)
        thumb_height = (self.height - 2 * inset) | 1
        if thumb_height > self.height - 2 * inset:
            thumb_height -= 2
        # No shadow: its margin would paint over the track's rounded ends. The darker lower edge lifts it.
        thumb = Look((thumb_height - 1) // 2, pal["thumb"], pal["track"], pal["rim_top"],
                     mix(pal["rim_bottom"], "#000000", 0.25 if dark else 0.08))
        segment = width / len(self.options)
        index = self.chosen()
        track_image = stretched(self, track, width, self.height)
        thumb_image = stretched(self, thumb, round(segment) - 2 * inset + 2 * thumb.margin,
                                thumb_height + 2 * thumb.margin)
        self._images = [track_image, thumb_image]
        self.itemconfigure(self._track, image=track_image)
        self.coords(self._thumb, round(index * segment) + inset - thumb.margin,
                    (self.height - thumb_height) // 2 - thumb.margin)
        self.itemconfigure(self._thumb, image=thumb_image)
        for i, label in enumerate(self._labels):
            self.coords(label, (i + 0.5) * segment, self.height / 2)
            color = pal["muted"] if self.disabled or i != index else pal["fg"]
            self.itemconfigure(label, fill=color, font="SunValleyBodyStrongFont" if i == index else
                               "SunValleyBodyFont")


class SearchField(tk.Canvas):
    """A rounded search box with a magnifying glass and a "Search" placeholder. ``entry`` is the text box;
    it's hidden behind the placeholder while it's empty and not in use (Tk draws embedded widgets over
    everything else on a canvas)."""

    def __init__(self, parent: tk.Misc, app: App, variable: tk.StringVar, width: int,
                 backdrop: str = "bg", placeholder: str = "Search") -> None:
        self.app, self.backdrop = app, backdrop
        self.height = app.px(30) | 1
        super().__init__(parent, highlightthickness=0, borderwidth=0, width=width, height=self.height)
        self.variable = variable
        self._image: tk.PhotoImage | None = None
        self._icon: tk.PhotoImage | None = None
        self._shape = self.create_image(0, 0, anchor="nw")
        self._glass = self.create_image(app.px(11), self.height // 2, anchor="w")
        self._placeholder = self.create_text(app.px(31), self.height // 2, text=placeholder, anchor="w",
                                             font="SunValleyBodyFont")
        self.entry = tk.Entry(self, textvariable=variable, relief="flat", borderwidth=0, highlightthickness=0,
                              font="SunValleyBodyFont")
        self._window = self.create_window(app.px(30), self.height // 2, window=self.entry, anchor="w")
        self.entry.bind("<FocusIn>", lambda _e: self._update_placeholder(), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self._update_placeholder(), add="+")
        variable.trace_add("write", lambda *_: self._update_placeholder())
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        self.bind("<Button-1>", lambda _e: self.focus_entry())
        app.surfaces.append(self)

    def focus_entry(self) -> None:
        self.itemconfigure(self._window, state="normal")
        self.itemconfigure(self._placeholder, state="hidden")
        self.entry.focus_set()

    def _update_placeholder(self) -> None:
        try:
            empty = not self.variable.get() and self.focus_get() is not self.entry
        except (KeyError, tk.TclError):  # focus_get can fail while a menu or dialog has the focus
            empty = not self.variable.get()
        self.itemconfigure(self._placeholder, state="normal" if empty else "hidden")
        self.itemconfigure(self._window, state="hidden" if empty else "normal")

    def redraw(self) -> None:
        pal, px = self.app.palette, self.app.px
        width = max(self.winfo_width(), int(self.cget("width")))
        look = panel_look(self.app, "field", self.backdrop)
        look = Look((self.height - 1) // 2, look.fill, look.backdrop, look.rim_top, look.rim_bottom)
        super().configure(background=pal[self.backdrop])
        self._image = stretched(self, look, width, self.height)
        self.itemconfigure(self._shape, image=self._image)
        self._icon = magnifier(self, pal["muted"], pal["field"], px(14))
        self.itemconfigure(self._glass, image=self._icon)
        self.itemconfigure(self._placeholder, fill=pal["muted"])
        self.itemconfigure(self._window, width=max(px(40), width - px(30) - px(14)))
        self.entry.configure(background=pal["field"], foreground=pal["fg"], insertbackground=pal["fg"],
                             selectbackground=pal["accent"], selectforeground="#ffffff")
        self._update_placeholder()

    def recolor(self) -> None:
        self.redraw()
