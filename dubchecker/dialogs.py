"""Episode details, Dismissed shows, Delete, Fix match and Settings windows."""
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

from dubchecker import APP_NAME, __version__, dpapi, glass, theme
from dubchecker import ui_common as ui
from dubchecker.anilist import parse_anilist_id
from dubchecker.models import FileStatus, ShowResult, size_text
from dubchecker.sonarr import (DELETE_FILES, DELETE_FILES_UNMONITOR, DELETE_SHOW, DeleteTarget, SonarrClient,
                               SonarrError, connection_summary)

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
        theme.set_title_bar(self, self.app.palette["mode"] == "dark", self.app.palette["bg"])
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
        # Glass widgets register with the main window to be repainted with the theme; forget ours.
        self.app.surfaces[:] = [s for s in self.app.surfaces if s.winfo_exists()]

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
        info = ui.TABS[ui.group_tab(result)]
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
        rows.append(("Rating:", ui.rating_detail(result)))
        rows.append(("Size:", f"{size_text(result.size)} on disk, {ui.plural(result.episodes, 'episode')}"))
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
        if app.sonarr_configured and ui.english_only_files([result]):
            ttk.Button(buttons, text="Search Sonarr for dual-audio versions of the English-only episodes...",
                       command=lambda: app.search_sonarr([result], english=True)).pack(side="left",
                                                                                       padx=(0, self.px(8)))
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
            groups = list(dict.fromkeys(ui.group_title(s) for s in seasons))
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


# ---------------------------------------------------------------- deleting

class DeleteDialog(Dialog):
    """Asks how to delete the selected shows from Sonarr; nothing is chosen until you pick."""

    CHOICES = (
        (DELETE_SHOW, "Delete the shows entirely",
         "Removes the whole shows from Sonarr, even if you picked only some seasons, and deletes their "
         "folders, with every file in them, from the disk."),
        (DELETE_FILES_UNMONITOR, "Delete their episodes and unmonitor them",
         "Deletes the episode files, and unmonitors the shows (or just those seasons) so Sonarr doesn't "
         "download them again. The shows stay in Sonarr."),
        (DELETE_FILES, "Just delete their episodes",
         "Deletes the episode files. The shows stay in Sonarr, still monitored, so Sonarr may download "
         "them again."),
    )

    def __init__(self, app: App, targets: list[tuple[DeleteTarget, list[str]]]) -> None:
        super().__init__(app, f"Delete from Sonarr - {APP_NAME}")
        self.targets = targets
        frame = ttk.Frame(self, padding=self.px(20))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        wrap = self.px(600)
        shows = ui.plural(len(targets), "show")
        ttk.Label(frame, text=f"Delete {shows} from Sonarr?", style="Subtitle.TLabel").grid(row=0, column=0,
                                                                                            sticky="w")
        ttk.Label(frame, style="Muted.TLabel", wraplength=wrap,
                  text="Sonarr deletes the files from the disk. This can't be undone.").grid(
            row=1, column=0, sticky="w", pady=(self.px(2), self.px(10)))

        names = tk.Listbox(frame, height=min(8, len(targets)), activestyle="none", selectmode="browse",
                           highlightthickness=0, borderwidth=0, background=app.palette["card"],
                           foreground=app.palette["fg"], selectbackground=app.palette["card_selected"],
                           selectforeground=app.palette["fg"], font="SunValleyBodyFont")
        names.grid(row=2, column=0, sticky="nsew")
        by_key, shows = app.model.results, app.model.by_show()
        self.picked_size = self.whole_size = 0  # bytes in the selected seasons, and in the whole shows
        for target, keys in targets:
            size = sum(by_key[k].size for k in keys if k in by_key)
            self.picked_size += size
            self.whole_size += sum(s.size for s in shows.get(target.folder_name, []))
            seasons = "" if target.seasons is None else " (" + ", ".join(
                by_key[k].group.season_label for k in keys if k in by_key) + ")"
            names.insert("end", f"  {target.title}{seasons}  -  {size_text(size)}")

        self.choice = tk.StringVar(value="")
        choices = ttk.Frame(frame)
        choices.grid(row=3, column=0, sticky="ew", pady=(self.px(14), 0))
        for row, (value, label, hint) in enumerate(self.CHOICES):
            ttk.Radiobutton(choices, text=label, value=value, variable=self.choice,
                            command=self._update_button).grid(row=row * 2, column=0, sticky="w",
                                                              pady=(self.px(6) if row else 0, 0))
            ttk.Label(choices, text=hint, style="Muted.TLabel", wraplength=wrap).grid(
                row=row * 2 + 1, column=0, sticky="w", padx=(self.px(30), 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="ew", pady=(self.px(18), 0))
        buttons.columnconfigure(0, weight=1)
        self.space = ttk.Label(buttons, style="Strong.TLabel")
        self.space.grid(row=0, column=0, sticky="w")
        self.delete_button = ttk.Button(buttons, text="Delete", style="Accent.TButton", command=self._delete)
        self.delete_button.grid(row=0, column=1, padx=(0, self.px(8)))
        cancel = ttk.Button(buttons, text="Cancel", command=self.close)
        cancel.grid(row=0, column=2)
        self._update_button()
        self.present(680, 420 + 22 * min(8, len(targets)))
        cancel.focus_set()

    def _update_button(self) -> None:
        action = self.choice.get()
        self.delete_button.state(("!disabled",) if action else ("disabled",))
        size = self.whole_size if action == DELETE_SHOW else self.picked_size
        self.space.configure(text=f"Frees about {size_text(size)}" if action and size
                             else f"Selected: {size_text(self.picked_size)}")

    def _delete(self) -> None:
        action = self.choice.get()
        if not action:
            return
        self.close()
        self.app.delete_in_sonarr(self.targets, action)



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

_ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")  # Windows' symbol fonts (11, then 10)


def _icon_family(widget: tk.Misc) -> str:
    families = set(tkfont.families(widget))
    return next((f for f in _ICON_FAMILIES if f in families), "")


class PaneIcon:
    """A settings page's symbol: a white glyph on a rounded square of colour, as in System Settings."""

    def __init__(self, dialog: SettingsDialog, canvas: tk.Canvas, size: int) -> None:
        self.dialog, self.canvas, self.size = dialog, canvas, size
        self._image: tk.PhotoImage | None = None
        self._shape = canvas.create_image(0, 0, anchor="w")
        self._glyph = canvas.create_text(0, 0, fill="#ffffff", font=dialog.glyph_font(size))

    def draw(self, index: int, x: int, y: int, backdrop: str) -> None:
        pal, size = self.dialog.app.palette, self.size
        _title, color, glyph = SettingsDialog.PANES[index]
        look = glass.Look(round(size * 0.24), pal[color], backdrop)
        self._image = glass.stretched(self.canvas, look, size, size)
        self.canvas.itemconfigure(self._shape, image=self._image)
        self.canvas.coords(self._shape, x, y)
        self.canvas.coords(self._glyph, x + size / 2, y)
        self.canvas.itemconfigure(self._glyph, text=glyph if self.dialog.icon_family else _title[0])


class PaneRow(tk.Canvas):
    """A page in the Settings sidebar: its icon and name, on a blue highlight when it's open."""

    def __init__(self, dialog: SettingsDialog, parent: tk.Misc, index: int) -> None:
        app = dialog.app
        super().__init__(parent, height=app.px(34), width=1, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.dialog, self.index, self.hover = dialog, index, False
        self._pill_image: tk.PhotoImage | None = None
        self._pill = self.create_image(0, 0, anchor="nw")
        self.icon = PaneIcon(dialog, self, app.px(22))
        self._title = self.create_text(0, 0, text=SettingsDialog.PANES[index][0], anchor="w",
                                       font="SunValleyBodyFont")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: dialog.select_pane(index))
        self.bind("<Configure>", lambda _e: self.recolor())

    def _set_hover(self, hover: bool) -> None:
        self.hover = hover
        self.recolor()

    def recolor(self) -> None:
        pal, px = self.dialog.app.palette, self.dialog.app.px
        width = max(self.winfo_width(), px(120))
        height = int(self.cget("height"))
        chosen = self.dialog.pane == self.index
        row = pal["accent"] if chosen else pal["card_hover"] if self.hover else ""
        self.configure(background=pal["card"])
        if row:
            self._pill_image = glass.stretched(self, glass.Look(px(7), row, pal["card"]), width, height)
            self.itemconfigure(self._pill, image=self._pill_image, state="normal")
        else:
            self.itemconfigure(self._pill, image="", state="hidden")  # the old image is about to go
            self._pill_image = None
        self.icon.draw(self.index, px(7), height // 2, row or pal["card"])
        self.coords(self._title, px(38), height / 2)
        self.itemconfigure(self._title, fill="#ffffff" if chosen else pal["fg"])


class SettingsDialog(Dialog):
    """Laid out like macOS System Settings: the pages in a glass sidebar, and each page's settings in
    rounded groups, a name on the left, its control on the right and what it does underneath."""
    THEMES = (("system", "Match Windows" if sys.platform == "win32" else "Match system"),
              ("light", "Light"), ("dark", "Dark"))
    # Name, colour (a palette key) and glyph (Segoe Fluent Icons) of each page.
    PANES = (("General", "grey", ""), ("Hard drive care", "amber", ""),
             ("Sonarr", "blue", ""), ("Schedule", "red", ""))

    def __init__(self, app: App, tab: int = 0) -> None:
        super().__init__(app, f"Settings - {APP_NAME}")
        cfg = app.config
        self.cfg = cfg
        self.results: queue.Queue[str] = queue.Queue()
        self.theme_var = tk.StringVar(value=dict(self.THEMES)[cfg.theme])
        self.threshold = tk.StringVar(value=str(cfg.confidence_threshold))
        self.expiry = tk.StringVar(value=str(cfg.cache_expiry_days))
        self.air_source = tk.StringVar(value=cfg.air_status_source)
        self.dub_grace = tk.StringVar(value=str(cfg.dub_grace_days))
        self.anilist_ratings = tk.BooleanVar(value=cfg.anilist_ratings)
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
        self.tag_after_scan = tk.BooleanVar(value=cfg.sonarr_tag_after_scan)
        self.path_from = tk.StringVar(value=cfg.sonarr_path_from)
        self.path_to = tk.StringVar(value=cfg.sonarr_path_to)
        self.schedule_on = tk.BooleanVar(value=cfg.schedule_enabled)
        self.schedule_scopes = ["all", *app.scope_tabs()]
        if cfg.schedule_scope not in self.schedule_scopes:
            self.schedule_scopes.append(cfg.schedule_scope)  # e.g. Airing while air status is off
        self.schedule_scope = tk.StringVar(value=self._scope_label(cfg.schedule_scope))
        self.schedule_hours = tk.StringVar(value=str(cfg.schedule_every_hours))
        self.icon_family = _icon_family(self)
        self._fonts: dict[int, tkfont.Font] = {}
        self.pane = -1
        first_surface = len(app.surfaces)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        gap = self.px(8)
        self._build_sidebar().grid(row=0, column=0, rowspan=2, sticky="ns", padx=(gap, 0), pady=gap)
        self._build_pages().grid(row=0, column=1, sticky="nsew", padx=(self.px(12), 0), pady=(self.px(14), 0))
        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=1, sticky="e", padx=self.px(18), pady=(self.px(6), self.px(12)))
        glass.PillButton(buttons, app, "Cancel", self.close, width=self.px(84)).pack(side="left")
        glass.PillButton(buttons, app, "Save", self._save, accent=True, width=self.px(84)).pack(side="left")
        for surface in app.surfaces[first_surface:]:
            surface.recolor()
        self.select_pane(tab)
        self._update_switches()
        self.present(940, 680)

    # ------------------------------------------------------------ the window

    def glyph_font(self, size: int) -> tkfont.Font:
        """The icons' symbol font, at about half the icon's size (kept: Tk forgets fonts nobody holds)."""
        if size not in self._fonts:
            family = self.icon_family or tkfont.nametofont("SunValleyBodyStrongFont").cget("family")
            self._fonts[size] = tkfont.Font(self, family=family, size=-round(size * (0.55 if self.icon_family
                                                                                      else 0.6)))
        return self._fonts[size]

    def _build_sidebar(self) -> glass.GlassPanel:
        panel = glass.GlassPanel(self, self.app, "sidebar", padding=8)
        panel.configure(width=self.px(230))
        panel.pack_propagate(False)
        self.pane_rows = []
        for index in range(len(self.PANES)):
            row = PaneRow(self, panel.body, index)
            row.pack(fill="x", pady=(0, self.px(2)))
            self.pane_rows.append(row)
        ttk.Label(panel.body, text=f"{APP_NAME} {__version__}", style="Card.Caption.TLabel").pack(
            side="bottom", anchor="w", padx=self.px(8), pady=(0, self.px(2)))
        return panel

    def _build_pages(self) -> ttk.Frame:
        """The open page's title over its settings, which scroll if the window is too short for them."""
        frame = ttk.Frame(self)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        size = self.px(30)
        self.header = tk.Canvas(frame, height=size + self.px(6), highlightthickness=0, borderwidth=0)
        self.header.grid(row=0, column=0, sticky="ew", padx=(self.px(8), 0), pady=(0, self.px(8)))
        self.header_icon = PaneIcon(self, self.header, size)
        self.header_title = self.header.create_text(size + self.px(12), (size + self.px(6)) // 2, anchor="w",
                                                    font="DubLargeTitleFont")
        self.scroller = tk.Canvas(frame, highlightthickness=0, borderwidth=0, yscrollincrement=self.px(20))
        self.scroller.grid(row=1, column=0, sticky="nsew")
        # Laid over the page's right edge like a macOS scroll bar, so showing it doesn't narrow the page
        # (which would re-wrap the text, which could change whether it's needed, and so on).
        self.scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.scroller.yview)
        self.scroller.configure(yscrollcommand=self.scrollbar.set)
        self.holder = ttk.Frame(self.scroller, padding=(self.px(4), self.px(4), self.px(14), self.px(12)))
        # Always a set width: the text wraps to fit it, so a width that followed the text could see-saw.
        holder_item = self.scroller.create_window(0, 0, window=self.holder, anchor="nw", width=self.px(680))
        self.scroller.bind("<Configure>", lambda e: (
            self.scroller.itemconfigure(holder_item, width=max(e.width, self.px(360))), self._update_scrolling()))
        self.holder.bind("<Configure>", lambda _e: self._update_scrolling())
        self.bind("<MouseWheel>", self._on_wheel)
        self.pages = [self._general_page(), self._drive_page(), self._sonarr_page(), self._schedule_page()]
        return frame

    def select_pane(self, index: int) -> None:
        if index == self.pane:
            return
        if self.pane >= 0:
            self.pages[self.pane].pack_forget()
        self.pane = index
        self.pages[index].pack(fill="x")
        for row in self.pane_rows:
            row.recolor()
        pal = self.app.palette
        self.header.configure(background=pal["bg"])
        self.header_icon.draw(index, 0, int(self.header.cget("height")) // 2, pal["bg"])
        self.header.itemconfigure(self.header_title, text=self.PANES[index][0], fill=pal["fg"])
        self.scroller.configure(background=pal["bg"])
        self.scroller.yview_moveto(0)

    def _update_scrolling(self) -> None:
        """Show the scroll bar only when the page doesn't fit."""
        self.scroller.configure(scrollregion=(0, 0, 0, self.holder.winfo_reqheight()))
        if self.holder.winfo_reqheight() > self.scroller.winfo_height() > 1:
            self.scrollbar.place(in_=self.scroller, relx=1, rely=0, relheight=1, anchor="ne")
        else:
            self.scrollbar.place_forget()
            self.scroller.yview_moveto(0)

    def _on_wheel(self, event: Any) -> str:
        if self.scrollbar.winfo_ismapped():
            self.scroller.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"  # also stops spin boxes and drop-downs changing their value while the page scrolls

    # ------------------------------------------------------------ layout helpers

    def _page(self) -> ttk.Frame:
        return ttk.Frame(self.holder)

    def _group(self, page: ttk.Frame, heading: str = "") -> tk.Frame:
        """A rounded group of settings. Returns the frame to add rows to."""
        if heading:
            ttk.Label(page, text=heading, style="Strong.TLabel").pack(
                anchor="w", padx=self.px(10), pady=(self.px(14) if page.winfo_children() else 0, self.px(4)))
        panel = glass.GlassPanel(page, self.app, "card", padding=0)
        panel.pack(fill="x")
        panel.body.columnconfigure(0, weight=1)
        return panel.body

    def _item(self, group: tk.Frame) -> ttk.Frame:
        """A row of a group, under a hairline if it isn't the first."""
        rows = group.grid_size()[1]
        if rows:
            ttk.Frame(group, style="Hairline.TFrame", height=1).grid(row=rows, column=0, sticky="ew",
                                                                     padx=self.px(12))
        item = ttk.Frame(group, style="Row.TFrame", padding=(self.px(12), self.px(9)))
        item.grid(row=rows + 1, column=0, sticky="ew")
        item.columnconfigure(0, weight=1)
        return item

    def _setting(self, group: tk.Frame, label: str, make: Any, hint: str = "") -> Any:
        """A row with ``label`` on the left and the control ``make(row)`` builds on the right."""
        item = self._item(group)
        ttk.Label(item, text=label, style="Card.TLabel").grid(row=0, column=0, sticky="w")
        widget = make(item)
        widget.grid(row=0, column=1, sticky="e", padx=(self.px(12), 0))
        if hint:
            self._hint(item, hint)
        return widget

    def _switch(self, group: tk.Frame, label: str, variable: tk.BooleanVar, hint: str = "") -> ttk.Checkbutton:
        return self._setting(group, label, lambda row: ttk.Checkbutton(
            row, variable=variable, style="Card.Switch.TCheckbutton", command=self._update_switches), hint)

    def _hint(self, item: ttk.Frame, text: str, variable: tk.StringVar | None = None,
              style: str = "Card.Caption.TLabel") -> ttk.Label:
        """Grey text under a row's setting, as wide as the row."""
        label = ttk.Label(item, text=text, textvariable=variable, style=style)
        label.grid(row=item.grid_size()[1], column=0, columnspan=2, sticky="w", pady=(self.px(3), 0))
        self._wrap(label, item, 2 * self.px(12))
        return label

    def _footnote(self, page: ttk.Frame, text: str) -> ttk.Label:
        """Grey text under a group."""
        label = ttk.Label(page, text=text, style="Caption.TLabel")
        label.pack(anchor="w", padx=self.px(10), pady=(self.px(6), 0))
        self._wrap(label, page, 2 * self.px(10))
        return label

    def _wrap(self, label: ttk.Label, container: tk.Misc, inset: int) -> None:
        container.bind("<Configure>", lambda e: label.configure(wraplength=max(self.px(200), e.width - inset)),
                       add="+")

    def _spinbox(self, parent: tk.Misc, variable: tk.StringVar, low: int, high: int, width: int = 6) -> ttk.Spinbox:
        box = ttk.Spinbox(parent, from_=low, to=high, textvariable=variable, width=width, style="Card.TSpinbox")
        box.bind("<MouseWheel>", self._on_wheel)
        return box

    def _spin_unit(self, parent: tk.Misc, variable: tk.StringVar, low: int, high: int, unit: str,
                   width: int = 6) -> ttk.Frame:
        """A spin box with its unit after it ("30 days"). The spin box is ``.spin``."""
        frame = ttk.Frame(parent, style="Row.TFrame")
        frame.spin = self._spinbox(frame, variable, low, high, width)  # type: ignore[attr-defined]
        frame.spin.pack(side="left")  # type: ignore[attr-defined]
        ttk.Label(frame, text=unit, style="Card.TLabel").pack(side="left", padx=(self.px(6), 0))
        return frame

    def _combobox(self, parent: tk.Misc, variable: tk.StringVar, values: list[str], width: int) -> ttk.Combobox:
        box = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=width,
                           style="Card.TCombobox")
        box.bind("<MouseWheel>", self._on_wheel)
        return box

    def _segmented(self, parent: tk.Misc, variable: tk.StringVar, options: list[tuple[str, str]],
                   width: int) -> glass.SegmentedControl:
        control = glass.SegmentedControl(parent, self.app, variable, options)
        control.configure(width=self.px(width))
        return control

    def _entry(self, parent: tk.Misc, variable: tk.StringVar, **options: Any) -> ttk.Entry:
        return ttk.Entry(parent, textvariable=variable, width=36, style="Card.TEntry", **options)

    # ------------------------------------------------------------ pages

    def _general_page(self) -> ttk.Frame:
        page = self._page()
        group = self._group(page)
        self._setting(group, "Appearance", lambda row: self._segmented(
            row, self.theme_var, [(name, name) for _, name in self.THEMES], 330))
        self._setting(group, "Minimum match score", lambda row: self._spinbox(row, self.threshold, 50, 100),
                      "AniList matches that score lower than this go to Check Manually. The default is 80.")
        self._setting(group, "Check AniList again after",
                      lambda row: self._spin_unit(row, self.expiry, 1, 365, "days"),
                      "Answers from AniList are saved and reused until then, which keeps rescans fast.")

        group = self._group(page, "Dubs on their way")
        self._setting(group, "Dubs can follow up to", lambda row: self._spin_unit(row, self.dub_grace, 0, 365, "days"),
                      "English dubs of airing shows often come out a few weeks after each episode. A Japanese-only "
                      "episode newer than this goes to Dub Coming instead of Needs English Audio, and moves back by "
                      "itself if its dub doesn't turn up in time. Air dates come from Sonarr, or AniList for shows "
                      "Sonarr doesn't have. Without dates, a show counts while it's still airing and only its newest "
                      "episodes lack English. 0 turns this off.")

        group = self._group(page, "Air status")
        self.air_hint = tk.StringVar()
        air = self._setting(group, "Look up air status", lambda row: self._segmented(
            row, self.air_source, [("anilist", "From AniList"), ("sonarr", "From Sonarr"), ("off", "Off")], 330))
        self._hint(air.master, "", self.air_hint)
        for variable in (self.air_source, self.sonarr_url, self.sonarr_key):
            variable.trace_add("write", lambda *_: self._update_air_hint())
        self._update_air_hint()

        group = self._group(page, "Ratings")
        self._switch(group, "Rate shows Sonarr doesn't by AniList's score", self.anilist_ratings,
                     "Each show's rating (out of 10) comes from Sonarr when it's set up and has one. Otherwise "
                     "AniList's average score for the season is used. Seasons decided from your files alone "
                     "haven't been looked up on AniList, so a scan looks them up for their score, which takes a "
                     "while the first time. Turn this off to skip that.")

        group = self._group(page, "Saved information")
        clear = self._setting(group, "Saved online info", lambda row: glass.PillButton(
            row, self.app, "Clear", self._clear_online, backdrop="card"))
        self.clear_feedback = self._hint(
            clear.master, "Clearing forgets the saved AniList answers and the dub list, so the next scan asks online "
                          "again. What Dub Checker knows about your files, your settings and your manual matches are "
                          "kept.")
        self._setting(group, "Data folder", lambda row: glass.PillButton(
            row, self.app, "Open", lambda: ui.open_path(self.app.ctx.data_dir), backdrop="card"),
            str(self.app.ctx.data_dir))
        return page

    def _drive_page(self) -> ttk.Frame:
        page = self._page()
        group = self._group(page)
        self._setting(group, "Files read at the same time", lambda row: self._spinbox(row, self.workers, 1, 16),
                      "Use 1 for a single spinning hard drive - it's usually fastest and gentlest. SSDs are happy "
                      "with 4 or more.")

        group = self._group(page, "Network shares")
        self.net_switch = self._switch(group, "Use a different number for network shares (NAS)", self.net_enabled)
        self.net_spin = self._setting(group, "Files read at the same time",
                                      lambda row: self._spinbox(row, self.net_workers, 1, 32),
                                      "Reading several files at once hides network delays, so shares often scan "
                                      "faster with 6-8.")

        group = self._group(page, "Breaks")
        self._switch(group, "Take breaks while reading", self.pause_enabled)
        self.pause_files_spin = self._setting(
            group, "Pause after reading", lambda row: self._spin_unit(row, self.pause_files, 1, 10000, "files", 7)).spin
        pause_for = self._setting(group, "Pause for",
                                  lambda row: self._spin_unit(row, self.pause_seconds, 1, 3600, "seconds"))
        self.pause_seconds_spin = pause_for.spin
        self.pause_hint = tk.StringVar()
        self._hint(pause_for.master, "", self.pause_hint)
        for variable in (self.pause_files, self.pause_seconds, self.pause_enabled):
            variable.trace_add("write", lambda *_: self._update_pause_hint())
        self._update_pause_hint()
        return page

    def _sonarr_page(self) -> ttk.Frame:
        page = self._page()
        group = self._group(page, "Connection")
        self._setting(group, "Address", lambda row: self._entry(row, self.sonarr_url),
                      "For example http://localhost:8989 (add the URL base if you set one, e.g. /sonarr).")
        key = self._setting(group, "API key", lambda row: self._entry(row, self.sonarr_key, show="•"),
                            "In Sonarr: Settings > General > API Key. " + (
                                "Dub Checker saves it encrypted, so only your Windows account on this PC can read "
                                "it." if dpapi.available() else
                                "It's saved in UserData/config.json, which only your user account can read."))
        if self.cfg.api_key_unreadable:
            self._hint(key.master, "Your API key was saved on another PC or Windows account, so it can't be read "
                                   "here. Please enter it again.", style="Card.Bad.TLabel")
        self.test_button = self._setting(group, "Test the connection", lambda row: glass.PillButton(
            row, self.app, "Test", self._test_connection, backdrop="card"))
        self.test_result = self._hint(self.test_button.master, "", style="Card.TLabel")
        self.test_result.grid_remove()  # shown once there's a result

        group = self._group(page, "Options")
        self._switch(group, "Only series set to the Anime series type", self.anime_only)
        self._switch(group, "Read files myself when Sonarr doesn't know a track's language", self.read_unknown)
        self._switch(group, "Tag shows in Sonarr after every scan", self.tag_after_scan,
                     "Tags each show dual-audio (every episode has both languages), missing-dub (an English dub "
                     "exists but some episodes don't have it), no-dub (no English dub exists) or english-only (some "
                     "episodes have English but not the original language). You can also do it any time with Tag in "
                     "Sonarr... in the sidebar.")

        group = self._group(page, "Path mapping")
        self._setting(group, "Sonarr sees files at", lambda row: self._entry(row, self.path_from))
        self._setting(group, "This PC sees them at", lambda row: self._entry(row, self.path_to),
                      "Example: /tv  →  \\\\nas\\media\\tv")
        self._footnote(page, "Only needed if Sonarr runs on another computer or in Docker and sees your files at a "
                             "different path. Fill in both or neither.")
        return page

    def _scope_label(self, scope: str) -> str:
        return "Every show" if scope == "all" else f"Only {ui.TABS[scope].title}"

    def _schedule_page(self) -> ttk.Frame:
        page = self._page()
        group = self._group(page)
        self._switch(group, "Scan automatically while Dub Checker is open", self.schedule_on)
        self.schedule_scope_box = self._setting(
            group, "What to scan", lambda row: self._combobox(
                row, self.schedule_scope, [self._scope_label(s) for s in self.schedule_scopes], 26),
            "Scanning only one tab is much quicker: just those shows are read and looked up, and the rest of your "
            "results stay as they are. It needs one full scan first, so Dub Checker knows which shows are in the "
            "tab.")
        self.schedule_hours_spin = self._setting(
            group, "Every", lambda row: self._spin_unit(row, self.schedule_hours, 1, 24 * 30, "hours"),
            "24 is once a day; 168 is once a week.").spin
        self._footnote(page, "Dub Checker has to be open for this (minimised is fine). If a scan came due while it "
                             "was closed, it runs about a minute after you open it. The scan uses the source and "
                             "folder chosen in the main window, and never interrupts a scan you started yourself.")
        return page

    # ------------------------------------------------------------ behaviour

    def _update_switches(self) -> None:
        self.net_spin.state(("!disabled",) if self.net_enabled.get() else ("disabled",))
        state = ("!disabled",) if self.pause_enabled.get() else ("disabled",)
        self.pause_files_spin.state(state)
        self.pause_seconds_spin.state(state)
        on = self.schedule_on.get()
        self.schedule_scope_box.state(("readonly", "!disabled") if on else ("disabled",))
        self.schedule_hours_spin.state(("!disabled",) if on else ("disabled",))

    def _update_air_hint(self) -> None:
        source = self.air_source.get()
        if source == "sonarr":
            text = ("Asks your Sonarr for each show's status and upcoming episodes: two quick requests, no waiting "
                    "on AniList. Shows Sonarr doesn't have get no air status. They're matched by folder name, so "
                    "this works for local files scans too.")
            if not (self.sonarr_url.get().strip() and self.sonarr_key.get().strip()):
                text += " Enter Sonarr's address and API key on the Sonarr page first."
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
        self.test_result.configure(text="Connecting...", style="Card.TLabel")
        self.test_result.grid()
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
        self.test_result.configure(text=text, style="Card.Good.TLabel" if kind == "ok" else "Card.Bad.TLabel")
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
            schedule_hours = self._int(self.schedule_hours, "Scan every ... hours", 1, 24 * 30)
            dub_grace = self._int(self.dub_grace, "Dubs can follow up to ... days", 0, 365)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        path_from, path_to = self.path_from.get().strip(), self.path_to.get().strip()
        if bool(path_from) != bool(path_to):
            self.select_pane(2)
            messagebox.showerror(APP_NAME, "Path mapping needs both paths filled in, or neither.", parent=self)
            return

        old_theme = cfg.theme
        cfg.theme = next(value for value, name in self.THEMES if name == self.theme_var.get())
        cfg.confidence_threshold = threshold
        cfg.cache_expiry_days = expiry
        cfg.air_status_source = self.air_source.get()
        cfg.dub_grace_days = dub_grace
        cfg.anilist_ratings = self.anilist_ratings.get()
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
        cfg.sonarr_tag_after_scan = self.tag_after_scan.get()
        cfg.sonarr_path_from, cfg.sonarr_path_to = path_from, path_to
        schedule_scope = self.schedule_scopes[[self._scope_label(x) for x in self.schedule_scopes].index(
            self.schedule_scope.get())]
        schedule = (self.schedule_on.get(), schedule_scope, schedule_hours)
        if schedule[0] and schedule != (cfg.schedule_enabled, cfg.schedule_scope, cfg.schedule_every_hours):
            cfg.schedule_last_run = time.time()  # a new or changed schedule counts from now
        cfg.schedule_enabled, cfg.schedule_scope, cfg.schedule_every_hours = schedule
        cfg.clamp()
        self.app.ctx.save_config()
        self.close()
        if cfg.theme != old_theme:
            self.app.set_theme(cfg.theme)
        self.app.settings_saved()
