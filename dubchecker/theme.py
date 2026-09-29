"""Light/dark theming on top of sv-ttk (Sun Valley).

Pitfalls handled here:
* sv-ttk's light theme doesn't reliably set the global background, so Tk's grey
  shows through - we configure the "." style ourselves.
* sv-ttk fonts are pixel-sized, so they are scaled once for the screen's DPI.
* Widgets on our custom card panels need ``Card.*`` style variants with the card
  background; the panels themselves are plain tk.Frames, not sv-ttk's Card.TFrame.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import sv_ttk

log = logging.getLogger(__name__)

PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#fafafa", "card": "#ffffff", "card_hover": "#f4f4f5", "card_selected": "#f0f5fd",
        "border": "#e2e2e4", "fg": "#1c1c1c", "muted": "#5f6368", "link": "#005fb8",
        "row_even": "#fafafa", "row_odd": "#f1f1f4", "row_hover": "#e3ecf9", "on_color": "#ffffff",
        "red": "#c42b1c", "grey": "#6b6f76", "green": "#0f7b0f", "amber": "#9d5d00", "blue": "#0063b1", "purple": "#6b3fd4",
    },
    "dark": {
        "bg": "#1c1c1c", "card": "#262626", "card_hover": "#2e2e2e", "card_selected": "#2b3440",
        "border": "#3a3a3a", "fg": "#fafafa", "muted": "#a8a8a8", "link": "#79c4ff",
        "row_even": "#1c1c1c", "row_odd": "#262626", "row_hover": "#2d3a4b", "on_color": "#1c1c1c",
        "red": "#ff8f8f", "grey": "#b3b3b3", "green": "#6ccb5f", "amber": "#f5c451", "blue": "#60b0ff", "purple": "#b99cff",
    },
}

CARD_STYLES = ("Card.TLabel", "Card.TButton", "Card.Accent.TButton", "Card.TEntry", "Card.TRadiobutton",
               "Card.TCheckbutton", "Card.Horizontal.TProgressbar")


def windows_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve_mode(setting: str) -> str:
    if setting in ("light", "dark"):
        return setting
    return "dark" if windows_prefers_dark() else "light"


def dpi_scale(widget: tk.Misc) -> float:
    return max(1.0, widget.winfo_fpixels("1i") / 96.0)


def _scale_fonts(root: tk.Tk) -> None:
    if getattr(root, "_dc_fonts_scaled", False):
        return
    scale = dpi_scale(root)
    for name in tkfont.names(root):
        if name.startswith("SunValley"):
            font = tkfont.nametofont(name, root=root)
            size = int(font.cget("size"))
            if size < 0:  # pixel size
                font.configure(size=-round(abs(size) * scale))
    root._dc_fonts_scaled = True  # type: ignore[attr-defined]


def set_title_bar(window: tk.Misc, dark: bool) -> None:
    """Dark or light Windows title bar (DWMWA_USE_IMMERSIVE_DARK_MODE is 20, or 19 on older builds)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        if window.winfo_viewable():  # nudge Windows to repaint the title bar
            window.attributes("-alpha", 0.99)
            window.attributes("-alpha", 1.0)
    except Exception as exc:
        log.debug("Couldn't set the title bar colour: %s", exc)


def apply_theme(root: tk.Tk, setting: str) -> dict[str, str]:
    """Switch sv-ttk to light or dark and configure our own styles. Returns the palette."""
    mode = resolve_mode(setting)
    sv_ttk.set_theme(mode, root)
    _scale_fonts(root)
    palette = dict(PALETTES[mode], mode=mode)
    scale = dpi_scale(root)

    style = ttk.Style(root)
    style.configure(".", background=palette["bg"], foreground=palette["fg"])
    for name in CARD_STYLES:
        style.configure(name, background=palette["card"])
    style.configure("Card.TLabel", foreground=palette["fg"])
    style.configure("Muted.TLabel", foreground=palette["muted"])
    style.configure("Card.Muted.TLabel", background=palette["card"], foreground=palette["muted"])
    style.configure("Title.TLabel", font="SunValleyTitleFont")
    style.configure("Subtitle.TLabel", font="SunValleySubtitleFont")
    style.configure("Strong.TLabel", font="SunValleyBodyStrongFont")
    style.configure("Caption.TLabel", font="SunValleyCaptionFont", foreground=palette["muted"])
    style.configure("Good.TLabel", foreground=palette["green"])
    style.configure("Bad.TLabel", foreground=palette["red"])

    body = tkfont.nametofont("SunValleyBodyFont", root=root)
    style.configure("Treeview", background=palette["bg"], fieldbackground=palette["bg"],
                    foreground=palette["fg"], rowheight=body.metrics("linespace") + round(10 * scale))
    style.configure("Heading", padding=(round(6 * scale), round(4 * scale)))
    # The results table shows audio bars in its first column. Its items get no expand indicator
    # (sv-ttk's is broken on Tk 9; we draw our own arrows) and no indent, so the bars line up.
    style.layout("Results.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.image", {"side": "left", "sticky": ""}),
        ("Treeitem.focus", {"side": "left", "sticky": "", "children": [
            ("Treeitem.text", {"side": "left", "sticky": ""})]})]})])
    style.configure("Results.Treeview", indent=0)
    root.configure(background=palette["bg"])
    set_title_bar(root, mode == "dark")
    return palette
