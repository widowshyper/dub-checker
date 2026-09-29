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

from dubchecker import APP_NAME, __version__, theme
from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import Config, Overrides
from dubchecker.dub_sources import make_online_lookup
from dubchecker.media_probe import MediaProber
from dubchecker.models import STATUS_BY_CODE, Category, FileResult, FileStatus, ShowResult, language_word
from dubchecker.pipeline import ScanOutcome, ScanPipeline, evaluate_season
from dubchecker.results_store import SavedScan, delete_scan, load_scan, save_scan
from dubchecker.sonarr import SonarrClient, SonarrError, search_for_replacements

log = logging.getLogger(__name__)

READY_TEXT = "Ready."
# Solid pointers: easy to see and hit, and part of Segoe UI itself. (Chevrons and the small triangles
# rely on Tk borrowing the glyph from another font, which can show as a box instead.)
ARROW_CLOSED, ARROW_OPEN = "►", "▼"
TOOLTIP_DELAY_MS = 550
REFRESH_INTERVAL = 0.5  # seconds between table rebuilds while a scan is adding results
# (id, heading, width at 100 %, stretches)
COLUMNS = (
    ("show", "Show", 250, True),
    ("anilist", "Matched on AniList", 280, False),
    ("episodes", "Episodes", 75, False),
    ("dual", "Dual audio", 95, False),
    ("orig", "Original only", 110, False),
    ("confirmed", "Dub confirmed by", 190, False),
    ("match", "Match %", 75, False),
    ("notes", "Notes", 200, True),
)
NUMERIC_COLUMNS = frozenset({"audio", "episodes", "dual", "orig", "match"})
BAR_WIDTH, BAR_HEIGHT = 96, 11  # the audio bar in the first column, at 100 % scaling


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

    def _color(self, status: FileStatus) -> str:
        return self.app.palette[ui.STATUS_COLORS[status]]

    def bar(self, code: str) -> tk.PhotoImage:
        """``code`` has one letter per episode (``ShowResult.audio_code``); equal codes share one image."""
        key = ("bar", code)
        if key not in self._images:
            width, height = self.app.px(BAR_WIDTH), self.app.px(BAR_HEIGHT)
            image = self._new(width, height)
            count = len(code)
            gap = 1 if count and width / count >= 4 else 0  # separate the blocks when there's room
            for number, letter in enumerate(code):
                left = round(number * width / count)
                right = max(left + 1, round((number + 1) * width / count) - gap)
                image.put(self._color(STATUS_BY_CODE[letter]), to=(left, 0, right, height))
            self._images[key] = image
        return self._images[key]

    def square(self, status: FileStatus) -> tk.PhotoImage:
        key = ("square", status)
        if key not in self._images:
            size = self.app.px(BAR_HEIGHT)
            image = self._new(size, size)
            image.put(self._color(status), to=(0, 0, size, size))
            self._images[key] = image
        return self._images[key]


class CellTooltip:
    """Shows the full text of a table cell that's too narrow for it, after the pointer rests there."""

    def __init__(self, tree: ttk.Treeview, app: App) -> None:
        self.tree = tree
        self.app = app
        self.cell: tuple[str, str] | None = None
        self.pending: str | None = None
        self.window: tk.Toplevel | None = None

    def track(self, event: Any) -> None:
        cell = (self.tree.identify_row(event.y), self.tree.identify_column(event.x))
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

    def save_config(self) -> None:
        try:
            self.config.save(self.config_path)
        except OSError as exc:
            log.warning("Couldn't save settings: %s", exc)


class SummaryCard(tk.Frame):
    """One of the four clickable group cards that act as the tabs."""

    def __init__(self, app: App, parent: tk.Misc, category: Category) -> None:
        super().__init__(parent, highlightthickness=max(1, round(app.scale)), cursor="hand2")
        self.app = app
        self.category = category
        self.info = ui.GROUPS[category]
        self.hover = False
        self.selected = False
        pad = round(14 * app.scale)
        self.stripe = tk.Frame(self, height=round(4 * app.scale))
        self.stripe.pack(fill="x", side="top")
        self.count = tk.Label(self, text="-", font="SunValleyTitleFont", anchor="w")
        self.count.pack(fill="x", padx=pad, pady=(round(8 * app.scale), 0))
        self.title = tk.Label(self, text=self.info.title, font="SunValleyBodyStrongFont", anchor="w")
        self.title.pack(fill="x", padx=pad)
        self.caption = tk.Label(self, text="shows", font="SunValleyCaptionFont", anchor="w")
        self.caption.pack(fill="x", padx=pad, pady=(0, round(10 * app.scale)))
        for widget in (self, self.stripe, self.count, self.title, self.caption):
            widget.bind("<Button-1>", lambda _e: app.select_group(category))
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _on_enter(self, _event: Any) -> None:
        self.hover = True
        self.recolor()

    def _on_leave(self, _event: Any) -> None:
        widget = self.winfo_containing(*self.winfo_pointerxy())
        if widget is not None and str(widget).startswith(str(self)):
            return  # moved onto one of our own labels
        self.hover = False
        self.recolor()

    def set_count(self, value: int | None) -> None:
        self.count.configure(text="-" if value is None else f"{value:,}")
        self.caption.configure(text="show" if value == 1 else "shows")

    def recolor(self) -> None:
        pal = self.app.palette
        color = pal[self.info.color]
        bg = pal["card_selected"] if self.selected else pal["card_hover"] if self.hover else pal["card"]
        self.configure(background=bg, highlightbackground=color if self.selected else pal["border"],
                       highlightcolor=color if self.selected else pal["border"])
        self.stripe.configure(background=color)
        self.count.configure(background=bg, foreground=color)
        self.title.configure(background=bg, foreground=pal["fg"])
        self.caption.configure(background=bg, foreground=pal["muted"])


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
        self.model = ui.ResultsModel()
        self.current = Category.NEEDS_DUB
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

        root.title(APP_NAME)
        self._set_icon()
        self.palette = theme.apply_theme(root, self.config.theme)
        self._build()
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
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        pad = self.px(18)
        self._build_header().grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, self.px(10)))
        self._build_source_card().grid(row=1, column=0, sticky="ew", padx=pad)
        self._build_cards().grid(row=2, column=0, sticky="ew", padx=pad, pady=(self.px(14), self.px(10)))
        self._build_toolbar().grid(row=3, column=0, sticky="ew", padx=pad, pady=(0, self.px(6)))
        self._build_table().grid(row=4, column=0, sticky="nsew", padx=pad)
        self._build_footer().grid(row=5, column=0, sticky="ew", padx=pad, pady=(self.px(6), self.px(10)))

    def _build_header(self) -> ttk.Frame:
        header = ttk.Frame(self.root)
        header.columnconfigure(1, weight=1)
        try:
            full = tk.PhotoImage(file=str(ui.asset_path("icon_256.png")))
            self.logo = full.subsample(max(1, round(256 / (48 * self.scale))))
            ttk.Label(header, image=self.logo).grid(row=0, column=0, rowspan=2, padx=(0, self.px(12)))
        except tk.TclError as exc:
            log.warning("Couldn't load the logo: %s", exc)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=1, sticky="sw")
        ttk.Label(header, text="Find the anime in your library that's missing English dub audio.",
                  style="Muted.TLabel").grid(row=1, column=1, sticky="nw")
        ttk.Button(header, text="Settings", command=self.open_settings).grid(row=0, column=2, rowspan=2, sticky="e")
        return header

    def _card_frame(self, parent: tk.Misc) -> tk.Frame:
        frame = tk.Frame(parent, highlightthickness=max(1, round(self.scale)))
        self._card_frames.append(frame)
        return frame

    def _build_source_card(self) -> tk.Frame:
        self._card_frames: list[tk.Frame] = []
        card = self._card_frame(self.root)
        inner = self._card_frame(card)
        inner.configure(highlightthickness=0)
        inner.pack(fill="x", padx=self.px(16), pady=self.px(14))
        inner.columnconfigure(0, weight=1)

        row = self._card_frame(inner)
        row.configure(highlightthickness=0)
        row.grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value=self.config.scan_source)
        ttk.Label(row, text="Scan from:", style="Card.TLabel").pack(side="left", padx=(0, self.px(10)))
        self.source_buttons = [
            ttk.Radiobutton(row, text="Local files", value="folder", variable=self.source_var,
                            style="Card.TRadiobutton", command=self._on_source_change),
            ttk.Radiobutton(row, text="Sonarr", value="sonarr", variable=self.source_var,
                            style="Card.TRadiobutton", command=self._on_source_change),
        ]
        for button in self.source_buttons:
            button.pack(side="left", padx=(0, self.px(14)))

        self.folder_row = self._card_frame(inner)
        self.folder_row.configure(highlightthickness=0)
        self.folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=self.config.library_path)
        self.folder_entry = ttk.Entry(self.folder_row, textvariable=self.folder_var, style="Card.TEntry")
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, self.px(8)))
        self.folder_entry.bind("<Return>", lambda _e: self.start_scan())
        self.browse_button = ttk.Button(self.folder_row, text="Browse...", style="Card.TButton",
                                        command=self.browse_folder)
        self.browse_button.grid(row=0, column=1)

        self.sonarr_row = self._card_frame(inner)
        self.sonarr_row.configure(highlightthickness=0)
        self.sonarr_label = ttk.Label(self.sonarr_row, style="Card.TLabel")
        self.sonarr_label.pack(side="left", padx=(0, self.px(12)))
        ttk.Button(self.sonarr_row, text="Sonarr settings...", style="Card.TButton",
                   command=lambda: self.open_settings(tab=2)).pack(side="left")

        buttons = self._card_frame(inner)
        buttons.configure(highlightthickness=0)
        buttons.grid(row=2, column=0, sticky="w", pady=(self.px(12), 0))
        self.scan_button = ttk.Button(buttons, text="Scan library", style="Card.Accent.TButton",
                                      command=self.start_scan)
        self.scan_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", style="Card.TButton", command=self.stop_scan)
        self.stop_button.pack(side="left", padx=(self.px(8), 0))
        self.clear_button = ttk.Button(buttons, text="Clear results", style="Card.TButton",
                                       command=self.clear_results)
        self.clear_button.pack(side="left", padx=(self.px(8), 0))

        self.progress = ttk.Progressbar(inner, style="Card.Horizontal.TProgressbar", maximum=100)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(self.px(12), 0))
        self.status_var = tk.StringVar(value=READY_TEXT)
        ttk.Label(inner, textvariable=self.status_var, style="Card.Muted.TLabel").grid(
            row=4, column=0, sticky="ew", pady=(self.px(8), 0))
        self._on_source_change(save=False)
        return card

    def _build_cards(self) -> ttk.Frame:
        frame = ttk.Frame(self.root)
        self.cards: dict[Category, SummaryCard] = {}
        for column, category in enumerate(Category):
            frame.columnconfigure(column, weight=1, uniform="cards")
            card = SummaryCard(self, frame, category)
            card.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else self.px(6), 0 if column == 3 else self.px(6)))
            self.cards[category] = card
        return frame

    def _build_toolbar(self) -> ttk.Frame:
        bar = ttk.Frame(self.root)
        bar.columnconfigure(0, weight=1)
        self.group_title = ttk.Label(bar, style="Subtitle.TLabel")
        self.group_title.grid(row=0, column=0, sticky="w")
        self.group_description = ttk.Label(bar, style="Muted.TLabel")
        self.group_description.grid(row=1, column=0, sticky="w")
        tools = ttk.Frame(bar)
        tools.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(tools, text="Filter:").pack(side="left", padx=(0, self.px(6)))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(tools, textvariable=self.filter_var, width=22)
        self.filter_entry.pack(side="left", padx=(0, self.px(12)))
        self.filter_entry.bind("<Escape>", self._clear_filter)
        self.filter_var.trace_add("write", lambda *_: self.refresh_table())
        ttk.Button(tools, text="Expand all", command=lambda: self.expand_all(True)).pack(side="left")
        ttk.Button(tools, text="Collapse all", command=lambda: self.expand_all(False)).pack(
            side="left", padx=(self.px(6), 0))
        ttk.Button(tools, text="Export list...", command=self.export_csv).pack(side="left", padx=(self.px(6), 0))

        legend = ttk.Frame(bar)
        legend.grid(row=2, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0))
        ttk.Label(legend, text="Audio key:", style="Caption.TLabel").pack(side="left", padx=(0, self.px(8)))
        self.legend_labels: list[tuple[ttk.Label, FileStatus]] = []
        for status, text in ui.LEGEND:
            label = ttk.Label(legend, text=text, compound="left", style="Caption.TLabel")
            label.pack(side="left", padx=(0, self.px(14)))
            self.legend_labels.append((label, status))
        return bar

    def _build_table(self) -> ttk.Frame:
        frame = ttk.Frame(self.root)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show=("tree", "headings"),
                                 selectmode="browse", style="Results.Treeview")
        self.tree.heading("#0", text="Audio", anchor="w", command=lambda: self.sort_by("audio"))
        self.tree.column("#0", width=self.px(BAR_WIDTH + 18), minwidth=self.px(BAR_WIDTH + 18), stretch=False)
        for column, heading, width, stretch in COLUMNS:
            self.tree.heading(column, text=heading, anchor="w", command=lambda c=column: self.sort_by(c))
            self.tree.column(column, width=self.px(width), minwidth=self.px(min(width, 70)), stretch=stretch,
                             anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
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
        self.tree.bind("<Button-3>", self._on_right_click)
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._on_right_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open_close)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_open_close)

        self.empty_label = tk.Label(frame, font="SunValleyBodyLargeFont", wraplength=self.px(560), justify="center")
        self._update_headings()
        return frame

    def _build_footer(self) -> ttk.Frame:
        footer = ttk.Frame(self.root)
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, style="Caption.TLabel",
                  text="Tip: click a row's arrow or audio bar (or double-click it) to open it; double-click an "
                       "episode for its audio tracks; right-click for more. F5 scans, Esc stops.").grid(
            row=0, column=0, sticky="w")
        ttk.Label(footer, text=f"Version {__version__}", style="Caption.TLabel").grid(row=0, column=1, sticky="e")
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
            frame.configure(background=pal["card"], highlightbackground=pal["border"], highlightcolor=pal["border"])
        for card in self.cards.values():
            card.selected = card.category is self.current
            card.recolor()
        self.tree.tag_configure("hover", background=pal["row_hover"])
        self.tree.tag_configure("even", background=pal["row_even"])
        self.tree.tag_configure("odd", background=pal["row_odd"])
        for status, color in ui.STATUS_COLORS.items():  # episode rows are coloured by their audio
            self.tree.tag_configure(f"audio_{status.name}", foreground=pal[color])
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
            self.sonarr_row.grid(row=1, column=0, sticky="ew", pady=(self.px(10), 0))
        else:
            self.sonarr_row.grid_remove()
            self.folder_row.grid(row=1, column=0, sticky="ew", pady=(self.px(10), 0))
        self.update_sonarr_summary()
        if save and source != self.config.scan_source:
            self.config.scan_source = source
            self.ctx.save_config()
            if not self.model.results:
                self._update_empty_state([])

    def update_sonarr_summary(self) -> None:
        url = self.config.sonarr_url.strip() or "not set up yet"
        key = "" if self.config.sonarr_api_key.strip() else " (no API key yet)"
        self.sonarr_label.configure(text=f"Sonarr: {url}{key}")

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
        self.browse_button.state(busy)
        self.folder_entry.state(busy)
        for button in self.source_buttons:
            button.state(busy)
        self.stop_button.state(idle)

    def start_scan(self) -> None:
        if self.scanning:
            return
        source = self.source_var.get()
        path = ""
        if source == "folder":
            path = self.folder_var.get().strip().strip('"')
            if not path:
                messagebox.showinfo(APP_NAME, "Choose your anime folder first (click Browse...).", parent=self.root)
                return
            if not os.path.isdir(path):
                messagebox.showerror(APP_NAME, f"The folder\n{path}\ncan't be found. Is the drive connected?",
                                     parent=self.root)
                return
            self.config.library_path = path
        elif not self.config.sonarr_url.strip() or not self.config.sonarr_api_key.strip():
            messagebox.showinfo(APP_NAME, "Enter Sonarr's address and API key first.", parent=self.root)
            self.open_settings(tab=2)
            return
        self.config.scan_source = source
        self.ctx.save_config()

        self.model.clear()
        self.results_source = source
        self.cancel = threading.Event()
        self._set_scanning(True)
        self._show_progress("Starting...", None, True)
        self.update_cards()
        self.refresh_table()
        self.worker = threading.Thread(target=self._scan_worker, args=(source, path, self.cancel),
                                       name="scan", daemon=True)
        self.worker.start()

    def _scan_worker(self, source: str, path: str, cancel: threading.Event) -> None:
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
                pipeline.run(sonarr=SonarrClient(self.config.sonarr_url, self.config.sonarr_api_key))
            else:
                pipeline.run(root=path)
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

    def _poll(self) -> None:
        changed = False
        progress = None
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    progress = payload
                elif kind in ("results", "result"):
                    items = payload[0] if kind == "results" else [payload[0]]
                    for result in items:
                        self.model.add(result)
                    changed = changed or bool(items)
                elif kind == "replace":
                    changed = self._on_replaced(payload[0]) or changed
                elif kind == "notice":
                    self.status_var.set(payload[0])
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
            for pending in (self._poll_id, self._refresh_id):
                if pending is not None:
                    self.root.after_cancel(pending)
            self._refresh_id = None
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
        shows = self.model.show_count()
        if outcome.error:
            self.status_var.set("The scan couldn't finish.")
            messagebox.showerror(APP_NAME, outcome.error, parent=self.root)
        elif outcome.stopped:
            self.status_var.set(f"Stopped - kept the {ui.plural(shows, 'show')} finished so far "
                                f"{outcome.stats.short_text()}")
        else:
            self.status_var.set(f"Done - checked {ui.plural(shows, 'show')} in "
                                f"{ui.format_duration(outcome.elapsed)} {outcome.stats.short_text()}")
        self.update_cards()
        self.refresh_table()
        self.results_time, self.results_stopped = time.time(), outcome.stopped
        self.save_results()
        for warning in outcome.warnings:
            messagebox.showwarning(APP_NAME, warning, parent=self.root)

    # ------------------------------------------------------------ groups and table

    def select_group(self, category: Category) -> None:
        self.current = category
        for card in self.cards.values():
            card.selected = card.category is category
            card.recolor()
        self.refresh_table()

    def update_cards(self) -> None:
        counts = self.model.counts() if (self.model.results or self.scanning) else None
        for category, card in self.cards.items():
            card.set_count(None if counts is None else counts[category])

    @staticmethod
    def _label(indent: str, arrow: bool, is_open: bool, text: str) -> str:
        if not arrow:
            return f"{indent}{text}"
        return f"{indent}{ARROW_OPEN if is_open else ARROW_CLOSED}  {text}"

    def _season_values(self, r: ShowResult, label: str) -> tuple:
        return (label, ui.anilist_text(r), r.episodes, r.dual, r.original_only, ui.confirmed_text(r),
                ui.match_text(r), ui.notes_text(r))

    def _parent_values(self, row: ui.ShowRow, label: str) -> tuple:
        sources: list[str] = []
        for s in row.seasons:
            for source in (s.dub.sources if s.dub else []):
                if source not in sources:
                    sources.append(source)
        return (label, row.seasons_text(), row.total("episodes"), row.total("dual"),
                row.total("original_only"), ", ".join(sources) or "-", "", row.also_text(self.current))

    @staticmethod
    def _episode_values(f: FileResult, label: str) -> tuple:
        tick = "✓"
        return (label, "", "", tick if f.status is FileStatus.DUAL else "",
                tick if f.status is FileStatus.ORIGINAL_ONLY else "", "", "", ui.episode_note(f))

    def _sort_value(self, row: ui.ShowRow, column: str) -> Any:
        first = row.seasons[0]
        if column == "audio":  # share of episodes that still need English audio
            return row.total("original_only") / max(1, row.total("episodes"))
        if column == "episodes":
            return row.total("episodes")
        if column == "dual":
            return row.total("dual")
        if column == "orig":
            return row.total("original_only")
        if column == "match":
            return min((s.match.confidence if s.match else -1.0) for s in row.seasons)
        if column == "anilist":
            return ui.anilist_text(first).casefold()
        if column == "confirmed":
            return ui.confirmed_text(first).casefold()
        if column == "notes":
            return ui.notes_text(first).casefold()
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

        info = ui.GROUPS[self.current]
        self.group_title.configure(text=info.title)
        self.group_description.configure(text=info.description)

        rows = self.model.rows(self.current, self.filter_var.get())
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
                             (stripe, "parent"), self.bars.bar("".join(r.audio_code for r in row.seasons)), is_open)
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

    def _update_empty_state(self, rows: list) -> None:
        if rows:
            self.empty_label.place_forget()
            return
        if not self.model.results:
            text = "Scanning..." if self.scanning else ui.EMPTY_BEFORE_SCAN[self.source_var.get()]
        elif self.filter_var.get().strip() and self.model.rows(self.current):
            text = f"No shows match “{self.filter_var.get().strip()}”."
        elif self.scanning:
            text = "Scanning..."
        else:
            text = ui.GROUPS[self.current].empty
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
                         (stripe, "child") if parent else (stripe,), self.bars.bar(r.audio_code), is_open)
        if is_open:
            self._insert_episodes(iid, r)

    def _insert_episodes(self, season_iid: str, r: ShowResult) -> None:
        if self.tree.get_children(season_iid):
            return
        indent = self.titles.get(season_iid, ("", ""))[0] + ui.INDENT + "  "  # line up after the arrow
        stripe = next(t for t in self.tree.item(season_iid, "tags") if t in ("even", "odd"))
        for number, f in enumerate(r.files):
            self._insert_row(season_iid, f"e|{r.key}|{number}", self._episode_values(f, indent + ui.episode_label(f)),
                             (stripe, f"audio_{f.status.name}"), self.bars.square(f.status))

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
        column = tree.identify_column(event.x)
        if column == "#0":
            return iid
        if column == "#1":
            bbox = tree.bbox(iid, "#1")
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
        if iid.startswith("s|") and tree.identify_column(event.x) == "#2" and result and result.match:
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
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        menu = tk.Menu(self.root, tearoff=False)
        result = self._result_for(iid)
        files: list[str] | None = None  # None = all of the Japanese/Korean/Chinese-only episodes in view
        if (episode := self._file_for(iid)) is not None and result is not None:
            key = result.key
            seasons = [result]
            files = [episode.name] if episode.status is FileStatus.ORIGINAL_ONLY else []
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
        if self.sonarr_configured:
            wanted = ui.original_only_files(seasons) if files is None else files
            label = "Search for a replacement in Sonarr..." if files is not None else \
                "Search for replacements in Sonarr..."
            menu.add_separator()
            menu.add_command(label=label, command=lambda: self.search_sonarr(seasons, wanted),
                             state="normal" if wanted else "disabled")
        menu.add_separator()
        for name in ui.nyaa_titles(seasons):
            menu.add_command(label=f"Search Nyaa for “{ui.nyaa_query(name)}”",
                             command=lambda n=name: self.search_nyaa(n))
        menu.add_separator()
        menu.add_command(label="Copy show name", command=lambda: self.copy_text(title))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _seasons_in_view(self, parent_iid: str) -> list[ShowResult]:
        """The seasons listed under a show row in the current group."""
        return [s for s in self.model.by_show().get(parent_iid[2:], []) if s.category is self.current]

    def _focus_filter(self, _event: Any = None) -> str:
        self.filter_entry.focus_set()
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

    def search_sonarr(self, seasons: list[ShowResult], files: list[str] | None = None) -> None:
        """Ask Sonarr to look for new releases of episodes that only have the original-language audio.

        ``files`` limits it to those file names (one episode); by default it's all of them in ``seasons``.
        """
        files = ui.original_only_files(seasons) if files is None else files
        if not seasons or not files:
            return
        group = seasons[0].group
        labels = ", ".join(s.group.season_label for s in seasons if s.group.season_label)
        what = f"{group.show_title}{f' ({labels})' if labels else ''}"
        wanted = set(files)
        language = language_word([f for s in seasons for f in s.files if f.name in wanted])
        episodes = f"episode “{files[0]}”" if len(files) == 1 else ui.plural(len(files), "episode")
        question = (f"Ask Sonarr to search for new releases of the {episodes} of {what} that only "
                    f"{'has' if len(files) == 1 else 'have'} {language} audio?\n\nSonarr replaces a file only when "
                    "it finds a release it rates "
                    "higher under your quality profile. To make it prefer dual-audio or English releases, give them "
                    "a higher score with a custom format in Sonarr.")
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
                         self.results_stopped, self.current.name, sorted(self.expanded),
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
        self.select_group(Category[saved.group])
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
                                      self.config.confidence_threshold)
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
        where = ui.GROUPS[result.category].title
        self.status_var.set(f"Updated {result.group.display_title} - it's now in {where}.")
        self.save_results()
        return True

    def export_csv(self) -> None:
        rows = self.model.rows(self.current, self.filter_var.get())
        if not rows:
            messagebox.showinfo(APP_NAME, "There's nothing in this list to export.", parent=self.root)
            return
        group = ui.GROUPS[self.current].title
        path = filedialog.asksaveasfilename(parent=self.root, title="Export list", defaultextension=".csv",
                                            initialfile=f"Dub Checker - {group}.csv",
                                            filetypes=[("CSV file (Excel)", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                import csv  # only needed here
                writer = csv.writer(fh)
                writer.writerow(["Group", "Show", "Season", "Matched on AniList", "AniList page", "Episodes",
                                 "Dual audio", "Original language only", "Dub confirmed by", "Match %", "Notes",
                                 "Folder"])
                for row in rows:
                    for r in row.seasons:
                        writer.writerow([group, r.group.show_title, r.group.season_label, ui.anilist_text(r),
                                         r.match.url if r.match else "", r.episodes, r.dual, r.original_only,
                                         ui.confirmed_text(r), ui.match_text(r), ui.notes_text(r),
                                         r.group.folder_name])
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
