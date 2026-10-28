"""The main window's Sonarr actions: open a show's page, search for replacement releases, tag shows and
delete them. A mixin of ``App`` (gui.py), kept apart so gui.py is about the window itself; the work is done
in the background and reported back through ``App.queue``."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any

from dubchecker import APP_NAME
from dubchecker import ui_common as ui
from dubchecker.categorize import dub_tag
from dubchecker.models import FileResult, FileStatus, ShowResult, language_word
from dubchecker.sonarr import (DELETE_SHOW, TAG_DUAL_AUDIO, TAG_DUB_PENDING, TAG_ENGLISH_ONLY, TAG_MISSING_DUB,
                               TAG_NO_DUB, DeleteTarget, SonarrError, apply_dub_tags, delete_from_sonarr,
                               search_for_replacements, series_page_url)


class SonarrActions:
    """Sonarr actions for the main window. Uses App's ``config``, ``model``, ``tree``, ``status_var``,
    ``background``, ``sonarr_client`` and the rest."""

    root: Any  # these come from App
    config: Any
    model: Any
    tree: Any
    status_var: Any
    background: Any
    sonarr_client: Any
    scanning: bool
    deleting: bool

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

    def open_in_sonarr(self, folder_name: str, title: str) -> None:
        """Open the show's page in Sonarr in the web browser. Finding it asks Sonarr, so that's done
        in the background."""
        self.status_var.set(f"Finding {title} in Sonarr...")

        def work() -> tuple:
            url = series_page_url(self.sonarr_client(), folder_name, title)
            return "open_url", (url, f"Opened {title} in Sonarr in your browser.")

        self.background("sonarr-open", work, lambda exc: ("error", (
            str(exc) if isinstance(exc, SonarrError) else f"Couldn't find the show in Sonarr: {exc}",)))

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

        self.background("sonarr-tags", lambda: ("notice", (apply_dub_tags(self.sonarr_client(), wanted).text(),)),
                        lambda exc: ("notice" if automatic else "error", (f"Couldn't tag shows in Sonarr: {exc}",)))

    def delete_selected(self) -> None:
        """Ask how to delete the selected shows and seasons from Sonarr (the Delete key, or the right-click
        menu)."""
        if not self.sonarr_configured or self.scanning or self.deleting:
            if self.scanning:
                self.status_var.set("Wait for the scan to finish before deleting shows.")
            elif self.deleting:
                self.status_var.set("Sonarr is still deleting the last shows you chose.")
            return
        targets = ui.delete_targets(self.tree.selection(), self.model)
        if targets:
            from dubchecker.dialogs import DeleteDialog
            DeleteDialog(self, targets)

    def delete_in_sonarr(self, targets: list[tuple[DeleteTarget, list[str]]], action: str) -> None:
        """Delete in the background; the deleted seasons then leave the list. No scan starts until it's done
        (see ``deleting``)."""
        self.deleting = True
        self.status_var.set(f"Deleting {ui.plural(len(targets), 'show')} in Sonarr...")
        keys = {target.folder_name: season_keys for target, season_keys in targets}
        if action == DELETE_SHOW:  # the whole show goes, whichever seasons were picked
            keys = {folder: [s.key for s in self.model.by_show().get(folder, [])] for folder in keys}
        sizes = {key: self.model.results[key].size for season_keys in keys.values() for key in season_keys
                 if key in self.model.results}

        def work() -> tuple:
            report = delete_from_sonarr(self.sonarr_client(), [target for target, _ in targets], action)
            gone = [key for folder in report.done for key in keys[folder]]
            if not report.freed:  # Sonarr didn't say how big the files were; go by the scan
                report.freed = sum(sizes.get(key, 0) for key in gone)
            return "deleted", (gone, report.text(), bool(report.failed))

        # A failure is reported through "deleted" too, so ``deleting`` is always cleared.
        self.background("sonarr-delete", work, lambda exc: ("deleted", ([], f"Couldn't delete in Sonarr: {exc}", True)))

    def _on_deleted(self, keys: list[str], text: str, failed: bool) -> None:
        self.deleting = False
        if keys:
            self.model.remove(keys)
            self.update_cards()
            self.refresh_table()
            self.save_results()
        self.status_var.set(text.split(" Couldn't")[0] if failed and keys else text)
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

        def work() -> tuple:
            message = search_for_replacements(self.sonarr_client(), group.folder_name, group.show_title,
                                              seasons_numbers, files)
            return "notice", (message + " Check Sonarr's Activity page for what it finds.",)

        self.background("sonarr-search", work, lambda exc: ("error", (
            str(exc) if isinstance(exc, SonarrError) else f"Couldn't ask Sonarr to search: {exc}",)))
