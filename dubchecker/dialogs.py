"""Episode details, Dismissed shows, Fix match and Settings windows."""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from dubchecker import APP_NAME, dpapi, theme
from dubchecker import ui_common as ui
from dubchecker.anilist import parse_anilist_id
from dubchecker.models import FileStatus, ShowResult
from dubchecker.sonarr import SonarrClient, SonarrError, connection_summary

if TYPE_CHECKING:
    from dubchecker.gui import App

log = logging.getLogger(__name__)


class Dialog(tk.Toplevel):
    """A themed child window: Esc closes it; modal ones grab input."""

    def __init__(self, app: App, title: str, modal: bool = True) -> None:
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.modal = modal
        self.title(title)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def px(self, value: float) -> int:
        return self.app.px(value)

    def present(self, width: int, height: int) -> None:
        """Size (fitting the screen), centre over the main window and show."""
        root = self.app.root
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.px(width), screen_w - 40), min(self.px(height), screen_h - 80)
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 3
        x, y = max(0, min(x, screen_w - w)), max(0, min(y, screen_h - h - 40))
        self.geometry(f"{w}x{h}+{x}+{y}")
        theme.set_title_bar(self, self.app.palette["mode"] == "dark")
        self.deiconify()
        if self.modal:
            self.grab_set()
        self.focus_set()

    def close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def link(self, parent: tk.Misc, text: str, url: str) -> tk.Label:
        font = tkfont.Font(font="SunValleyBodyFont")
        font.configure(underline=True)
        label = tk.Label(parent, text=text, font=font, cursor="hand2", anchor="w", padx=0, pady=0, borderwidth=0,
                         background=self.app.palette["bg"], foreground=self.app.palette["link"])
        label.bind("<Button-1>", lambda _e: ui.open_url(url))
        label._font = font  # type: ignore[attr-defined]  # keep a reference
        return label


# ---------------------------------------------------------------- episode details

class EpisodeDetailsDialog(Dialog):
    COLUMNS = (("item", "File / audio track", 360), ("tag", "Language tag", 110), ("understood", "Understood as", 150),
               ("name", "Track name", 200), ("format", "Format", 90), ("channels", "Channels", 80))

    def __init__(self, app: App, result: ShowResult, focus_path: str | None = None) -> None:
        super().__init__(app, f"{result.group.display_title} - {APP_NAME}", modal=False)
        self.result = result
        focus_row = ""
        pal = app.palette
        frame = ttk.Frame(self, padding=self.px(18))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=result.group.display_title, style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        info = ui.GROUPS[result.category]
        tk.Label(top, text=f"  {info.title}  ", font="SunValleyBodyStrongFont", background=pal[info.color],
                 foreground=pal["on_color"]).grid(row=0, column=1, sticky="e")

        facts = ttk.Frame(frame)
        facts.grid(row=1, column=0, sticky="ew", pady=(self.px(10), self.px(12)))
        facts.columnconfigure(1, weight=1)
        rows: list[tuple[str, Any]] = []
        if result.match:
            rows.append(("AniList:", self.link(facts, f"{result.match.title}  ({result.match.url})", result.match.url)))
        else:
            rows.append(("AniList:", ui.anilist_text(result)))
        rows.append(("Match:", ui.match_info_text(result)))
        rows.append(("English dub:", ui.dub_status_text(result)))
        if result.notes:
            rows.append(("Notes:", ui.notes_text(result)))
        for number, (label, value) in enumerate(rows):
            ttk.Label(facts, text=label, style="Strong.TLabel").grid(row=number, column=0, sticky="nw",
                                                                    padx=(0, self.px(10)), pady=self.px(1))
            if isinstance(value, str):
                value = ttk.Label(facts, text=value, wraplength=self.px(760))
            value.grid(row=number, column=1, sticky="w", pady=self.px(1))

        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show=("tree", "headings"),
                            selectmode="browse", style="Results.Treeview")
        tree.column("#0", width=self.px(30), minwidth=self.px(30), stretch=False)  # the colour square
        for column, heading, width in self.COLUMNS:
            tree.heading(column, text=heading, anchor="w")
            tree.column(column, width=self.px(width), stretch=column in ("item", "name"), anchor="w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        for status, color in ui.STATUS_COLORS.items():
            tree.tag_configure(status.name, foreground=pal[color], font="SunValleyBodyStrongFont")
        tree.tag_configure("track", foreground=pal["fg"])

        self.tree = tree
        self.row_files: dict[str, str] = {}  # tree row -> the file it belongs to
        for f in result.files:
            detail = f.error or f.note or ("Read from Sonarr" if f.source == "sonarr" else "")
            row = tree.insert("", "end", image=app.bars.square(f.status), values=(f.name, "", f.status_text, detail,
                                                                               "", ""), tags=(f.status.name,))
            self.row_files[row] = f.path
            if f.path == focus_path:
                focus_row = row
            for number, track in enumerate(f.tracks, 1):
                understood = track.lang.value + (" (ignored - commentary)" if track.ignored else "")
                self.row_files[tree.insert("", "end", tags=("track",), values=(
                    f"{ui.INDENT}Audio track {number}", track.language or "(none)", understood, track.title,
                    track.codec, track.channels))] = f.path
            if not f.tracks and f.status is not FileStatus.ERROR and not f.note:
                tree.insert("", "end", tags=("track",), values=(f"{ui.INDENT}No audio tracks", "", "", "", "", ""))

        if app.results_are_local:
            tree.bind("<Button-3>", self._on_right_click)
            if sys.platform == "darwin":
                tree.bind("<Button-2>", self._on_right_click)

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="e", pady=(self.px(12), 0))
        ttk.Button(buttons, text="Search Nyaa for dual audio",
                   command=lambda: app.search_nyaa(ui.nyaa_titles([result])[0])).pack(side="left", padx=(0, self.px(8)))
        if app.sonarr_configured and ui.original_only_files([result]):
            ttk.Button(buttons, text="Search for replacements in Sonarr...",
                       command=lambda: app.search_sonarr([result])).pack(side="left", padx=(0, self.px(8)))
        ttk.Button(buttons, text="Fix match...", command=self._fix).pack(side="left")
        open_button = ttk.Button(buttons, text="Open AniList page",
                                 command=lambda: result.match and ui.open_url(result.match.url))
        open_button.pack(side="left", padx=(self.px(8), 0))
        if not result.match:
            open_button.state(("disabled",))
        ttk.Button(buttons, text="Close", style="Accent.TButton", command=self.close).pack(
            side="left", padx=(self.px(8), 0))
        self.present(1100, 640)
        if focus_row:  # opened from an episode row: start at that file
            tree.selection_set(focus_row)
            tree.focus(focus_row)
            tree.see(focus_row)

    def _on_right_click(self, event: Any) -> None:
        row = self.tree.identify_row(event.y)
        path = self.row_files.get(row)
        if not path:
            return
        self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Open file location", command=lambda: self.app.open_location_of_file(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _fix(self) -> None:
        key = self.result.key
        self.close()
        self.app.fix_match(key)


# ---------------------------------------------------------------- dismissed shows

class DismissedDialog(Dialog):
    """The shows you've dismissed, with a way to bring them back."""

    COLUMNS = (("show", "Show", 300), ("where", "In your last scan", 300), ("when", "Dismissed on", 140))

    def __init__(self, app: App) -> None:
        super().__init__(app, f"Dismissed shows - {APP_NAME}")
        frame = ttk.Frame(self, padding=self.px(18))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        ttk.Label(frame, text="Dismissed shows", style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, style="Muted.TLabel", wraplength=self.px(700),
                  text="These are hidden from every tab and the list, and stay hidden after a rescan. Select one or "
                       "more and click Bring back to list them again.").grid(
            row=1, column=0, sticky="w", pady=(self.px(4), self.px(12)))

        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="extended")
        for column, heading, width in self.COLUMNS:
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=self.px(width), stretch=column != "when", anchor="w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Double-Button-1>", lambda _e: self._restore(list(self.tree.selection())))
        self.empty = ttk.Label(table, text="No shows are dismissed. Right-click a show in the list and choose "
                                           "Dismiss this show to hide it.", style="Muted.TLabel")

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="e", pady=(self.px(12), 0))
        self.restore_button = ttk.Button(buttons, text="Bring back selected",
                                         command=lambda: self._restore(list(self.tree.selection())))
        self.restore_button.pack(side="left")
        self.restore_all_button = ttk.Button(buttons, text="Bring back all",
                                             command=lambda: self._restore(list(self.tree.get_children())))
        self.restore_all_button.pack(side="left", padx=(self.px(8), 0))
        ttk.Button(buttons, text="Close", style="Accent.TButton", command=self.close).pack(
            side="left", padx=(self.px(8), 0))
        self._fill()
        self.present(820, 520)

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        shows = self.app.model.by_show()
        for folder, info in self.app.ctx.dismissed.items():
            seasons = shows.get(folder, [])
            groups = list(dict.fromkeys(s.category.value for s in seasons))
            where = ", ".join(groups) if groups else "Not in the current results"
            when = time.strftime("%d %b %Y", time.localtime(info["dismissed_at"])) if info["dismissed_at"] else ""
            self.tree.insert("", "end", iid=folder, values=(info["title"], where, when))
        if self.tree.get_children():
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.restore_button.state(("!disabled",) if self.tree.selection() else ("disabled",))
        self.restore_all_button.state(("!disabled",) if self.tree.get_children() else ("disabled",))

    def _restore(self, folders: list[str]) -> None:
        if folders:
            self.app.restore_shows(folders)
            self._fill()



# ---------------------------------------------------------------- fix match

class FixMatchDialog(Dialog):
    def __init__(self, app: App, result: ShowResult) -> None:
        super().__init__(app, f"Fix match - {APP_NAME}")
        self.result = result
        self.key = result.key
        frame = ttk.Frame(self, padding=self.px(20))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        wrap = self.px(520)

        ttk.Label(frame, text="Which AniList entry is this?", style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=result.group.display_title, style="Muted.TLabel", wraplength=wrap).grid(
            row=1, column=0, sticky="w", pady=(0, self.px(4)))
        if result.match:
            ttk.Label(frame, text=f"Currently matched to: {result.match.title}", wraplength=wrap).grid(
                row=2, column=0, sticky="w")

        ttk.Label(frame, text="1.  Find the right show on AniList.", style="Strong.TLabel").grid(
            row=3, column=0, sticky="w", pady=(self.px(16), self.px(4)))
        search = result.group.show_title
        ttk.Button(frame, text=f"Search AniList for “{search}”",
                   command=lambda: ui.open_url(f"https://anilist.co/search/anime?search={quote(search)}")).grid(
            row=4, column=0, sticky="w")

        ttk.Label(frame, text="2.  Copy the address of its page and paste it here (or just its ID number).",
                  style="Strong.TLabel", wraplength=wrap).grid(row=5, column=0, sticky="w",
                                                               pady=(self.px(16), self.px(4)))
        self.value = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.value)
        entry.grid(row=6, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self._save())
        self.feedback = ttk.Label(frame, text=" ", wraplength=wrap)
        self.feedback.grid(row=7, column=0, sticky="w", pady=(self.px(4), 0))
        self.value.trace_add("write", lambda *_: self._validate())

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, sticky="ew", pady=(self.px(20), 0))
        buttons.columnconfigure(0, weight=1)
        if self.key in app.ctx.overrides:
            ttk.Button(buttons, text="Remove my manual match", command=self._remove).grid(row=0, column=0, sticky="w")
        self.save_button = ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save)
        self.save_button.grid(row=0, column=1, padx=(0, self.px(8)))
        ttk.Button(buttons, text="Cancel", command=self.close).grid(row=0, column=2)
        self._validate()
        self.present(600, 370)
        entry.focus_set()

    def _validate(self) -> int | None:
        text = self.value.get().strip()
        anilist_id = parse_anilist_id(text)
        if not text:
            self.feedback.configure(text=" ", style="TLabel")
        elif anilist_id is None:
            self.feedback.configure(style="Bad.TLabel", text="That doesn't look like an AniList address. It should "
                                    "look like https://anilist.co/anime/16498/...")
        else:
            self.feedback.configure(style="Good.TLabel", text=f"✓ AniList entry {anilist_id}")
        self.save_button.state(("!disabled",) if anilist_id else ("disabled",))
        return anilist_id

    def _save(self) -> None:
        anilist_id = self._validate()
        if anilist_id is None:
            return
        self.app.ctx.overrides.set(self.key, anilist_id)
        self.close()
        self.app.recheck(self.key)

    def _remove(self) -> None:
        self.close()
        self.app.remove_override(self.key)


# ---------------------------------------------------------------- settings

class SettingsDialog(Dialog):
    THEMES = (("system", "Match Windows" if sys.platform == "win32" else "Match system"),
              ("light", "Light"), ("dark", "Dark"))

    def __init__(self, app: App, tab: int = 0) -> None:
        super().__init__(app, f"Settings - {APP_NAME}")
        cfg = app.config
        self.cfg = cfg
        self.results: queue.Queue[str] = queue.Queue()
        self.wrap = self.px(500)
        self.theme_var = tk.StringVar(value=dict(self.THEMES)[cfg.theme])
        self.threshold = tk.StringVar(value=str(cfg.confidence_threshold))
        self.expiry = tk.StringVar(value=str(cfg.cache_expiry_days))
        self.air_source = tk.StringVar(value=cfg.air_status_source)
        self.workers = tk.StringVar(value=str(cfg.probe_workers))
        self.net_enabled = tk.BooleanVar(value=cfg.network_workers_enabled)
        self.net_workers = tk.StringVar(value=str(cfg.network_workers))
        self.pause_enabled = tk.BooleanVar(value=cfg.pause_enabled)
        self.pause_files = tk.StringVar(value=str(cfg.pause_every_files))
        self.pause_seconds = tk.StringVar(value=str(cfg.pause_seconds))
        self.sonarr_url = tk.StringVar(value=cfg.sonarr_url)
        self.sonarr_key = tk.StringVar(value=cfg.sonarr_api_key)
        self.anime_only = tk.BooleanVar(value=cfg.sonarr_anime_only)
        self.read_unknown = tk.BooleanVar(value=cfg.sonarr_read_unknown)
        self.path_from = tk.StringVar(value=cfg.sonarr_path_from)
        self.path_to = tk.StringVar(value=cfg.sonarr_path_to)

        outer = ttk.Frame(self, padding=self.px(16))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook.add(self._general_tab(), text="General")
        self.notebook.add(self._drive_tab(), text="Hard drive care")
        self.notebook.add(self._sonarr_tab(), text="Sonarr")
        self.notebook.select(tab)

        buttons = ttk.Frame(outer)
        buttons.grid(row=1, column=0, sticky="e", pady=(self.px(12), 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="left", padx=(self.px(8), 0))
        self._update_switches()
        self.present(640, 610)

    # ------------------------------------------------------------ layout helpers

    def _page(self) -> ttk.Frame:
        page = ttk.Frame(self.notebook, padding=self.px(16))
        page.columnconfigure(1, weight=1)
        page._row = 0  # type: ignore[attr-defined]
        return page

    def _next_row(self, page: ttk.Frame) -> int:
        row = page._row  # type: ignore[attr-defined]
        page._row += 1  # type: ignore[attr-defined]
        return row

    def _setting(self, page: ttk.Frame, label: str, widget: tk.Misc, hint: str = "") -> None:
        row = self._next_row(page)
        ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", padx=(0, self.px(12)), pady=(self.px(8), 0))
        widget.grid(row=row, column=1, sticky="w", pady=(self.px(8), 0))
        if hint:
            self._hint(page, hint)

    def _hint(self, page: ttk.Frame, text: str, variable: tk.StringVar | None = None) -> ttk.Label:
        label = ttk.Label(page, text=text, textvariable=variable, style="Caption.TLabel", wraplength=self.wrap)
        label.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(2), 0))
        return label

    def _heading(self, page: ttk.Frame, text: str) -> None:
        ttk.Label(page, text=text, style="Strong.TLabel").grid(row=self._next_row(page), column=0, columnspan=2,
                                                               sticky="w", pady=(self.px(16), 0))

    def _spinbox(self, parent: tk.Misc, variable: tk.StringVar, low: int, high: int, width: int = 6) -> ttk.Spinbox:
        return ttk.Spinbox(parent, from_=low, to=high, textvariable=variable, width=width)

    # ------------------------------------------------------------ tabs

    def _general_tab(self) -> ttk.Frame:
        page = self._page()
        combo = ttk.Combobox(page, textvariable=self.theme_var, values=[name for _, name in self.THEMES],
                             state="readonly", width=18)
        self._setting(page, "Appearance", combo)
        self._setting(page, "Minimum match score", self._spinbox(page, self.threshold, 50, 100),
                      "AniList matches that score lower than this go to Check Manually. The default is 80.")
        row = ttk.Frame(page)
        self._spinbox(row, self.expiry, 1, 365).pack(side="left")
        ttk.Label(row, text="days").pack(side="left", padx=(self.px(6), 0))
        self._setting(page, "Check AniList again after", row,
                      "Answers from AniList are saved and reused until then, which keeps rescans fast.")

        self._heading(page, "Air status")
        choices = ttk.Frame(page)
        choices.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))
        for value, text in (("anilist", "From AniList"), ("sonarr", "From Sonarr"), ("off", "Off")):
            ttk.Radiobutton(choices, text=text, value=value, variable=self.air_source).pack(
                side="left", padx=(0, self.px(18)))
        self.air_hint = tk.StringVar()
        self._hint(page, "", self.air_hint)
        for variable in (self.air_source, self.sonarr_url, self.sonarr_key):
            variable.trace_add("write", lambda *_: self._update_air_hint())
        self._update_air_hint()

        self._heading(page, "Saved information")
        clear_row = ttk.Frame(page)
        clear_row.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))
        ttk.Button(clear_row, text="Clear saved online info", command=self._clear_online).pack(side="left")
        ttk.Button(clear_row, text="Open data folder", command=lambda: ui.open_path(self.app.ctx.data_dir)).pack(
            side="left", padx=(self.px(8), 0))
        self.clear_feedback = self._hint(
            page, "Clearing forgets the saved AniList answers and the dub list, so the next scan asks online again. "
                  "What Dub Checker knows about your files, your settings and your manual matches are kept.")
        self._hint(page, f"Data folder: {self.app.ctx.data_dir}")
        return page

    def _drive_tab(self) -> ttk.Frame:
        page = self._page()
        self._setting(page, "Files read at the same time", self._spinbox(page, self.workers, 1, 16),
                      "Use 1 for a single spinning hard drive - it's usually fastest and gentlest. SSDs are happy "
                      "with 4 or more.")

        self._heading(page, "Network shares")
        self.net_switch = ttk.Checkbutton(page, text="Use a different number for network shares (NAS)",
                                          variable=self.net_enabled, style="Switch.TCheckbutton",
                                          command=self._update_switches)
        self.net_switch.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))
        self.net_spin = self._spinbox(page, self.net_workers, 1, 32)
        self._setting(page, "Files read at the same time", self.net_spin,
                      "Reading several files at once hides network delays, so shares often scan faster with 6-8.")

        self._heading(page, "Breaks")
        ttk.Checkbutton(page, text="Take breaks while reading", variable=self.pause_enabled,
                        style="Switch.TCheckbutton", command=self._update_switches).grid(
            row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))
        row = ttk.Frame(page)
        row.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))
        ttk.Label(row, text="Read").pack(side="left")
        self.pause_files_spin = self._spinbox(row, self.pause_files, 1, 10000, width=7)
        self.pause_files_spin.pack(side="left", padx=self.px(6))
        ttk.Label(row, text="files, then pause").pack(side="left")
        self.pause_seconds_spin = self._spinbox(row, self.pause_seconds, 1, 3600, width=6)
        self.pause_seconds_spin.pack(side="left", padx=self.px(6))
        ttk.Label(row, text="seconds").pack(side="left")
        self.pause_hint = tk.StringVar()
        self._hint(page, "", self.pause_hint)
        for variable in (self.pause_files, self.pause_seconds, self.pause_enabled):
            variable.trace_add("write", lambda *_: self._update_pause_hint())
        self._update_pause_hint()
        return page

    def _sonarr_tab(self) -> ttk.Frame:
        page = self._page()
        self._setting(page, "Address", ttk.Entry(page, textvariable=self.sonarr_url, width=40),
                      "For example http://localhost:8989 (add the URL base if you set one, e.g. /sonarr).")
        self._setting(page, "API key", ttk.Entry(page, textvariable=self.sonarr_key, width=40, show="•"),
                      "In Sonarr: Settings > General > API Key. " + (
                          "Dub Checker saves it encrypted, so only your Windows account on this PC can read it."
                          if dpapi.available() else
                          "It's saved in UserData/config.json, which only your user account can read."))
        if self.cfg.api_key_unreadable:
            self._hint(page, "Your API key was saved on another PC or Windows account, so it can't be read "
                             "here. Please enter it again.")
        for text, variable in (("Only series set to the Anime series type", self.anime_only),
                               ("Read files myself when Sonarr doesn't know a track's language", self.read_unknown)):
            ttk.Checkbutton(page, text=text, variable=variable).grid(
                row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(8), 0))

        self._heading(page, "Path mapping")
        self._hint(page, "Only needed if Sonarr runs on another computer or in Docker and sees your files at a "
                         "different path. Fill in both or neither.")
        self._setting(page, "Sonarr sees files at", ttk.Entry(page, textvariable=self.path_from, width=40))
        self._setting(page, "This PC sees them at", ttk.Entry(page, textvariable=self.path_to, width=40),
                      "Example: /tv  →  \\\\nas\\media\\tv")

        test_row = ttk.Frame(page)
        test_row.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(16), 0))
        self.test_button = ttk.Button(test_row, text="Test connection", command=self._test_connection)
        self.test_button.pack(side="left")
        self.test_result = ttk.Label(page, text="", wraplength=self.wrap)
        self.test_result.grid(row=self._next_row(page), column=0, columnspan=2, sticky="w", pady=(self.px(6), 0))
        return page

    # ------------------------------------------------------------ behaviour

    def _update_switches(self) -> None:
        self.net_spin.state(("!disabled",) if self.net_enabled.get() else ("disabled",))
        state = ("!disabled",) if self.pause_enabled.get() else ("disabled",)
        self.pause_files_spin.state(state)
        self.pause_seconds_spin.state(state)

    def _update_air_hint(self) -> None:
        source = self.air_source.get()
        if source == "sonarr":
            text = ("Asks your Sonarr for each show's status and upcoming episodes: two quick requests, no waiting "
                    "on AniList. Shows Sonarr doesn't have get no air status. They're matched by folder name, so "
                    "this works for local files scans too.")
            if not (self.sonarr_url.get().strip() and self.sonarr_key.get().strip()):
                text += " Enter Sonarr's address and API key in the Sonarr tab first."
        elif source == "off":
            text = "No air status: nothing extra is looked up, and the Air status column and Airing tab are hidden."
        else:
            text = ("Shows decided from your files alone are looked up on AniList too, so every show gets an air "
                    "status. The first scan takes longer, because AniList limits how fast apps can ask; after that, "
                    "finished shows are remembered and airing ones are checked again twice a day.")
        self.air_hint.set(text)

    def _update_pause_hint(self) -> None:
        try:
            files, seconds = int(self.pause_files.get()), int(self.pause_seconds.get())
            minutes = (1000 // max(1, files)) * seconds / 60
        except (ValueError, tk.TclError):
            self.pause_hint.set("Enter whole numbers.")
            return
        if minutes >= 1.5:
            amount = f"about {minutes:.0f} minutes"
        else:
            amount = "about a minute" if minutes >= 0.75 else "less than a minute"
        text = f"adds {amount} of breaks per 1,000 files read from disk. Files Dub Checker already knows don't count."
        self.pause_hint.set(("This " if self.pause_enabled.get() else "Switched off. When on, this ") + text)

    def _clear_online(self) -> None:
        count = self.app.ctx.cache.clear_anilist()
        try:
            (self.app.ctx.data_dir / "dubInfo.json").unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Couldn't delete dubInfo.json: %s", exc)
        self.clear_feedback.configure(text=f"Done - forgot {ui.plural(count, 'saved AniList answer')} and the dub "
                                           "list. Your files, settings and manual matches were kept.")

    def _test_connection(self) -> None:
        self.test_button.state(("disabled",))
        self.test_result.configure(text="Connecting...", style="TLabel")
        url, key = self.sonarr_url.get(), self.sonarr_key.get()

        def work() -> None:
            try:
                self.results.put("ok:" + connection_summary(SonarrClient(url, key, timeout=15)))
            except SonarrError as exc:
                self.results.put("error:" + str(exc))
            except Exception as exc:
                log.exception("Sonarr test failed")
                self.results.put(f"error:Something went wrong: {exc}")

        threading.Thread(target=work, name="sonarr-test", daemon=True).start()
        self.after(100, self._check_test)

    def _check_test(self) -> None:
        try:
            message = self.results.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(100, self._check_test)
            return
        if not self.winfo_exists():
            return
        kind, _, text = message.partition(":")
        self.test_result.configure(text=text, style="Good.TLabel" if kind == "ok" else "Bad.TLabel")
        self.test_button.state(("!disabled",))

    def _int(self, variable: tk.StringVar, name: str, low: int, high: int) -> int:
        try:
            value = int(str(variable.get()).strip())
        except ValueError:
            raise ValueError(f"“{name}” needs a whole number.") from None
        if not low <= value <= high:
            raise ValueError(f"“{name}” must be between {low} and {high}.")
        return value

    def _save(self) -> None:
        cfg = self.cfg
        try:
            threshold = self._int(self.threshold, "Minimum match score", 1, 100)
            expiry = self._int(self.expiry, "Check AniList again after", 1, 3650)
            workers = self._int(self.workers, "Files read at the same time", 1, 32)
            net_workers = self._int(self.net_workers, "Files read at the same time (network shares)", 1, 32)
            pause_files = self._int(self.pause_files, "Read ... files", 1, 100000)
            pause_seconds = self._int(self.pause_seconds, "Pause ... seconds", 1, 3600)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        path_from, path_to = self.path_from.get().strip(), self.path_to.get().strip()
        if bool(path_from) != bool(path_to):
            self.notebook.select(2)
            messagebox.showerror(APP_NAME, "Path mapping needs both paths filled in, or neither.", parent=self)
            return

        old_theme = cfg.theme
        cfg.theme = next(value for value, name in self.THEMES if name == self.theme_var.get())
        cfg.confidence_threshold = threshold
        cfg.cache_expiry_days = expiry
        cfg.air_status_source = self.air_source.get()
        cfg.probe_workers = workers
        cfg.network_workers_enabled = self.net_enabled.get()
        cfg.network_workers = net_workers
        cfg.pause_enabled = self.pause_enabled.get()
        cfg.pause_every_files = pause_files
        cfg.pause_seconds = pause_seconds
        cfg.sonarr_url = self.sonarr_url.get().strip()
        cfg.sonarr_api_key = self.sonarr_key.get().strip()
        cfg.forget_unreadable_key()  # replaced by what's in the field now (even if that's nothing)
        cfg.sonarr_anime_only = self.anime_only.get()
        cfg.sonarr_read_unknown = self.read_unknown.get()
        cfg.sonarr_path_from, cfg.sonarr_path_to = path_from, path_to
        cfg.clamp()
        self.app.ctx.save_config()
        self.close()
        if cfg.theme != old_theme:
            self.app.set_theme(cfg.theme)
        self.app.settings_saved()
