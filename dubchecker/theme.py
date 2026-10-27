"""Light/dark theming on top of sv-ttk (Sun Valley), with a macOS-style palette: Apple's system colours,
glass panels (see glass.py) and a title bar that blends into the window on Windows 11.

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

# bg: the window; card: glass panels (the sidebar, the activity panel); card_hover / card_selected: sidebar
# rows; glass*: capsule buttons; rim_*/sheen: the bright edge of glass; field*/track/thumb: sunken controls.
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#fbfbfd", "card": "#f1f1f4", "card_hover": "#e6e6eb", "card_selected": "#dcdce2",
        "border": "#d2d2d7", "fg": "#1d1d1f", "muted": "#6e6e73", "link": "#0066cc",
        "row_even": "#fbfbfd", "row_odd": "#f3f3f6", "row_hover": "#e8eefa", "row_selected": "#d3e3fd",
        "on_color": "#ffffff", "accent": "#007aff", "accent_hover": "#1f8bff", "accent_pressed": "#006ae6",
        "glass": "#fdfdfe", "glass_hover": "#f4f4f7", "glass_pressed": "#e9e9ee",
        "rim_top": "#ffffff", "rim_bottom": "#d4d4da", "sheen": "#ffffff",
        "field": "#ececf0", "field_rim_top": "#d6d6dc", "field_rim_bottom": "#f6f6f8",
        "track": "#e3e3e8", "thumb": "#ffffff",
        "red": "#d70015", "grey": "#8e8e93", "green": "#248a3d", "amber": "#c45f00", "blue": "#007aff",
        "purple": "#8944ab", "teal": "#0a7f99",
    },
    "dark": {
        "bg": "#1c1c1e", "card": "#2a2a2d", "card_hover": "#343437", "card_selected": "#3c3c40",
        "border": "#3a3a3c", "fg": "#f5f5f7", "muted": "#98989d", "link": "#2997ff",
        "row_even": "#1c1c1e", "row_odd": "#222225", "row_hover": "#29303d", "row_selected": "#203a5c",
        "on_color": "#1c1c1e", "accent": "#0a84ff", "accent_hover": "#3096ff", "accent_pressed": "#0070e0",
        "glass": "#3a3a3d", "glass_hover": "#444448", "glass_pressed": "#2f2f32",
        "rim_top": "#616166", "rim_bottom": "#2c2c2f", "sheen": "#4c4c50",
        "field": "#2c2c2e", "field_rim_top": "#18181a", "field_rim_bottom": "#3a3a3d",
        "track": "#1f1f22", "thumb": "#55555a",
        "red": "#ff453a", "grey": "#98989d", "green": "#30d158", "amber": "#ff9f0a", "blue": "#0a84ff",
        "purple": "#bf5af2", "teal": "#40c8e0",
    },
}

CARD_STYLES = ("Card.TLabel", "Card.TButton", "Card.Accent.TButton", "Card.TEntry", "Card.TRadiobutton",
               "Card.TCheckbutton", "Card.Horizontal.TProgressbar", "Card.TFrame")


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
    # Our own: a large title like macOS's, and small bold section headings for the sidebar.
    title_family = tkfont.nametofont("SunValleyTitleFont", root=root).cget("family")
    strong_family = tkfont.nametofont("SunValleyBodyStrongFont", root=root).cget("family")
    # Kept with the root: tkinter deletes a named font when its Font object goes.
    root._dc_fonts = [  # type: ignore[attr-defined]
        tkfont.Font(root=root, name="DubLargeTitleFont", family=title_family, size=-round(24 * scale)),
        tkfont.Font(root=root, name="DubSectionFont", family=strong_family, size=-round(12 * scale))]


def _colorref(color: str) -> int:
    color = color.lstrip("#")
    return int(color[4:6], 16) << 16 | int(color[2:4], 16) << 8 | int(color[0:2], 16)


def set_title_bar(window: tk.Misc, dark: bool, caption: str = "") -> None:
    """Dark or light Windows title bar (DWMWA_USE_IMMERSIVE_DARK_MODE is 20, or 19 on older builds). On
    Windows 11 it also takes the window's own colour (``caption``), so it blends in as on a Mac, with
    rounded corners."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        if caption:  # Windows 11 only; older versions ignore these
            for attribute, setting in ((33, 2),                       # DWMWA_WINDOW_CORNER_PREFERENCE: round
                                       (35, _colorref(caption)),      # DWMWA_CAPTION_COLOR
                                       (34, _colorref(caption))):     # DWMWA_BORDER_COLOR
                value = ctypes.c_int(setting)
                set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
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
    style.configure("Card.Strong.TLabel", background=palette["card"], foreground=palette["fg"],
                    font="SunValleyBodyStrongFont")
    style.configure("Title.TLabel", font="SunValleyTitleFont")
    style.configure("Subtitle.TLabel", font="SunValleySubtitleFont")
    style.configure("Strong.TLabel", font="SunValleyBodyStrongFont")
    style.configure("Caption.TLabel", font="SunValleyCaptionFont", foreground=palette["muted"])
    style.configure("Good.TLabel", foreground=palette["green"])
    style.configure("Bad.TLabel", foreground=palette["red"])
    style.configure("LargeTitle.TLabel", font="DubLargeTitleFont")
    style.configure("Section.TLabel", background=palette["card"], foreground=palette["muted"], font="DubSectionFont")
    style.configure("Card.Caption.TLabel", background=palette["card"], foreground=palette["muted"],
                    font="SunValleyCaptionFont")
    style.configure("Card.Link.TLabel", background=palette["card"], foreground=palette["link"],
                    font="SunValleyCaptionFont")

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
    style.configure("Results.Treeview", indent=0, background=palette["bg"], fieldbackground=palette["bg"])
    # No frame around the results: they sit straight on the window, as in Finder.
    style.layout("Results.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.map("Treeview", background=[("selected", palette["row_selected"])],
              foreground=[("selected", palette["fg"])])
    # Plain column headings: small grey text, no buttons.
    try:
        style.element_create("Flat.Treeheading.cell", "from", "default", "Treeheading.cell")
    except tk.TclError:
        pass  # this theme has it already
    style.layout("Results.Treeview.Heading", [
        ("Flat.Treeheading.cell", {"sticky": "nswe"}),
        ("Treeheading.padding", {"sticky": "nswe", "children": [
            ("Treeheading.image", {"side": "right", "sticky": ""}),
            ("Treeheading.text", {"sticky": "we"})]})])
    style.configure("Results.Treeview.Heading", background=palette["bg"], foreground=palette["muted"],
                    font="DubSectionFont", padding=(round(8 * scale), round(6 * scale)), relief="flat",
                    borderwidth=0)
    style.map("Results.Treeview.Heading", background=[("active", palette["row_odd"])],
              foreground=[("active", palette["fg"])])
    root.configure(background=palette["bg"])
    set_title_bar(root, mode == "dark", palette["bg"])
    return palette
