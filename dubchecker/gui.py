"""The main window."""
from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from dubchecker import APP_NAME, __version__, glass, theme
from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import AIR_FILTERS, Config, Dismissed, Overrides
from dubchecker.dub_sources import make_online_lookup
from dubchecker.media_probe import MediaProber
from dubchecker.models import STATUS_BY_CODE, Category, FileResult, FileStatus, ShowResult, language_word
from dubchecker.pipeline import ScanOutcome, ScanPipeline, evaluate_season
from dubchecker.results_store import SavedScan, delete_scan, load_scan, save_scan
from dubchecker.categorize import dub_tag
from dubchecker.sonarr import (TAG_DUAL_AUDIO, TAG_DUB_PENDING, TAG_ENGLISH_ONLY, TAG_MISSING_DUB, TAG_NO_DUB,
                               DELETE_SHOW, DeleteTarget, SonarrClient, SonarrError, apply_dub_tags, delete_from_sonarr,
                               search_for_replacements, series_page_url)

log = logging.getLogger(__name__)

READY_TEXT = "Ready."
# Solid pointers: easy to see and hit, and part of Segoe UI itself. (Chevrons and the small triangles
# rely on Tk borrowing the glyph from another font, which can show as a box instead.)
ARROW_CLOSED, ARROW_OPEN = "►", "▼"
TOOLTIP_DELAY_MS = 550
REFRESH_INTERVAL = 0.5  # seconds between table rebuilds while a scan is adding results
SCHEDULE_CHECK_MS = 60_000  # how often to see whether a scheduled scan is due
# (id, heading, width at 100 %, stretches)
COLUMNS = (
    ("show", "Show", 230, True),
    ("anilist", "Matched on AniList", 220, False),
    ("air", "Air status", 150, False),
    ("rating", "Rating", 70, False),
    ("episodes", "Episodes", 70, False),
    ("dual", "Dual audio", 90, False),
    ("orig", "Original only", 105, False),
    ("eng", "English only", 95, False),
    ("confirmed", "Dub confirmed by", 160, False),
    ("match", "Match %", 70, False),
    ("notes", "Notes", 220, True),
    ("group", "Group", 140, False),  # only shown when several groups are listed together
)
NUMERIC_COLUMNS = frozenset({"audio", "rating", "episodes", "dual", "orig", "eng", "match"})
BAR_WIDTH, BAR_HEIGHT = 96, 11  # the audio bar in the first column, at 100 % scaling
DRAG_THRESHOLD = 10  # pixels (at 100 %) a tab must move before a press counts as dragging, not clicking
# Episode rows: only the episodes that need attention are coloured; the square in the Audio column
# already shows every episode's colour, so full-colour text on every row was just noise.
EPISODE_TEXT_COLORS: dict[FileStatus, str] = {
    FileStatus.DUAL: "fg",
    FileStatus.ENGLISH_ONLY: "blue",
    FileStatus.ORIGINAL_ONLY: "red",
    FileStatus.UNLABELED: "amber",
    FileStatus.ERROR: "muted",
}
PENDING_COLOR = "purple"  # an episode whose dub is still on its way: in the audio bar, the key and its row


class AudioBars:
    """Little coloured pictures for the Audio column: one block per episode, in episode order."""

    def __init__(self, app: App) -> None:
        self.app = app
        self._images: dict[tuple, tk.PhotoImage] = {}

    def clear(self) -> None:
        """Forget the images, e.g. after the colours change with the theme."""
        self._images.clear()

    def _new(self, width: int, height: int) -> tk.PhotoImage:
        return tk.PhotoImage(master=self.app.root, width=width, height=height)

    def _color(self, status: FileStatus | None) -> str:
        """A status's colour; None is an episode whose dub is still on its way."""
        return self.app.palette[PENDING_COLOR if status is None else ui.STATUS_COLORS[status]]

    def bar(self, code: str) -> tk.PhotoImage:
        """``code`` has one letter per episode (``ui.display_code``: ``ShowResult.audio_code`` with "p" for
        episodes waiting for their dub); equal codes share one image."""
        key = ("bar", code)
        if key not in self._images:
            width, height = self.app.px(BAR_WIDTH), self.app.px(BAR_HEIGHT)
            image = self._new(width, height)
            count = len(code)
            gap = 1 if count and width / count >= 4 else 0  # separate the blocks when there's room
            for number, letter in enumerate(code):
                left = round(number * width / count)
                right = max(left + 1, round((number + 1) * width / count) - gap)
                image.put(self._color(None if letter == "p" else STATUS_BY_CODE[letter]),
                          to=(left, 0, right, height))
            self._images[key] = image
        return self._images[key]

    def square(self, status: FileStatus | None) -> tk.PhotoImage:
        key = ("square", status)
        if key not in self._images:
            size = self.app.px(BAR_HEIGHT)
            image = self._new(size, size)
            image.put(self._color(status), to=(0, 0, size, size))
            self._images[key] = image
        return self._images[key]


def column_at(tree: ttk.Treeview, x: int) -> str:
    """The column id under x ("show", "notes", ...), "#0" for the Audio column, or "" for none.

    Treeview reports "#1", "#2"... by position among the columns on show, which changes when the
    Group column appears, so this turns that into the column's name.
    """
    column = tree.identify_column(x)
    if column in ("", "#0"):
        return column
    try:
        return str(tree.column(column, "id"))
    except tk.TclError:
        return ""


class CellTooltip:
    """Shows the full text of a table cell that's too narrow for it, after the pointer rests there."""

    def __init__(self, tree: ttk.Treeview, app: App) -> None:
        self.tree = tree
        self.app = app
        self.cell: tuple[str, str] | None = None
        self.pending: str | None = None
        self.window: tk.Toplevel | None = None

    def track(self, event: Any) -> None:
        cell = (self.tree.identify_row(event.y), column_at(self.tree, event.x))
        if cell == self.cell:
            return
        self.hide()
        self.cell = cell
        if cell[0] and cell[1] not in ("", "#0"):
            self.pending = self.tree.after(TOOLTIP_DELAY_MS, lambda: self._show(event.x_root, event.y_root))

    def hide(self, _event: Any = None) -> None:
        self.cell = None
        if self.pending:
            self.tree.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def _show(self, x: int, y: int) -> None:
        self.pending = None
        if self.cell is None or not self.tree.exists(self.cell[0]):
            return
        iid, column = self.cell
        text = str(self.tree.set(iid, column)).strip()
        font = "SunValleyBodyStrongFont" if "parent" in self.tree.item(iid, "tags") else "SunValleyBodyFont"
        if not text or tkfont.nametofont(font).measure(text) <= self.tree.column(column, "width") - self.app.px(12):
            return  # it already fits
        pal = self.app.palette
        self.window = tk.Toplevel(self.tree)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(self.window, text=text, justify="left", wraplength=self.app.px(520), font="SunValleyBodyFont",
                 background=pal["card"], foreground=pal["fg"], padx=self.app.px(8), pady=self.app.px(5),
                 highlightthickness=1, highlightbackground=pal["border"]).pack()
        self.window.geometry(f"+{x + self.app.px(14)}+{y + self.app.px(18)}")


@dataclass
class AppContext:
    config: Config
    config_path: Path
    overrides: Overrides
    cache: Cache
    data_dir: Path
    dismissed: Dismissed | None = None  # made from data_dir when not given

    def __post_init__(self) -> None:
        if self.dismissed is None:
            self.dismissed = Dismissed(self.data_dir / "dismissed.json")

    def save_config(self) -> None:
        try:
            self.config.save(self.config_path)
        except OSError as exc:
            log.warning("Couldn't save settings: %s", exc)


class SidebarItem(tk.Canvas):
    """A tab in the sidebar: click to select or deselect it, drag it up or down to move it, right-click for
    more. Selected tabs sit on a rounded highlight, as in Finder's sidebar."""

    def __init__(self, app: App, parent: tk.Misc, tab: str) -> None:
        super().__init__(parent, height=app.px(30), width=1, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.app = app
        self.tab = tab
        self.info = ui.TABS[tab]
        self.hover = False
        self.selected = False
        self.dragging = False
        self._images: list[tk.PhotoImage] = []
        self._pill = self.create_image(0, 0, anchor="nw")
        self._dot = self.create_image(0, 0, anchor="w")
        self._title = self.create_text(0, 0, text=self.info.title, anchor="w", font="SunValleyBodyFont")
        self.count = glass.TextItem(self, self.create_text(0, 0, text="", anchor="e", font="SunValleyBodyFont"))
        # Selecting happens on release, so a press that turns into a drag moves the tab instead.
        self.bind("<ButtonPress-1>", lambda e: app.card_press(tab, e))
        self.bind("<B1-Motion>", lambda e: app.card_drag(tab, e))
        self.bind("<ButtonRelease-1>", lambda e: app.card_release(tab, e))
        self.bind("<Button-3>", lambda e: app.tab_menu(tab, e))
        if sys.platform == "darwin":
            self.bind("<Button-2>", lambda e: app.tab_menu(tab, e))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Configure>", lambda _e: self.recolor())

    def _set_hover(self, hover: bool) -> None:
        self.hover = hover
        self.recolor()

    def set_count(self, value: int | None, total: int | None = None) -> None:
        """``total``: how many there are with the Air status filter off, when it's on ("3 of 19")."""
        text = "" if value is None else f"{value:,}" if total is None else f"{value:,} of {total:,}"
        self.itemconfigure(self.count.item, text=text)

    def recolor(self) -> None:
        pal, px = self.app.palette, self.app.px
        width = max(self.winfo_width(), px(120))
        height = max(self.winfo_height(), int(self.cget("height")))
        middle = height / 2
        color = pal[self.info.color]
        row = pal["card_selected"] if self.selected or self.dragging else pal["card_hover"] if self.hover else ""
        self.configure(background=pal["card"], cursor="fleur" if self.dragging else "hand2")
        images = []
        if row:
            edge = (color, color) if self.dragging else ()
            images.append(glass.stretched(self, glass.Look(px(7), row, pal["card"], *edge), width, height))
            self.itemconfigure(self._pill, image=images[-1], state="normal")
        else:
            self.itemconfigure(self._pill, image="", state="hidden")  # the old image is about to go
        images.append(glass.dot(self, color, row or pal["card"], px(10)))
        self.itemconfigure(self._dot, image=images[-1])
        self.coords(self._dot, px(11), middle)
        self.coords(self._title, px(30), middle)
        self.itemconfigure(self._title, fill=pal["fg"])
        self.coords(self.count.item, width - px(11), middle)
        self.itemconfigure(self.count.item, fill=pal["muted"])
        self._images = images


class App:
    def __init__(self, root: tk.Tk, ctx: AppContext) -> None:
        self.root = root
        self.ctx = ctx
        self.config = ctx.config
        self.queue: queue.Queue[tuple[str, tuple]] = queue.Queue()
        self.cancel = threading.Event()
        self.closing = threading.Event()
        self.worker: threading.Thread | None = None
        self.helpers: list[threading.Thread] = []
        self.scanning = False
        self.dismissed: Dismissed = ctx.dismissed  # type: ignore[assignment]  # set by AppContext
        self.model = ui.ResultsModel()
        self.model.dismissed = self.dismissed.keys()
        self.model.dub_grace_days = self.config.dub_grace_days
        self._timing_hour = int(time.time() // 3600)  # dubs on their way are re-checked every hour
        self.tabs: set[str] = {Category.NEEDS_DUB.name}  # selected tabs (ui.TAB_IDS); none = every show
        self._drag: dict[str, Any] | None = None  # the tab being pressed or dragged
        # The scan running now: None = every show, else the show folders of one tab (``scan_scope``).
        self.scan_only: set[str] | None = None
        self.scan_scope = "all"
        self.scan_scheduled = False
        self.scan_keys: set[str] = set()
        self._schedule_id: str | None = None
        self.sort_column = "show"
        self.sort_desc = False
        self.expanded: set[str] = set()           # shows whose seasons are listed
        self.expanded_seasons: set[str] = set()   # seasons whose episodes are listed
        self.titles: dict[str, tuple[str, str]] = {}  # row -> (indent, label) for redrawing its arrow
        self.bars = AudioBars(self)
        self.scale = theme.dpi_scale(root)
        self._poll_id: str | None = None
        self._refresh_id: str | None = None  # a scheduled table rebuild during a scan
        self._last_refresh = 0.0
        self.results_path = ctx.data_dir / "last_scan.json"
        self.results_source = self.config.scan_source  # where the results on screen came from
        self.results_time = 0.0
        self.results_stopped = False
        self.surfaces: list[Any] = []            # glass-drawn widgets, repainted with the theme
        self._card_frames: list[tk.Frame] = []   # plain frames on glass panels

        root.title(APP_NAME)
        self._set_icon()
        self.palette = theme.apply_theme(root, self.config.theme)
        self._build()
        self._layout_cards()
        self._show_air_filter()
        self.apply_palette()
        root.bind("<<ThemeChanged>>", self._on_theme_changed, add="+")
        root.bind("<F5>", lambda _e: self.start_scan())
        root.bind("<Escape>", lambda _e: self.stop_scan())
        root.bind("<Control-f>", self._focus_filter)
        root.bind("<Control-F>", self._focus_filter)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._restore_geometry()
        self._reset_view()
        self._load_saved_results()
        if self.config.api_key_unreadable:
            self.status_var.set("Your Sonarr API key was saved on another PC or Windows account, so it can't be "
                                "read here. Enter it again in Settings > Sonarr.")
        self._poll()
        self.update_schedule_label()
        self._schedule_id = root.after(SCHEDULE_CHECK_MS, self._check_schedule)

    # ------------------------------------------------------------ building

    def _set_icon(self) -> None:
        try:
            if sys.platform == "win32":
                self.root.iconbitmap(default=str(ui.asset_path("icon.ico")))
            else:
                self._icon_image = tk.PhotoImage(file=str(ui.asset_path("icon_256.png")))
                self.root.iconphoto(True, self._icon_image)
        except tk.TclError as exc:
            log.warning("Couldn't set the window icon: %s", exc)

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def _build(self) -> None:
        """A glass sidebar on the left (tabs, scan controls, Settings); the list on the right, with its
        title, search and filters above it and the scan's progress below."""
        root = self.root
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        gap = self.px(8)
        self._build_sidebar().grid(row=0, column=0, sticky="ns", padx=(gap, 0), pady=gap)
        content = ttk.Frame(root)
        content.grid(row=0, column=1, sticky="nsew", padx=(self.px(16), self.px(20)), pady=(self.px(14), self.px(8)))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        self._build_toolbar(content).grid(row=0, column=0, sticky="ew", pady=(0, self.px(12)))
        self._build_table(content).grid(row=1, column=0, sticky="nsew")
        self._build_activity(content).grid(row=2, column=0, sticky="ew", pady=(self.px(8), 0))
        self._build_footer(content).grid(row=3, column=0, sticky="ew", pady=(self.px(4), 0))

    def _card_frame(self, parent: tk.Misc) -> tk.Frame:
        """A plain frame on a glass panel, in the panel's colour."""
        frame = tk.Frame(parent, borderwidth=0, highlightthickness=0)
        self._card_frames.append(frame)
        return frame

    def _build_sidebar(self) -> glass.GlassPanel:
        panel = glass.GlassPanel(self.root, self, "sidebar", padding=10)
        panel.configure(width=self.px(284))
        panel.pack_propagate(False)
        side = panel.body
        side.columnconfigure(0, weight=1)
        side.rowconfigure(3, weight=1)  # room between the tabs and the scan controls

        header = self._card_frame(side)
        header.grid(row=0, column=0, sticky="ew", pady=(self.px(2), self.px(16)))
        header.columnconfigure(1, weight=1)
        try:
            full = tk.PhotoImage(file=str(ui.asset_path("icon_256.png")))
            self.logo = full.subsample(max(1, round(256 / (34 * self.scale))))
            ttk.Label(header, image=self.logo, style="Card.TLabel").grid(row=0, column=0, rowspan=2,
                                                                         padx=(self.px(4), self.px(10)))
        except tk.TclError as exc:
            log.warning("Couldn't load the logo: %s", exc)
        ttk.Label(header, text=APP_NAME, style="Card.Strong.TLabel").grid(row=0, column=1, sticky="sw")
        ttk.Label(header, text=f"Version {__version__}", style="Card.Caption.TLabel").grid(row=1, column=1,
                                                                                          sticky="nw")
        glass.PillButton(header, self, "Settings", self.open_settings, backdrop="card", small=True).grid(
            row=0, column=2, rowspan=2, sticky="e")

        heading = self._card_frame(side)
        heading.grid(row=1, column=0, sticky="ew", padx=self.px(10), pady=(0, self.px(4)))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="Library", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.edit_tabs_link = ttk.Label(heading, text="Edit", style="Card.Link.TLabel", cursor="hand2")
        self.edit_tabs_link.grid(row=0, column=1, sticky="e")
        self.edit_tabs_link.bind("<Button-1>", self.open_tabs_menu)
        self.tab_visible_vars = {tab: tk.BooleanVar(value=tab not in self.config.hidden_tabs) for tab in ui.TAB_IDS}

        self.cards_frame = self._card_frame(side)
        self.cards_frame.grid(row=2, column=0, sticky="ew")
        self.cards_frame.columnconfigure(0, weight=1)
        self.cards: dict[str, SidebarItem] = {tab: SidebarItem(self, self.cards_frame, tab) for tab in ui.TAB_IDS}
        self.no_tabs_label = ttk.Label(self.cards_frame, text="Every tab is hidden. Click Edit to bring them back.",
                                       style="Card.Caption.TLabel", wraplength=self.px(220))

        self._build_scan_controls(side).grid(row=4, column=0, sticky="ew", pady=(self.px(12), self.px(4)))
        return panel

    def _build_scan_controls(self, parent: tk.Misc) -> tk.Frame:
        """Where to scan from, how much to scan, and the buttons."""
        box = self._card_frame(parent)
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Scan", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=self.px(10),
                                                                pady=(0, self.px(6)))
        self.source_var = tk.StringVar(value=self.config.scan_source)
        self.source_switch = glass.SegmentedControl(box, self, self.source_var,
                                                    [("folder", "Local files"), ("sonarr", "Sonarr")],
                                                    command=self._on_source_change)
        self.source_switch.grid(row=1, column=0, sticky="ew")
        self.source_buttons = self.source_switch.segments

        self.folder_row = self._card_frame(box)
        self.folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=self.config.library_path)
        self.folder_entry = ttk.Entry(self.folder_row, textvariable=self.folder_var, style="Card.TEntry", width=1)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, self.px(6)))
        self.folder_entry.bind("<Return>", lambda _e: self.start_scan())
        self.browse_button = glass.PillButton(self.folder_row, self, "Browse...", self.browse_folder,
                                              backdrop="card", small=True)
        self.browse_button.grid(row=0, column=1)

        self.sonarr_row = self._card_frame(box)
        self.sonarr_row.columnconfigure(0, weight=1)
        self.sonarr_label = ttk.Label(self.sonarr_row, style="Card.Muted.TLabel", wraplength=self.px(150))
        self.sonarr_label.grid(row=0, column=0, sticky="w", padx=(self.px(4), self.px(6)))
        glass.PillButton(self.sonarr_row, self, "Change...", lambda: self.open_settings(tab=2),
                         backdrop="card", small=True).grid(row=0, column=1)

        self.scope_box = ttk.Combobox(box, state="readonly", width=1)
        self.scope_box.grid(row=3, column=0, sticky="ew", pady=(self.px(10), 0))
        self.scope_box.bind("<<ComboboxSelected>>", self._on_scope_chosen)
        self._scope_ids: list[str] = []

        buttons = self._card_frame(box)
        buttons.grid(row=4, column=0, sticky="ew", pady=(self.px(10), 0))
        buttons.columnconfigure(0, weight=1)
        self.scan_button = glass.PillButton(buttons, self, "Scan library", self.start_scan, accent=True,
                                            backdrop="card")
        self.scan_button.grid(row=0, column=0, sticky="ew")
        self.stop_button = glass.PillButton(buttons, self, "Stop", self.stop_scan, backdrop="card")
        self.stop_button.grid(row=0, column=1, padx=(self.px(6), 0))

        more = self._card_frame(box)
        more.grid(row=5, column=0, sticky="w", pady=(self.px(4), 0))
        self.clear_button = glass.PillButton(more, self, "Clear results", self.clear_results, backdrop="card",
                                             small=True)
        self.clear_button.grid(row=0, column=0)
        self.tag_button = glass.PillButton(more, self, "Tag in Sonarr...", self.tag_in_sonarr, backdrop="card",
                                           small=True)
        self.tag_button.grid(row=0, column=1, padx=(self.px(4), 0))
        self._on_source_change(save=False)
        return box

    def _layout_cards(self) -> None:
        """List the tabs that aren't hidden in the user's order."""
        visible = self.visible_tabs()
        for tab, card in self.cards.items():
            if tab not in visible:
                card.grid_remove()
        for row, tab in enumerate(visible):
            self.cards[tab].grid(row=row, column=0, sticky="ew", pady=(0, self.px(2)))
        if visible:
            self.no_tabs_label.grid_remove()
        else:
            self.no_tabs_label.grid(row=0, column=0, sticky="w", padx=self.px(10), pady=self.px(4))
        self._update_scope_box()

    def tabs_menu(self) -> tk.Menu:
        """The Library section's Edit menu: tick the tabs to show in the sidebar."""
        menu = tk.Menu(self.root, tearoff=False)
        for tab in ui.ordered_tabs(self.config.tab_order):
            if tab != ui.AIRING or self.air_status_on:  # no Airing tab when air status is off
                menu.add_checkbutton(label=ui.TABS[tab].title, variable=self.tab_visible_vars[tab],
                                     command=lambda t=tab: self.set_tab_visible(t, self.tab_visible_vars[t].get()))
        menu.add_separator()
        menu.add_command(label="Show all tabs", command=self.show_all_tabs)
        return menu

    def open_tabs_menu(self, event: Any) -> None:
        menu = self.tabs_menu()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_toolbar(self, parent: tk.Misc) -> ttk.Frame:
        bar = ttk.Frame(parent)
        bar.columnconfigure(0, weight=1)
        self.group_title = ttk.Label(bar, style="LargeTitle.TLabel")
        self.group_title.grid(row=0, column=0, sticky="w")
        self.group_description = ttk.Label(bar, style="Muted.TLabel")
        self.group_description.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.px(2), 0))
        bar.bind("<Configure>", lambda e: self.group_description.configure(wraplength=max(self.px(300), e.width)),
                 add="+")

        # Narrowing the list: search text and air status.
        filters = ttk.Frame(bar)
        filters.grid(row=0, column=1, sticky="e")
        self.filter_var = tk.StringVar()
        self.search_field = glass.SearchField(filters, self, self.filter_var, width=self.px(220))
        self.search_field.pack(side="left")
        self.filter_entry = self.search_field.entry
        self.filter_entry.bind("<Escape>", self._clear_filter)
        self.filter_var.trace_add("write", lambda *_: self.refresh_table())
        self.air_filter_frame = ttk.Frame(filters)
        self.air_filter_frame.pack(side="left", padx=(self.px(10), 0))
        ttk.Label(self.air_filter_frame, text="Air status", style="Muted.TLabel").pack(side="left",
                                                                                     padx=(0, self.px(6)))
        self.air_filter_box = ttk.Combobox(self.air_filter_frame, state="readonly", width=15,
                                           values=[ui.AIR_FILTER_LABELS[f] for f in AIR_FILTERS])
        self.air_filter_box.set(ui.AIR_FILTER_LABELS[self.config.air_filter])
        self.air_filter_box.pack(side="left")
        self.air_filter_box.bind("<<ComboboxSelected>>", self._on_air_filter)

        strip = ttk.Frame(bar)  # the audio key, and buttons for the list
        strip.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(self.px(10), 0))
        strip.columnconfigure(0, weight=1)
        legend = ttk.Frame(strip)
        legend.grid(row=0, column=0, sticky="w")
        tools = ttk.Frame(strip)
        tools.grid(row=0, column=1, sticky="e")

        def make_room(event: Any) -> None:  # the buttons go under the key when both don't fit
            if legend.winfo_reqwidth() + tools.winfo_reqwidth() + self.px(16) > event.width:
                tools.grid_configure(row=1, column=0, pady=(self.px(6), 0))
            else:
                tools.grid_configure(row=0, column=1, pady=0)
        strip.bind("<Configure>", make_room, add="+")
        self.dismissed_button = glass.PillButton(tools, self, "Dismissed shows", self.open_dismissed, small=True)
        self.dismissed_button.pack(side="left", padx=(0, self.px(4)))
        self._update_dismissed_button()
        glass.PillButton(tools, self, "Expand all", lambda: self.expand_all(True), small=True).pack(side="left")
        glass.PillButton(tools, self, "Collapse all", lambda: self.expand_all(False), small=True).pack(
            side="left", padx=(self.px(4), 0))
        glass.PillButton(tools, self, "Export list...", self.export_csv, small=True).pack(side="left",
                                                                                        padx=(self.px(4), 0))
        self.legend_labels: list[tuple[ttk.Label, FileStatus | None]] = []
        for status, text in [*ui.LEGEND[:3], (None, "Dub on its way"), *ui.LEGEND[3:]]:
            label = ttk.Label(legend, text=text, compound="left", style="Caption.TLabel")
            label.pack(side="left", padx=(0, self.px(14)))
            self.legend_labels.append((label, status))
        return bar

    def _build_table(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show=("tree", "headings"),
                                 selectmode="extended", style="Results.Treeview")
        self.tree.heading("#0", text="Audio", anchor="w", command=lambda: self.sort_by("audio"))
        self.tree.column("#0", width=self.px(BAR_WIDTH + 18), minwidth=self.px(BAR_WIDTH + 18), stretch=False)
        for column, heading, width, stretch in COLUMNS:
            self.tree.heading(column, text=heading, anchor="w", command=lambda c=column: self.sort_by(c))
            self.tree.column(column, width=self.px(width), minwidth=self.px(min(width, 70)), stretch=stretch,
                             anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        # All the columns don't fit a small window, so the table can scroll sideways too.
        sideways = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=sideways.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        sideways.grid(row=1, column=0, sticky="ew")
        # Tag priority follows creation order, so the hover colour must exist before the stripes.
        self.tree.tag_configure("hover")
        self.tree.tag_configure("even")
        self.tree.tag_configure("odd")
        self.tree.tag_configure("parent", font="SunValleyBodyStrongFont")
        self._hover_iid = ""
        self._cursor = ""
        self.tooltip = CellTooltip(self.tree, self)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._on_leave)
        self.tree.bind("<MouseWheel>", self.tooltip.hide, add="+")
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<space>", self._on_return)
        self.tree.bind("<Right>", lambda _e: self._on_arrow_key(True))
        self.tree.bind("<Left>", lambda _e: self._on_arrow_key(False))
        self.tree.bind("<Delete>", lambda _e: self.delete_selected())
        self.tree.bind("<Button-3>", self._on_right_click)
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._on_right_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open_close)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_open_close)

        self.empty_label = tk.Label(frame, font="SunValleyBodyLargeFont", wraplength=self.px(560), justify="center")
        self._update_headings()
        return frame

    def _build_activity(self, parent: tk.Misc) -> glass.GlassPanel:
        """A glass panel under the list: the scan's current step in plain words, its progress, and the
        status line."""
        panel = glass.GlassPanel(parent, self, "card", padding=12)
        inner = panel.body
        inner.columnconfigure(0, weight=1)
        self.phase_frame = self._card_frame(inner)
        self.phase_frame.grid(row=0, column=0, sticky="ew", pady=(0, self.px(8)))
        self.phase_title = ttk.Label(self.phase_frame, style="Card.Strong.TLabel")
        self.phase_title.pack(fill="x")
        self.phase_detail = ttk.Label(self.phase_frame, style="Card.Muted.TLabel", wraplength=self.px(900))
        self.phase_detail.pack(fill="x", pady=(self.px(2), 0))
        self.progress = ttk.Progressbar(inner, style="Card.Horizontal.TProgressbar", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(0, self.px(8)))
        self.status_var = tk.StringVar(value=READY_TEXT)
        status = ttk.Label(inner, textvariable=self.status_var, style="Card.TLabel")
        status.grid(row=2, column=0, sticky="ew")
        self.schedule_label = ttk.Label(inner, style="Card.Muted.TLabel")
        self.schedule_label.grid(row=3, column=0, sticky="ew", pady=(self.px(4), 0))

        def wrap(event: Any) -> None:  # long lines wrap instead of running off the edge
            width = max(self.px(300), event.width - 8)
            for label in (self.phase_detail, status):
                label.configure(wraplength=width)
        inner.bind("<Configure>", wrap, add="+")
        return panel

    def _build_footer(self, parent: tk.Misc) -> ttk.Frame:
        footer = ttk.Frame(parent)
        footer.columnconfigure(0, weight=1)
        tip = ttk.Label(footer, style="Caption.TLabel",
                        text="Tip: click a row's arrow or audio bar (or double-click it) to open it; double-click an "
                             "episode for its audio tracks; right-click a show to fix its match, dismiss it and more. "
                             "Ctrl- or Shift-click to select several shows; Delete deletes them in Sonarr. "
                             "F5 scans, Esc stops.")
        tip.grid(row=0, column=0, sticky="w")
        footer.bind("<Configure>", lambda e: tip.configure(wraplength=max(self.px(300), e.width)), add="+")
        return footer

    def _restore_geometry(self) -> None:
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(self.px(1320), screen_w - 60), min(self.px(860), screen_h - 100)
        self.root.minsize(min(self.px(900), screen_w - 60), min(self.px(600), screen_h - 100))
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.config.window_geometry)
        if match:
            w, h, x, y = (int(v) for v in match.groups())
            if -20 <= x < screen_w - 100 and -20 <= y < screen_h - 100:  # still on screen
                self.root.geometry(f"{min(w, screen_w)}x{min(h, screen_h)}+{x}+{y}")
                return
        self.root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 3)}")

    # ------------------------------------------------------------ theming

    def _on_theme_changed(self, event: Any) -> None:
        if event.widget is self.root:  # sv-ttk's tk_setPalette recolours plain widgets; repaint ours after it
            self.root.after_idle(self.apply_palette)

    def apply_palette(self) -> None:
        pal = self.palette
        for frame in self._card_frames:
            frame.configure(background=pal["card"])
        for surface in self.surfaces:
            surface.recolor()
        for card in self.cards.values():
            card.selected = card.tab in self.tabs
            card.recolor()
        self.tree.tag_configure("hover", background=pal["row_hover"])
        self.tree.tag_configure("even", background=pal["row_even"])
        self.tree.tag_configure("odd", background=pal["row_odd"])
        for status, color in EPISODE_TEXT_COLORS.items():  # episodes that need attention stand out
            self.tree.tag_configure(f"audio_{status.name}", foreground=pal[color])
        self.tree.tag_configure("audio_PENDING", foreground=pal[PENDING_COLOR])
        self.empty_label.configure(background=pal["bg"], foreground=pal["muted"])  # the table's empty area shows the window colour
        self.bars.clear()
        for label, status in self.legend_labels:
            label.configure(image=self.bars.square(status))
        self.refresh_table()

    def set_theme(self, setting: str) -> None:
        self.palette = theme.apply_theme(self.root, setting)
        self.apply_palette()

    # ------------------------------------------------------------ scan source

    def _on_source_change(self, save: bool = True) -> None:
        source = self.source_var.get()
        if source == "sonarr":
            self.folder_row.grid_remove()
            self.sonarr_row.grid(row=2, column=0, sticky="ew", pady=(self.px(10), 0))
        else:
            self.sonarr_row.grid_remove()
            self.folder_row.grid(row=2, column=0, sticky="ew", pady=(self.px(10), 0))
        self.update_sonarr_summary()
        if save and source != self.config.scan_source:
            self.config.scan_source = source
            self.ctx.save_config()
            if not self.model.results:
                self._update_empty_state([])

    def settings_saved(self) -> None:
        """Settings changed: the Sonarr summary, and whether the Airing tab, Air status column and
        Air status filter show."""
        self.update_sonarr_summary()
        self.model.refresh_dub_timing(self.config.dub_grace_days)
        self.update_cards()
        self._layout_cards()
        self._show_air_filter()
        self.select_tabs(self.tabs)
        self.update_schedule_label()

    def update_sonarr_summary(self) -> None:
        url = re.sub(r"^https?://", "", self.config.sonarr_url.strip()).rstrip("/") or "Not set up yet"
        key = "" if self.config.sonarr_api_key.strip() else " (no API key yet)"
        self.sonarr_label.configure(text=f"{url}{key}")
        if self.sonarr_configured:  # tagging is only offered once Sonarr is set up
            self.tag_button.grid()
        else:
            self.tag_button.grid_remove()

    def browse_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, title="Choose your anime folder",
                                         initialdir=self.folder_var.get() or None, mustexist=True)
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    # ------------------------------------------------------------ scanning

    def _set_scanning(self, scanning: bool) -> None:
        self.scanning = scanning
        busy, idle = ("disabled",) if scanning else ("!disabled",), ("!disabled",) if scanning else ("disabled",)
        self.scan_button.state(busy)
        self.clear_button.state(busy)
        self.tag_button.state(busy)
        self.browse_button.state(busy)
        self.folder_entry.state(busy)
        self.scope_box.state(("disabled",) if scanning else ("readonly", "!disabled"))
        for button in self.source_buttons:
            button.state(busy)
        self.stop_button.state(idle)

    def start_scan(self, scope: str | None = None, scheduled: bool = False) -> bool:
        """Scan every show, or (``scope`` = a tab) only the shows in that tab, keeping every other result.
        A scheduled scan reports problems on the status line instead of in message boxes."""
        if self.scanning:
            return False
        scope = self.config.scan_scope if scope is None else scope

        def refuse(text: str, error: bool = False) -> bool:
            if scheduled:
                self.status_var.set("The scheduled scan didn't run: " + text.replace("\n", " "))
            elif error:
                messagebox.showerror(APP_NAME, text, parent=self.root)
            else:
                messagebox.showinfo(APP_NAME, text, parent=self.root)
            return False

        source = self.source_var.get()
        path = ""
        if source == "folder":
            path = self.folder_var.get().strip().strip('"')
            if not path:
                return refuse("Choose your anime folder first (click Browse...).")
            if not os.path.isdir(path):
                return refuse(f"The folder\n{path}\ncan't be found. Is the drive connected?", error=True)
            self.config.library_path = path
        elif not self.config.sonarr_url.strip() or not self.config.sonarr_api_key.strip():
            if scheduled:
                return refuse("Sonarr's address and API key aren't set.")
            messagebox.showinfo(APP_NAME, "Enter Sonarr's address and API key first.", parent=self.root)
            self.open_settings(tab=2)
            return False

        only: set[str] | None = None
        if scope != "all":
            title = ui.TABS[scope].title
            if not self.model.results or self.results_source != source:
                where = "Sonarr" if source == "sonarr" else "your local files"
                return refuse(f"To scan only {title}, scan every show from {where} once first, so Dub Checker "
                              "knows which shows are in it.")
            only = self.model.shows_in_tab(scope)
            if not only:
                return refuse(f"No shows are in {title} right now, so there's nothing to scan.")
        self.config.scan_source = source
        self.ctx.save_config()

        if only is None:
            self.model.clear()
        self.scan_only, self.scan_scope, self.scan_scheduled = only, scope, scheduled
        self.scan_keys: set[str] = set()  # seasons this scan has reported, to spot ones that are gone
        self.results_source = source
        self.cancel = threading.Event()
        self._set_scanning(True)
        self._show_phase("Starting the scan...", "")
        self._show_progress("Getting ready...", None, True)
        self.update_cards()
        self.refresh_table()
        self.worker = threading.Thread(target=self._scan_worker, args=(source, path, self.cancel, only),
                                       name="scan", daemon=True)
        self.worker.start()
        return True

    def _scan_worker(self, source: str, path: str, cancel: threading.Event, only: set[str] | None = None) -> None:
        def emit(kind: str, *payload: Any) -> None:
            self.queue.put((kind, payload))

        try:
            prober = MediaProber(self.ctx.data_dir / "bin", status=lambda t: emit("progress", t, None, True),
                                 cancel=cancel)

            def lookup_factory() -> Any:
                return make_online_lookup(self.ctx.cache, self.config, self.ctx.data_dir, cancel,
                                          lambda t: emit("progress", t, None, False))

            pipeline = ScanPipeline(self.config, self.ctx.cache, self.ctx.overrides, prober, lookup_factory, emit,
                                    cancel)
            if source == "sonarr":
                pipeline.run(sonarr=SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key), only=only)
            else:
                # Air status from Sonarr works after a local files scan too, when Sonarr is set up.
                wants_sonarr = self.config.air_status_source == "sonarr" and self.sonarr_configured
                air_client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key) if wants_sonarr else None
                dates_client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key) \
                    if self.sonarr_configured else None  # when episodes aired, for dubs on their way
                pipeline.run(root=path, air_client=air_client, only=only, dates_client=dates_client)
        except Exception as exc:  # the pipeline reports its own errors; this is a last resort
            log.exception("Scan worker crashed")
            emit("done", ScanOutcome(error=f"The scan failed unexpectedly: {exc}"))

    def stop_scan(self) -> None:
        if self.scanning and not self.cancel.is_set():
            self.cancel.set()
            self._show_progress("Stopping - finishing the files being read right now...", None, True)

    def clear_results(self) -> None:
        """Empty the table and reset the window, and forget the saved results.

        The file cache, AniList answers, settings and manual matches are kept.
        """
        if self.scanning:
            return
        self._reset_view()
        delete_scan(self.results_path)

    def _reset_view(self) -> None:
        self.model.clear()
        self.expanded.clear()
        self.filter_var.set("")
        self.status_var.set(READY_TEXT)
        self._hide_progress()
        self._set_scanning(False)
        self.update_cards()
        self.refresh_table()

    def _show_phase(self, title: str, explanation: str) -> None:
        self.phase_title.configure(text=title)
        self.phase_detail.configure(text=explanation)
        if explanation:
            self.phase_detail.pack(fill="x", pady=(self.px(2), 0))
        else:
            self.phase_detail.pack_forget()
        self.phase_frame.grid()

    def _show_progress(self, text: str, fraction: float | None, busy: bool) -> None:
        self.status_var.set(text)
        self.progress.grid()
        if busy:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        elif fraction is not None:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=max(0.0, min(100.0, fraction * 100)))

    def _hide_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.progress.grid_remove()
        self.phase_frame.grid_remove()

    def _poll(self) -> None:
        changed = False
        progress = None
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    progress = payload
                elif kind == "phase":
                    if self.scanning:
                        self._show_phase(*payload)
                elif kind in ("results", "result"):
                    items = payload[0] if kind == "results" else [payload[0]]
                    for result in items:
                        self._add_scanned(result)
                    changed = changed or bool(items)
                elif kind == "replace":
                    changed = self._on_replaced(payload[0]) or changed
                elif kind == "deleted":
                    self._on_deleted(*payload)
                elif kind == "notice":
                    self.status_var.set(payload[0])
                elif kind == "open_url":
                    ui.open_url(payload[0])
                    self.status_var.set(payload[1])
                elif kind == "error":
                    self.status_var.set(payload[0].splitlines()[0])
                    messagebox.showerror(APP_NAME, payload[0], parent=self.root)
                elif kind == "done":
                    if progress is not None and self.scanning:
                        self._show_progress(*progress)
                    progress = None
                    changed = False
                    self._scan_finished(payload[0])  # rebuilds the table itself
        except queue.Empty:
            pass
        if progress is not None and self.scanning:
            self._show_progress(*progress)
        if changed:
            self._request_refresh()
        if not self.closing.is_set():
            self._poll_id = self.root.after(100, self._poll)

    def _request_refresh(self) -> None:
        """Rebuild the cards and table now, or - while a scan streams results in - at most twice a second."""
        if self._refresh_id is not None:
            return  # already scheduled
        wait = self._last_refresh + REFRESH_INTERVAL - time.monotonic() if self.scanning else 0.0
        if wait <= 0:
            self._refresh_now()
        else:
            self._refresh_id = self.root.after(max(1, int(wait * 1000)), self._refresh_now)

    def _refresh_now(self) -> None:
        self._refresh_id = None
        self._last_refresh = time.monotonic()
        self.update_cards()
        self.refresh_table()

    def stop_polling(self) -> None:
        """Cancel the queue check so destroying the window leaves no stray callback behind."""
        self.closing.set()
        try:
            self.progress.stop()  # its animation timer would outlive the window
            for pending in (self._poll_id, self._refresh_id, self._schedule_id):
                if pending is not None:
                    self.root.after_cancel(pending)
            self._refresh_id = self._schedule_id = None
        except tk.TclError:  # the window is already gone
            pass
        self._poll_id = None

    def _scan_finished(self, outcome: ScanOutcome) -> None:
        if self._refresh_id is not None:  # the full refresh below replaces it
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None
        self.worker = None
        self._set_scanning(False)
        self._hide_progress()
        only, scheduled = self.scan_only, self.scan_scheduled
        if only is not None and not outcome.error and not outcome.stopped:
            self._forget_missing_seasons(only)
        shows = self.model.show_count() if only is None else len(only)
        what = ui.plural(shows, "show") + ("" if only is None else f" in {ui.TABS[self.scan_scope].title}")
        prefix = "Scheduled scan: " if scheduled else ""
        if outcome.error:
            self.status_var.set(f"{prefix}The scan couldn't finish." + (f" {outcome.error}" if scheduled else ""))
            if not scheduled:
                messagebox.showerror(APP_NAME, outcome.error, parent=self.root)
        elif outcome.stopped:
            kept = f"kept the {what} finished so far" if only is None else f"kept what was rechecked of the {what}"
            self.status_var.set(f"{prefix}Stopped - {kept} {outcome.stats.short_text()}")
        else:
            verb = "checked" if only is None else "rechecked"
            rest = "" if only is None else " The rest are as before."
            self.status_var.set(f"{prefix}Done - {verb} {what} in {ui.format_duration(outcome.elapsed)} "
                                f"{outcome.stats.short_text()}{rest}")
        self.update_cards()
        self.refresh_table()
        if only is None:
            self.results_time, self.results_stopped = time.time(), outcome.stopped
        self.save_results()
        self.scan_only, self.scan_scheduled = None, False
        if not outcome.error and not outcome.stopped and self.config.sonarr_tag_after_scan and self.sonarr_configured:
            self.tag_in_sonarr(automatic=True)
        for warning in outcome.warnings:
            if scheduled:  # nobody may be watching: no message boxes piling up
                log.warning("Scheduled scan: %s", warning)
                self.status_var.set(self.status_var.get() + " There was a warning - see logs/app.log.")
            else:
                messagebox.showwarning(APP_NAME, warning, parent=self.root)

    # ------------------------------------------------------------ groups and table

    # ------------------------------------------------------------ tabs

    @property
    def air_status_on(self) -> bool:
        return self.config.air_status_source != "off"

    def visible_tabs(self) -> list[str]:
        """Tabs not hidden by the user, in the user's order. The Airing tab also goes when air status is
        switched off."""
        return [tab for tab in ui.ordered_tabs(self.config.tab_order) if tab not in self.config.hidden_tabs
                and (tab != ui.AIRING or self.air_status_on)]

    # Moving tabs: drag one up or down the sidebar, or right-click it and choose Move up / Move down.

    def card_press(self, tab: str, event: Any) -> None:
        self._drag = {"tab": tab, "y": event.y_root, "moved": False}

    def card_drag(self, tab: str, event: Any) -> None:
        drag = self._drag
        if drag is None or drag["tab"] != tab:
            return
        if not drag["moved"]:
            if abs(event.y_root - drag["y"]) < self.px(DRAG_THRESHOLD):
                return  # still a click
            drag["moved"] = True
            self.cards[tab].dragging = True
            self.cards[tab].recolor()
        visible = self.visible_tabs()
        others = [t for t in visible if t != tab]
        # The dragged tab goes before the first tab whose middle is still below the pointer.
        index = sum(1 for t in others
                    if event.y_root > self.cards[t].winfo_rooty() + self.cards[t].winfo_height() / 2)
        self._set_visible_order(others[:index] + [tab] + others[index:])

    def card_release(self, tab: str, _event: Any) -> None:
        drag, self._drag = self._drag, None
        if drag is not None and drag["moved"]:
            self.cards[tab].dragging = False
            self.cards[tab].recolor()
            self.ctx.save_config()
            return
        self.toggle_tab(tab)

    def _set_visible_order(self, order: list[str]) -> None:
        """Put the tabs on show in this order; hidden tabs keep their places among them."""
        full = ui.ordered_tabs(self.config.tab_order)
        visible = self.visible_tabs()
        if order == visible:
            return
        slots = [i for i, tab in enumerate(full) if tab in visible]
        for slot, tab in zip(slots, order):
            full[slot] = tab
        self.config.tab_order = full
        self._layout_cards()

    def move_tab(self, tab: str, step: int) -> None:
        """Move a tab one place up (-1) or down (+1) among the tabs on show."""
        visible = self.visible_tabs()
        if tab not in visible:
            return
        index = visible.index(tab)
        target = max(0, min(len(visible) - 1, index + step))
        visible.insert(target, visible.pop(index))
        self._set_visible_order(visible)
        self.ctx.save_config()

    def view_tabs(self) -> set[str]:
        """The selected tabs that are on show. Empty means the list shows every show."""
        return self.tabs & set(self.visible_tabs())

    def select_tabs(self, tabs: set[str]) -> None:
        self.tabs = set(tabs)
        for card in self.cards.values():
            card.selected = card.tab in self.tabs
            card.recolor()
        self.refresh_table()

    def toggle_tab(self, tab: str) -> None:
        """Clicking a tab selects it (alongside any others) or, if it's selected, deselects it."""
        self.select_tabs(self.tabs ^ {tab})

    def set_tab_visible(self, tab: str, visible: bool) -> None:
        hidden = [t for t in self.config.hidden_tabs if t != tab] + ([] if visible else [tab])
        self.config.hidden_tabs = [t for t in ui.TAB_IDS if t in hidden]
        self.ctx.save_config()
        self.tab_visible_vars[tab].set(visible)
        self._layout_cards()
        self.select_tabs(self.tabs - ({tab} if not visible else set()))

    def show_all_tabs(self) -> None:
        for tab in ui.TAB_IDS:
            self.tab_visible_vars[tab].set(True)
        self.config.hidden_tabs = []
        self.ctx.save_config()
        self._layout_cards()
        self.refresh_table()

    def tab_menu(self, tab: str, event: Any) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        title = ui.TABS[tab].title
        menu.add_command(label=f"Deselect {title}" if tab in self.tabs else f"Select {title}",
                         command=lambda: self.toggle_tab(tab))
        menu.add_command(label=f"Show only {title}", command=lambda: self.select_tabs({tab}))
        menu.add_command(label="Deselect all tabs (list every show)", command=lambda: self.select_tabs(set()))
        menu.add_separator()
        visible = self.visible_tabs()
        menu.add_command(label="Move up", command=lambda: self.move_tab(tab, -1),
                         state="normal" if visible and visible[0] != tab else "disabled")
        menu.add_command(label="Move down", command=lambda: self.move_tab(tab, 1),
                         state="normal" if visible and visible[-1] != tab else "disabled")
        menu.add_separator()
        menu.add_command(label="Hide this tab", command=lambda: self.set_tab_visible(tab, False))
        if self.config.hidden_tabs:
            menu.add_command(label="Show all tabs", command=self.show_all_tabs)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def update_cards(self) -> None:
        counts = self.model.counts() if (self.model.results or self.scanning) else None
        # With an Air status filter, a card counts the shows it lists, out of all of them.
        listed = self.model.counts(self.air_filter) if counts is not None and self.air_filter != "all" else None
        for tab, card in self.cards.items():
            if listed is not None:
                card.set_count(listed[tab], counts[tab])  # type: ignore[index]
            else:
                card.set_count(None if counts is None else counts[tab])
        self._update_scope_box(counts)

    # ------------------------------------------------------------ scanning one tab

    def scope_tabs(self) -> list[str]:
        """The tabs a scan can be limited to, in the user's order (no Airing when air status is off)."""
        return [tab for tab in ui.ordered_tabs(self.config.tab_order) if tab != ui.AIRING or self.air_status_on]

    def scope_name(self, scope: str) -> str:
        return "every show" if scope == "all" else ui.TABS[scope].title

    def _update_scope_box(self, counts: dict[str, int] | None = None) -> None:
        """The Scan: drop-down - every show, or only the shows in one tab, with how many there are."""
        if counts is None and self.model.results:
            counts = self.model.counts()
        self._scope_ids = ["all", *self.scope_tabs()]
        if self.config.scan_scope not in self._scope_ids:
            self.config.scan_scope = "all"
        labels = ["Every show"]
        for tab in self._scope_ids[1:]:
            count = f" ({ui.plural(counts[tab], 'show')})" if counts else ""
            labels.append(f"Only {ui.TABS[tab].title}{count}")
        self.scope_box.configure(values=labels)
        self.scope_box.current(self._scope_ids.index(self.config.scan_scope))

    def _on_scope_chosen(self, _event: Any = None) -> None:
        self.config.scan_scope = self._scope_ids[self.scope_box.current()]
        self.scope_box.selection_clear()
        self.ctx.save_config()

    def _add_scanned(self, result: ShowResult) -> None:
        """A season from the scan. A one-tab scan that was stopped before a season keeps that season's
        earlier result rather than replacing it with "not checked"."""
        if self.scan_only is not None:
            self.scan_keys.add(result.key)
            old = self.model.results.get(result.key)
            if result.stopped and old is not None and not old.stopped:
                return
        self.model.add(result)

    def _forget_missing_seasons(self, shows: set[str]) -> None:
        """After a one-tab scan: seasons of the rescanned shows that weren't found again are gone."""
        by_show = self.model.by_show()
        self.model.remove([s.key for show in shows for s in by_show.get(show, []) if s.key not in self.scan_keys])

    # ------------------------------------------------------------ scheduled scans

    def schedule_due_at(self) -> float | None:
        """When the next scheduled scan is due (Unix time), or None if scheduling is off."""
        cfg = self.config
        if not cfg.schedule_enabled:
            return None
        return cfg.schedule_last_run + cfg.schedule_every_hours * 3600

    def _check_schedule(self) -> None:
        """Every minute: start the scheduled scan if it's due and nothing else is scanning."""
        self._schedule_id = None
        try:
            due = self.schedule_due_at()
            if due is not None and time.time() >= due and not self.scanning:
                self.config.schedule_last_run = time.time()
                self.ctx.save_config()
                self.start_scan(self.config.schedule_scope, scheduled=True)
            self.update_schedule_label()
            hour = int(time.time() // 3600)
            if hour != self._timing_hour and not self.scanning:
                self._timing_hour = hour
                if self.model.refresh_dub_timing():
                    self.update_cards()
                    self.refresh_table()
        finally:
            if not self.closing.is_set():
                self._schedule_id = self.root.after(SCHEDULE_CHECK_MS, self._check_schedule)

    def update_schedule_label(self) -> None:
        due = self.schedule_due_at()
        if due is None:
            self.schedule_label.configure(text="")
            self.schedule_label.grid_remove()
            return
        when = "in about a minute" if due <= time.time() else time.strftime("%a %d %b at %H:%M", time.localtime(due))
        self.schedule_label.configure(
            text=f"Next scheduled scan ({self.scope_name(self.config.schedule_scope)}): {when}")
        self.schedule_label.grid()

    # ------------------------------------------------------------ air status filter

    @property
    def air_filter(self) -> str:
        """The Air status drop-down's choice; "all" while air status is switched off."""
        return self.config.air_filter if self.air_status_on else "all"

    def _on_air_filter(self, _event: Any = None) -> None:
        label = self.air_filter_box.get()
        self.config.air_filter = next((f for f in AIR_FILTERS if ui.AIR_FILTER_LABELS[f] == label), "all")
        self.air_filter_box.selection_clear()
        self.ctx.save_config()
        self.update_cards()
        self.refresh_table()

    def _show_air_filter(self) -> None:
        if self.air_status_on:
            self.air_filter_frame.pack(side="left", padx=(self.px(10), 0))
        else:
            self.air_filter_frame.pack_forget()

    # ------------------------------------------------------------ dismissed shows

    def _update_dismissed_button(self) -> None:
        count = len(self.dismissed)
        self.dismissed_button.configure(text=f"Dismissed shows ({count})" if count else "Dismissed shows")

    def dismiss_show(self, show_id: str, title: str) -> None:
        """Hide a show from every tab and the list, until it's brought back from Dismissed shows."""
        self.dismissed.add(show_id, title, time.time())
        self._dismissed_changed()
        self.status_var.set(f"Dismissed {title}. It's hidden from every tab now; to bring it back, click "
                            "Dismissed shows above the list.")

    def dismiss_shows(self, shows: list[tuple[str, str]]) -> None:
        """Dismiss several shows at once: (folder name, title) pairs."""
        when = time.time()
        for show_id, title in shows:
            self.dismissed.add(show_id, title, when)
        self._dismissed_changed()
        self.status_var.set(f"Dismissed {ui.plural(len(shows), 'show')}. To bring them back, click Dismissed shows "
                            "above the list.")

    def restore_shows(self, show_ids: list[str]) -> None:
        self.dismissed.remove(show_ids)
        self._dismissed_changed()
        self.status_var.set(f"Brought back {ui.plural(len(show_ids), 'show')}.")

    def _dismissed_changed(self) -> None:
        self.model.dismissed = self.dismissed.keys()
        self._update_dismissed_button()
        self.update_cards()
        self.refresh_table()

    def open_dismissed(self) -> None:
        from dubchecker.dialogs import DismissedDialog
        DismissedDialog(self)

    @staticmethod
    def _label(indent: str, arrow: bool, is_open: bool, text: str) -> str:
        if not arrow:
            return f"{indent}{text}"
        return f"{indent}{ARROW_OPEN if is_open else ARROW_CLOSED}  {text}"

    # The value tuples follow COLUMNS: show, anilist, air, rating, episodes, dual, orig, eng, confirmed, match, notes,
    # group.

    def _season_values(self, r: ShowResult, label: str) -> tuple:
        rating = ui.rating_of(r)
        return (label, ui.anilist_text(r), ui.air_status_text(r), ui.rating_text(rating.value if rating else None),
                r.episodes, r.dual, r.original_only, r.english_only, ui.confirmed_text(r), ui.match_text(r),
                ui.notes_text(r), ui.group_title(r))

    def _parent_values(self, row: ui.ShowRow, label: str) -> tuple:
        sources: list[str] = []
        groups: list[str] = []
        for s in row.seasons:
            for source in (s.dub.sources if s.dub else []):
                if source not in sources:
                    sources.append(source)
            if ui.group_title(s) not in groups:
                groups.append(ui.group_title(s))
        speaker = ui.show_air_status(row.all_seasons)
        return (label, row.seasons_text(), ui.air_status_text(speaker) if speaker else "-",
                ui.rating_text(ui.show_rating(row.all_seasons)), row.total("episodes"),
                row.total("dual"), row.total("original_only"), row.total("english_only"), ", ".join(sources) or "-", "", row.also_text(),
                ", ".join(groups))

    @staticmethod
    def _episode_values(f: FileResult, label: str, note: str) -> tuple:
        tick = "✓"
        return (label, "", "", "", "", tick if f.status is FileStatus.DUAL else "",
                tick if f.status is FileStatus.ORIGINAL_ONLY else "", tick if f.status is FileStatus.ENGLISH_ONLY else "",
                "", "", note, "")

    def _sort_value(self, row: ui.ShowRow, column: str) -> Any:
        first = row.seasons[0]
        if column == "audio":  # share of episodes that still need English audio
            return row.total("original_only") / max(1, row.total("episodes"))
        if column == "episodes":
            return row.total("episodes")
        if column == "rating":  # unrated last
            rating = ui.show_rating(row.all_seasons)
            return -1.0 if rating is None else rating
        if column == "dual":
            return row.total("dual")
        if column == "orig":
            return row.total("original_only")
        if column == "eng":
            return row.total("english_only")
        if column == "match":
            return min((s.match.confidence if s.match else -1.0) for s in row.seasons)
        if column == "anilist":
            return ui.anilist_text(first).casefold()
        if column == "confirmed":
            return ui.confirmed_text(first).casefold()
        if column == "notes":
            return ui.notes_text(first).casefold()
        if column == "air":  # airing first, soonest next episode first
            speaker = ui.show_air_status(row.all_seasons)
            return ui.air_sort_key(speaker) if speaker else (9, 0)
        if column == "group":
            return ui.group_title(first)
        return row.title.casefold()

    def sort_by(self, column: str) -> None:
        if column == self.sort_column:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = column
            self.sort_desc = column in NUMERIC_COLUMNS  # numbers sort largest first
        self._update_headings()
        self.refresh_table()

    def _update_headings(self) -> None:
        for column, heading in [("audio", "Audio"), *((c[0], c[1]) for c in COLUMNS)]:
            marker = (" ▼" if self.sort_desc else " ▲") if column == self.sort_column else ""
            self.tree.heading("#0" if column == "audio" else column, text=heading + marker)

    def refresh_table(self) -> None:
        """Rebuild the rows for the current group, keeping scroll position, selection and open shows."""
        tree = self.tree
        top = tree.yview()[0]
        selection = tree.selection()
        focus = tree.focus()
        tree.delete(*tree.get_children())
        self.titles.clear()

        view = self.view_tabs()
        title, description = self._view_heading(view)
        if self.air_filter != "all":
            description += f" Only seasons that are “{ui.AIR_FILTER_LABELS[self.air_filter]}” are listed."
        self.group_title.configure(text=title)
        self.group_description.configure(text=description)
        # The Group column is only needed when seasons from different groups are listed together.
        mixed = len(view) != 1 or bool(view & ui.CROSS_TABS)
        tree.configure(displaycolumns=[c[0] for c in COLUMNS
                                       if (mixed or c[0] != "group") and (self.air_status_on or c[0] != "air")])

        rows = self.model.rows(view, self.filter_var.get(), self.air_filter)
        rows.sort(key=lambda r: (self._sort_value(r, self.sort_column), r.title.casefold()), reverse=self.sort_desc)
        for number, row in enumerate(rows):
            stripe = "odd" if number % 2 else "even"
            if row.single:
                self._insert_season("", row.seasons[0], "", row.seasons[0].group.display_title, stripe)
                continue
            iid = "p|" + row.show_id
            is_open = row.show_id in self.expanded
            self.titles[iid] = ("", row.title)
            self._insert_row("", iid, self._parent_values(row, self._label("", True, is_open, row.title)),
                             (stripe, "parent"), self.bars.bar("".join(ui.display_code(r) for r in row.seasons)),
                             is_open)
            for r in row.seasons:
                self._insert_season(iid, r, ui.INDENT, ui.season_name(r), stripe)

        hover, self._hover_iid = self._hover_iid, ""
        self._set_hover(hover)  # the rows were rebuilt; keep the highlight under the pointer
        kept = [iid for iid in selection if tree.exists(iid)]
        if kept:
            tree.selection_set(kept)
        if focus and tree.exists(focus):
            tree.focus(focus)
        tree.yview_moveto(top)
        self._update_empty_state(rows)

    @staticmethod
    def _view_heading(view: set[str]) -> tuple[str, str]:
        if not view:
            return "All shows", "No tab is selected, so every show is listed. Click a tab above to narrow it down."
        titles = [ui.TABS[tab].title for tab in ui.TAB_IDS if tab in view]
        if len(titles) == 1:
            info = ui.TABS[next(iter(view))]
            return info.title, info.description
        return " + ".join(titles), "These tabs are listed together; the Group column says where each season is."

    def _update_empty_state(self, rows: list) -> None:
        if rows:
            self.empty_label.place_forget()
            return
        view = self.view_tabs()
        if not self.model.results:
            text = "Scanning..." if self.scanning else ui.EMPTY_BEFORE_SCAN[self.source_var.get()]
        elif self.filter_var.get().strip() and self.model.rows(view, air_filter=self.air_filter):
            text = f"No shows match “{self.filter_var.get().strip()}”."
        elif self.air_filter != "all" and self.model.rows(view):
            text = (f"No shows here are “{ui.AIR_FILTER_LABELS[self.air_filter]}”. Set Air status to "
                    f"“{ui.AIR_FILTER_LABELS['all']}” to list them all.")
        elif self.scanning:
            text = "Scanning..."
        elif len(view) == 1:
            text = ui.TABS[next(iter(view))].empty
        else:
            text = "Nothing in the selected tabs."
        self.empty_label.configure(text=text)
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.empty_label.lift()

    @property
    def empty_text(self) -> str:
        """The empty-state message currently shown over the table, or ''."""
        return str(self.empty_label.cget("text")) if self.empty_label.winfo_manager() else ""

    # ------------------------------------------------------------ expanding

    def _insert_season(self, parent: str, r: ShowResult, indent: str, text: str, stripe: str) -> None:
        """A season row; its episodes are added when it's opened (or already open)."""
        iid = "s|" + r.key
        is_open = r.key in self.expanded_seasons
        self.titles[iid] = (indent, text)
        self._insert_row(parent, iid, self._season_values(r, self._label(indent, True, is_open, text)),
                         (stripe, "child") if parent else (stripe,), self.bars.bar(ui.display_code(r)), is_open)
        if is_open:
            self._insert_episodes(iid, r)

    def _insert_episodes(self, season_iid: str, r: ShowResult) -> None:
        if self.tree.get_children(season_iid):
            return
        indent = self.titles.get(season_iid, ("", ""))[0] + ui.INDENT + "  "  # line up after the arrow
        stripe = next(t for t in self.tree.item(season_iid, "tags") if t in ("even", "odd"))
        for number, f in enumerate(r.files):
            waiting = number in r.pending  # its dub is still on its way
            note = ui.episode_note(f) + (f" - {ui.pending_note(r)}" if waiting else "")
            self._insert_row(season_iid, f"e|{r.key}|{number}", self._episode_values(f, indent + ui.episode_label(f), note),
                             (stripe, "audio_PENDING" if waiting else f"audio_{f.status.name}"),
                             self.bars.square(None if waiting else f.status))

    def _insert_row(self, parent: str, iid: str, values: tuple, tags: tuple, image: tk.PhotoImage,
                    is_open: bool = False) -> None:
        """Treeview.insert without tkinter's per-value string conversion (a big share of refresh time
        with thousands of rows); Tk converts the Python tuples to lists itself."""
        self.tree.tk.call(self.tree._w, "insert", parent, "end", "-id", iid, "-values", values,  # type: ignore
                          "-tags", tags, "-image", image, "-open", is_open)

    @staticmethod
    def _can_open(iid: str) -> bool:
        return iid.startswith(("p|", "s|"))

    def _set_open(self, iid: str, is_open: bool) -> None:
        name = iid[2:]
        if iid.startswith("s|"):
            (self.expanded_seasons.add if is_open else self.expanded_seasons.discard)(name)
            if is_open and (result := self.model.results.get(name)) is not None:
                self._insert_episodes(iid, result)
        else:
            (self.expanded.add if is_open else self.expanded.discard)(name)
        self.tree.item(iid, open=is_open)
        indent, text = self.titles.get(iid, ("", name))
        self.tree.set(iid, "show", self._label(indent, True, is_open, text))

    def toggle(self, iid: str) -> None:
        if self._can_open(iid):
            self._set_open(iid, not self.tree.item(iid, "open"))

    def expand_all(self, is_open: bool) -> None:
        """Open or close every show's list of seasons (episode lists are opened one season at a time)."""
        for iid in self.tree.get_children():
            if iid.startswith("p|"):
                self._set_open(iid, is_open)

    def _on_tree_open_close(self, _event: Any) -> None:
        # Tk sends these before it changes the state, so read it afterwards.
        iid = self.tree.focus()
        if self._can_open(iid):
            self.root.after_idle(lambda: self.tree.exists(iid) and self._set_open(iid, bool(self.tree.item(iid, "open"))))

    def _on_arrow_key(self, open_it: bool) -> str:
        """Right opens a show or season; Left closes it, or jumps to the row it belongs to."""
        tree = self.tree
        iid = tree.focus()
        if not iid:
            return "break"
        if self._can_open(iid) and bool(tree.item(iid, "open")) != open_it:
            self._set_open(iid, open_it)
        elif not open_it and (parent := tree.parent(iid)):
            tree.selection_set(parent)
            tree.focus(parent)
            tree.see(parent)
        return "break"

    # ------------------------------------------------------------ mouse and keyboard

    def _result_for(self, iid: str) -> ShowResult | None:
        """The season a season row or episode row belongs to."""
        if iid.startswith("s|"):
            return self.model.results.get(iid[2:])
        if iid.startswith("e|"):
            return self.model.results.get(iid[2:].rsplit("|", 1)[0])
        return None

    def _file_for(self, iid: str) -> FileResult | None:
        result = self._result_for(iid) if iid.startswith("e|") else None
        if result is None:
            return None
        number = int(iid.rsplit("|", 1)[1])
        return result.files[number] if number < len(result.files) else None

    def _arrow_width(self, iid: str) -> int:
        """How far from the left of the Show cell a click still counts as clicking the arrow."""
        indent, _ = self.titles.get(iid, ("", ""))
        font = "SunValleyBodyStrongFont" if "parent" in self.tree.item(iid, "tags") else "SunValleyBodyFont"
        return tkfont.nametofont(font).measure(f"{indent}{ARROW_CLOSED}  ") + self.px(8)

    def _toggle_zone(self, event: Any) -> str:
        """The show/season row under the pointer if it's over its arrow or its audio bar, else ''."""
        tree = self.tree
        iid = tree.identify_row(event.y)
        if not self._can_open(iid):
            return ""
        column = column_at(tree, event.x)
        if column == "#0":
            return iid
        if column == "show":
            bbox = tree.bbox(iid, "show")
            if bbox and event.x - bbox[0] <= self._arrow_width(iid):
                return iid
        return ""

    def _on_click(self, event: Any) -> str | None:
        self.tooltip.hide()
        iid = self._toggle_zone(event)
        if not iid:
            return None
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.toggle(iid)
        return "break"

    def _on_double_click(self, event: Any) -> str | None:
        """Shows and seasons open or close; an episode opens the episode window at that file."""
        tree = self.tree
        if tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return None
        if self._toggle_zone(event):
            return "break"  # the first click of the double-click already toggled it
        iid = tree.identify_row(event.y)
        result = self._result_for(iid)
        if iid.startswith("s|") and column_at(tree, event.x) == "anilist" and result and result.match:
            ui.open_url(result.match.url)
        elif self._can_open(iid):
            self.toggle(iid)
        elif result is not None:
            self.show_details(result.key, self._file_for(iid))
        return "break"  # stop Tk's own double-click toggle

    def _on_return(self, _event: Any) -> str:
        """Enter or Space: open or close a show or season; on an episode, open the episode window."""
        iid = self.tree.focus()
        if self._can_open(iid):
            self.toggle(iid)
        elif (result := self._result_for(iid)) is not None:
            self.show_details(result.key, self._file_for(iid))
        return "break"

    def _on_motion(self, event: Any) -> None:
        iid = self.tree.identify_row(event.y)
        if iid != self._hover_iid:
            self._set_hover(iid)
        cursor = "hand2" if self._toggle_zone(event) else ""
        if cursor != self._cursor:
            self._cursor = cursor
            self.tree.configure(cursor=cursor)
        self.tooltip.track(event)

    def _on_leave(self, _event: Any) -> None:
        self._set_hover("")
        if self._cursor:
            self._cursor = ""
            self.tree.configure(cursor="")
        self.tooltip.hide()

    def _set_hover(self, iid: str) -> None:
        """Highlight the row under the pointer, so it's clear what a click will hit."""
        tree = self.tree
        old, self._hover_iid = self._hover_iid, ""
        if old and tree.exists(old):
            tree.item(old, tags=[t for t in tree.item(old, "tags") if t != "hover"])
        if iid and tree.exists(iid):
            tree.item(iid, tags=[*tree.item(iid, "tags"), "hover"])
            self._hover_iid = iid

    def _on_right_click(self, event: Any) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        menu = tk.Menu(self.root, tearoff=False)
        selection = self.tree.selection()
        if iid in selection and len(selection) > 1:  # Ctrl- or Shift-clicked several rows
            self._selection_menu(menu, selection)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        result = self._result_for(iid)
        episode = self._file_for(iid)
        if episode is not None and result is not None:
            key = result.key
            seasons = [result]
            menu.add_command(label="Open episode window", command=lambda: self.show_details(key, episode))
            if self.results_are_local:
                menu.add_command(label="Open file location", command=lambda: self.open_location_of_file(episode.path))
            menu.add_command(label="Copy file name", command=lambda: self.copy_text(episode.name))
            title = result.group.show_title
        elif result is not None:
            key = result.key
            seasons = [result]
            is_open = bool(self.tree.item(iid, "open"))
            menu.add_command(label="Hide episodes" if is_open else "Show episodes", command=lambda: self.toggle(iid))
            menu.add_command(label="Open episode window", command=lambda: self.show_details(key))
            menu.add_command(label="Open AniList page", state="normal" if result.match else "disabled",
                             command=lambda: result.match and ui.open_url(result.match.url))
            if self.results_are_local:
                menu.add_command(label="Open file location", command=lambda: self.open_location(seasons, True))
            menu.add_separator()
            menu.add_command(label="Wrong show? Fix the match...", command=lambda: self.fix_match(key))
            menu.add_command(label="Remove my manual match", command=lambda: self.remove_override(key),
                             state="normal" if key in self.ctx.overrides else "disabled")
            title = result.group.show_title
        else:
            seasons = self._seasons_in_view(iid)
            is_open = bool(self.tree.item(iid, "open"))
            menu.add_command(label="Hide seasons" if is_open else "Show seasons", command=lambda: self.toggle(iid))
            menu.add_command(label="Expand all", command=lambda: self.expand_all(True))
            menu.add_command(label="Collapse all", command=lambda: self.expand_all(False))
            if self.results_are_local:
                menu.add_command(label="Open file location", command=lambda: self.open_location(seasons, False))
            title = self.titles.get(iid, ("", iid[2:]))[1]
        show_id = result.group.folder_name if result is not None else iid[2:]
        if self.sonarr_configured:
            menu.add_separator()
            menu.add_command(label="Open in Sonarr", command=lambda: self.open_in_sonarr(show_id, title))
            self._add_sonarr_searches(menu, seasons, episode)
            if episode is None:
                menu.add_command(label="Delete from Sonarr...", command=self.delete_selected)
        menu.add_separator()
        for name in ui.nyaa_titles(seasons):
            menu.add_command(label=f"Search Nyaa for “{ui.nyaa_query(name)}”",
                             command=lambda n=name: self.search_nyaa(n))
        menu.add_separator()
        menu.add_command(label="Copy show name", command=lambda: self.copy_text(title))
        menu.add_command(label="Dismiss this show (hide it from every tab)",
                         command=lambda: self.dismiss_show(show_id, title))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selection_menu(self, menu: tk.Menu, selection: tuple[str, ...]) -> None:
        """The right-click menu when several rows are selected."""
        targets = ui.delete_targets(selection, self.model)
        shows = ui.plural(len(targets), "show")
        menu.add_command(label=f"Delete {shows} from Sonarr..." if targets else "Delete from Sonarr...",
                         command=self.delete_selected,
                         state="normal" if targets and self.sonarr_configured else "disabled")
        if not self.sonarr_configured:
            menu.add_command(label="(Set up Sonarr in Settings to delete shows)", state="disabled")
        menu.add_separator()
        menu.add_command(label=f"Dismiss {shows} (hide them from every tab)", state="normal" if targets else "disabled",
                         command=lambda: self.dismiss_shows([(t.folder_name, t.title) for t, _ in targets]))
        menu.add_command(label="Clear the selection", command=lambda: self.tree.selection_set(()))

    def _add_sonarr_searches(self, menu: tk.Menu, seasons: list[ShowResult], episode: FileResult | None) -> None:
        """The replacement searches: for episodes missing English (original language only), and for episodes
        missing the original language (English only). On an episode row, just that episode."""
        if episode is not None:
            english = episode.status is FileStatus.ENGLISH_ONLY
            wanted = [episode.name] if episode.status in (FileStatus.ORIGINAL_ONLY, FileStatus.ENGLISH_ONLY) else []
            menu.add_command(label="Search for a dual-audio replacement in Sonarr..." if english
                             else "Search for a replacement in Sonarr...",
                             command=lambda: self.search_sonarr(seasons, wanted, english),
                             state="normal" if wanted else "disabled")
            return
        original, english = ui.original_only_files(seasons), ui.english_only_files(seasons)
        if original or not english:
            menu.add_command(label="Search for replacements in Sonarr...",
                             command=lambda: self.search_sonarr(seasons, original),
                             state="normal" if original else "disabled")
        if english:
            menu.add_command(label="Search for dual-audio replacements of the English-only episodes in Sonarr...",
                             command=lambda: self.search_sonarr(seasons, english, english=True))

    def _seasons_in_view(self, parent_iid: str) -> list[ShowResult]:
        """The seasons listed under a show row with the tabs selected now."""
        view = self.view_tabs()
        return [s for s in self.model.by_show().get(parent_iid[2:], [])
                if ui.in_tabs(s, view) and ui.air_matches(s, self.air_filter)]

    def _focus_filter(self, _event: Any = None) -> str:
        self.search_field.focus_entry()
        self.filter_entry.select_range(0, "end")
        return "break"

    def _clear_filter(self, _event: Any = None) -> str:
        self.filter_var.set("")
        self.tree.focus_set()
        return "break"

    def copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(f"Copied “{text}”.")

    # ------------------------------------------------------------ dialogs and actions

    def show_details(self, key: str, focus: FileResult | None = None) -> None:
        """The episode window for a season, scrolled to ``focus`` when an episode was double-clicked."""
        from dubchecker.dialogs import EpisodeDetailsDialog
        result = self.model.results.get(key)
        if result is not None:
            EpisodeDetailsDialog(self, result, focus.path if focus else None)

    def fix_match(self, key: str) -> None:
        from dubchecker.dialogs import FixMatchDialog
        result = self.model.results.get(key)
        if result is not None:
            FixMatchDialog(self, result)

    def open_settings(self, tab: int = 0) -> None:
        from dubchecker.dialogs import SettingsDialog
        SettingsDialog(self, tab)

    @property
    def results_are_local(self) -> bool:
        """True when the results on screen came from a local files scan (not Sonarr)."""
        return self.results_source == "folder"

    @property
    def sonarr_configured(self) -> bool:
        return bool(self.config.sonarr_url.strip() and self.config.sonarr_api_key.strip())

    def open_location(self, seasons: list[ShowResult], select_file: bool) -> None:
        """Open the season's folder with its first episode selected, or the show's folder."""
        files = [f.path for r in seasons for f in r.files]
        self.open_location_of_file(files[0] if select_file and files else ui.shared_folder(seasons))

    def open_location_of_file(self, path: str) -> None:
        if not ui.open_file_location(path):
            messagebox.showerror(APP_NAME, f"This can't be found any more:\n{path}\n\nIs the drive connected?",
                                 parent=self.root)

    def search_nyaa(self, title: str) -> None:
        """Open a Nyaa search for '<title> dual audio' in the web browser."""
        ui.open_url(ui.nyaa_search_url(title))
        self.status_var.set(f"Opened a Nyaa search for “{ui.nyaa_query(title)}” in your browser.")

    def open_in_sonarr(self, folder_name: str, title: str) -> None:
        """Open the show's page in Sonarr in the web browser. Finding it asks Sonarr, so that's done
        in the background."""
        self.status_var.set(f"Finding {title} in Sonarr...")

        def work() -> None:
            try:
                client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key)
                url = series_page_url(client, folder_name, title)
                self.queue.put(("open_url", (url, f"Opened {title} in Sonarr in your browser.")))
            except SonarrError as exc:
                self.queue.put(("error", (str(exc),)))
            except Exception as exc:
                log.exception("Opening the show in Sonarr failed")
                self.queue.put(("error", (f"Couldn't find the show in Sonarr: {exc}",)))

        thread = threading.Thread(target=work, name="sonarr-open", daemon=True)
        self.helpers = [t for t in self.helpers if t.is_alive()] + [thread]
        thread.start()

    def dub_tags_wanted(self) -> dict[tuple[str, str], str | None]:
        """Each show's Sonarr tag, by (folder name, title). Dismissed shows are left alone."""
        return {(show_id, seasons[0].group.show_title): dub_tag(seasons)
                for show_id, seasons in self.model.by_show().items() if show_id not in self.dismissed}

    def tag_in_sonarr(self, automatic: bool = False) -> None:
        """Tag shows dual-audio, missing-dub or no-dub in Sonarr. ``automatic`` (after a scan, from
        Settings > Sonarr) asks nothing and reports on the status line only."""
        if not self.sonarr_configured or self.scanning:
            return
        if not self.model.results:
            if not automatic:
                messagebox.showinfo(APP_NAME, "Scan your library first, so Dub Checker knows how to tag each show.",
                                    parent=self.root)
            return
        wanted = self.dub_tags_wanted()
        if not automatic:
            counts = {tag: sum(1 for t in wanted.values() if t == tag)
                      for tag in (TAG_DUAL_AUDIO, TAG_MISSING_DUB, TAG_NO_DUB, TAG_ENGLISH_ONLY, TAG_DUB_PENDING,
                                  None)}
            question = (
                "Tag your shows in Sonarr?\n\n"
                f"{TAG_DUAL_AUDIO}: {ui.plural(counts[TAG_DUAL_AUDIO], 'show')} (every episode has both languages)\n"
                f"{TAG_MISSING_DUB}: {ui.plural(counts[TAG_MISSING_DUB], 'show')} (an English dub exists, but some "
                "episodes don't have it)\n"
                f"{TAG_NO_DUB}: {ui.plural(counts[TAG_NO_DUB], 'show')} (no English dub exists)\n"
                f"{TAG_ENGLISH_ONLY}: {ui.plural(counts[TAG_ENGLISH_ONLY], 'show')} (some episodes have English "
                "but not the original language)\n"
                f"{TAG_DUB_PENDING}: {ui.plural(counts[TAG_DUB_PENDING], 'show')} (the dub of the newest episodes "
                "is on its way)\n"
                f"No tag: {ui.plural(counts[None], 'show')} (something to check manually)\n\n"
                "A show whose tag changes loses its old one. Your other Sonarr tags aren't touched, and nor are "
                "dismissed shows or shows Sonarr doesn't have.")
            if not messagebox.askyesno(APP_NAME, question, parent=self.root):
                return
            self.status_var.set("Tagging your shows in Sonarr...")

        def work() -> None:
            try:
                client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key)
                self.queue.put(("notice", (apply_dub_tags(client, wanted).text(),)))
            except SonarrError as exc:
                self.queue.put(("notice" if automatic else "error", (f"Couldn't tag shows in Sonarr: {exc}",)))
            except Exception as exc:
                log.exception("Tagging in Sonarr failed")
                self.queue.put(("notice" if automatic else "error", (f"Couldn't tag shows in Sonarr: {exc}",)))

        thread = threading.Thread(target=work, name="sonarr-tags", daemon=True)
        self.helpers = [t for t in self.helpers if t.is_alive()] + [thread]
        thread.start()

    def delete_selected(self) -> None:
        """Ask how to delete the selected shows and seasons from Sonarr (the Delete key, or the right-click
        menu)."""
        if not self.sonarr_configured or self.scanning:
            if self.scanning:
                self.status_var.set("Wait for the scan to finish before deleting shows.")
            return
        targets = ui.delete_targets(self.tree.selection(), self.model)
        if targets:
            from dubchecker.dialogs import DeleteDialog
            DeleteDialog(self, targets)

    def delete_in_sonarr(self, targets: list[tuple[DeleteTarget, list[str]]], action: str) -> None:
        """Delete in the background; the deleted seasons then leave the list."""
        self.status_var.set(f"Deleting {ui.plural(len(targets), 'show')} in Sonarr...")
        keys = {target.folder_name: season_keys for target, season_keys in targets}
        if action == DELETE_SHOW:  # the whole show goes, whichever seasons were picked
            keys = {folder: [s.key for s in self.model.by_show().get(folder, [])] for folder in keys}

        def work() -> None:
            try:
                client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key)
                report = delete_from_sonarr(client, [target for target, _ in targets], action)
                gone = [key for folder in report.done for key in keys[folder]]
                self.queue.put(("deleted", (gone, report.text(), bool(report.failed))))
            except SonarrError as exc:
                self.queue.put(("error", (f"Couldn't delete in Sonarr: {exc}",)))
            except Exception as exc:
                log.exception("Deleting in Sonarr failed")
                self.queue.put(("error", (f"Couldn't delete in Sonarr: {exc}",)))

        thread = threading.Thread(target=work, name="sonarr-delete", daemon=True)
        self.helpers = [t for t in self.helpers if t.is_alive()] + [thread]
        thread.start()

    def _on_deleted(self, keys: list[str], text: str, failed: bool) -> None:
        if keys:
            self.model.remove(keys)
            self.update_cards()
            self.refresh_table()
            self.save_results()
        self.status_var.set(text.split(" Couldn't")[0] if failed else text)
        if failed:
            messagebox.showerror(APP_NAME, text, parent=self.root)

    def search_sonarr(self, seasons: list[ShowResult], files: list[str] | None = None, english: bool = False) -> None:
        """Ask Sonarr to look for new releases of episodes that only have the original-language audio, or
        (``english``) only English audio.

        ``files`` limits it to those file names (one episode); by default it's all of them in ``seasons``.
        """
        if files is None:
            files = ui.english_only_files(seasons) if english else ui.original_only_files(seasons)
        if not seasons or not files:
            return
        group = seasons[0].group
        labels = ", ".join(s.group.season_label for s in seasons if s.group.season_label)
        what = f"{group.show_title}{f' ({labels})' if labels else ''}"
        wanted = set(files)
        language = "English" if english else language_word([f for s in seasons for f in s.files if f.name in wanted])
        prefer = "dual-audio releases" if english else "dual-audio or English releases"
        episodes = f"episode “{files[0]}”" if len(files) == 1 else ui.plural(len(files), "episode")
        question = (f"Ask Sonarr to search for new releases of the {episodes} of {what} that only "
                    f"{'has' if len(files) == 1 else 'have'} {language} audio?\n\nSonarr replaces a file only when "
                    "it finds a release it rates higher under your quality profile. To make it prefer "
                    f"{prefer}, give them a higher score with a custom format in Sonarr.")
        if not messagebox.askyesno(APP_NAME, question, parent=self.root):
            return
        self.status_var.set(f"Asking Sonarr to search for {what}...")
        seasons_numbers = [s.group.season for s in seasons]

        def work() -> None:
            try:
                client = SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key)
                message = search_for_replacements(client, group.folder_name, group.show_title, seasons_numbers, files)
                self.queue.put(("notice", (message + " Check Sonarr's Activity page for what it finds.",)))
            except SonarrError as exc:
                self.queue.put(("error", (str(exc),)))
            except Exception as exc:
                log.exception("Sonarr search failed")
                self.queue.put(("error", (f"Couldn't ask Sonarr to search: {exc}",)))

        thread = threading.Thread(target=work, name="sonarr-search", daemon=True)
        self.helpers = [t for t in self.helpers if t.is_alive()] + [thread]
        thread.start()

    def save_results(self) -> None:
        """Remember what's on screen so it's still there next time. An empty list forgets it."""
        if not self.model.results:
            delete_scan(self.results_path)
            return
        scan = SavedScan(list(self.model.results.values()), self.results_source, self.results_time or time.time(),
                         self.results_stopped, sorted(self.tabs), sorted(self.expanded),
                         sorted(self.expanded_seasons))
        try:
            save_scan(self.results_path, scan)
        except OSError as exc:
            log.warning("Couldn't save the results: %s", exc)

    def _load_saved_results(self) -> None:
        saved = load_scan(self.results_path)
        if saved is None or not saved.results:
            return
        for result in saved.results:
            self.model.add(result)
        self.results_source, self.results_time, self.results_stopped = saved.source, saved.finished_at, saved.stopped
        self.expanded = set(saved.expanded)
        self.expanded_seasons = set(saved.expanded_seasons)
        self.update_cards()
        self.select_tabs({tab for tab in saved.tabs if tab in ui.TAB_IDS})
        when = time.strftime("%d %b %Y at %H:%M", time.localtime(saved.finished_at))
        early = " (stopped before it finished)" if saved.stopped else ""
        self.status_var.set(f"Showing your last scan from {when}{early}: {ui.plural(self.model.show_count(), 'show')}. "
                            "Click Scan library to check again.")

    def remove_override(self, key: str) -> None:
        self.ctx.overrides.remove(key)
        self.recheck(key)

    def recheck(self, key: str) -> None:
        """Look one season up again in the background (after Fix match)."""
        result = self.model.results.get(key)
        if result is None:
            return
        self.status_var.set(f"Checking AniList again for {result.group.display_title}...")

        def work() -> None:
            try:
                service = make_online_lookup(self.ctx.cache, self.config, self.ctx.data_dir, self.closing, None)
                new = evaluate_season(result.group, result.files, service, self.ctx.overrides.get(key),
                                      self.config.confidence_threshold, self.config.air_status_source)
                if self.config.air_status_source == "sonarr":
                    new.air = result.air  # a new AniList match doesn't change what Sonarr says
                new.rating = result.rating  # nor does it change Sonarr's rating
                self.queue.put(("replace", (new,)))
            except Exception as exc:
                log.exception("Re-check failed")
                self.queue.put(("notice", (f"Couldn't check AniList again: {exc}",)))

        thread = threading.Thread(target=work, name="recheck", daemon=True)
        self.helpers = [t for t in self.helpers if t.is_alive()] + [thread]
        thread.start()

    def _on_replaced(self, result: ShowResult) -> bool:
        if not self.model.replace(result):
            return False
        where = ui.group_title(result)
        self.status_var.set(f"Updated {result.group.display_title} - it's now in {where}.")
        self.save_results()
        return True

    def export_csv(self) -> None:
        view = self.view_tabs()
        rows = self.model.rows(view, self.filter_var.get(), self.air_filter)
        if not rows:
            messagebox.showinfo(APP_NAME, "There's nothing in this list to export.", parent=self.root)
            return
        group = self._view_heading(view)[0]
        path = filedialog.asksaveasfilename(parent=self.root, title="Export list", defaultextension=".csv",
                                            initialfile=f"Dub Checker - {group}.csv",
                                            filetypes=[("CSV file (Excel)", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                import csv  # only needed here
                writer = csv.writer(fh)
                writer.writerow(["Group", "Show", "Season", "Matched on AniList", "AniList page", "Air status",
                                 "Rating (out of 10)", "Rating from", "Episodes", "Dual audio", "Original language only", "English only", "Dub confirmed by",
                                 "Match %",
                                 "Notes", "Folder"])
                for row in rows:
                    for r in row.seasons:
                        rating = ui.rating_of(r)
                        writer.writerow([ui.group_title(r), r.group.show_title, r.group.season_label,
                                         ui.anilist_text(r), r.match.url if r.match else "", ui.air_status_text(r),
                                         f"{rating.value:.1f}" if rating else "", rating.source if rating else "",
                                         r.episodes, r.dual, r.original_only, r.english_only, ui.confirmed_text(r),
                                         ui.match_text(r),
                                         ui.notes_text(r), r.group.folder_name])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save the file:\n{exc}", parent=self.root)
            return
        self.status_var.set(f"Exported {ui.plural(sum(len(r.seasons) for r in rows), 'season')} to {path}")

    # ------------------------------------------------------------ closing

    def on_close(self) -> None:
        """Stop any scan, remember the window, and close. main() then waits for the workers."""
        self.cancel.set()
        self.stop_polling()
        if self.scanning:  # keep what was finished, marked as an unfinished scan
            self.results_time, self.results_stopped = time.time(), True
        self.save_results()
        try:
            if self.root.state() == "normal":
                self.config.window_geometry = self.root.geometry()
            self.ctx.save_config()
        except tk.TclError:
            pass
        self.root.destroy()

    def join_workers(self, timeout: float = 10.0) -> None:
        """Wait for background threads so none of them touches the database after it closes."""
        deadline = time.monotonic() + timeout
        for thread in [self.worker, *self.helpers]:
            if thread is not None and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
