"""Start-up: logging, the UserData check, Windows niceties and the Tk main loop."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback
import uuid
from pathlib import Path

from dubchecker import APP_NAME, __version__
from dubchecker.config import Config, Overrides, user_data_dir

log = logging.getLogger("dubchecker")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if sys.stderr is not None:  # None under pythonw and in the windowed exe
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)
    for noisy in ("urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def data_dir_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def show_fatal(message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message, parent=root)
        root.destroy()
    except tk.TclError:
        print(message, file=sys.stderr if sys.stderr else sys.stdout)


def windows_setup() -> None:
    """Crisp text on high-DPI screens, and our own icon on the taskbar."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DubChecker.App")
    except (AttributeError, OSError):
        pass


def main() -> int:
    data_dir = user_data_dir()
    if not data_dir_is_writable(data_dir):
        show_fatal("Dub Checker keeps its settings and saved information in a folder called UserData next to the "
                   f"program:\n\n{data_dir}\n\nIt isn't allowed to write there - for example because it's inside "
                   "Program Files or on read-only media.\n\nMove the Dub Checker folder somewhere you can write to, "
                   "such as Documents or a USB stick, and start it again.")
        return 1
    setup_logging(data_dir / "logs")
    log.info("Dub Checker %s starting (Python %s, %s)", __version__, sys.version.split()[0], sys.platform)
    windows_setup()

    import tkinter as tk
    from tkinter import messagebox

    from dubchecker.cache import Cache
    from dubchecker.gui import App, AppContext

    config_path = data_dir / "config.json"
    cache = Cache(data_dir / "cache.db")
    ctx = AppContext(Config.load(config_path), config_path, Overrides(data_dir / "overrides.json"), cache, data_dir)

    root = tk.Tk()
    root.withdraw()  # build hidden, then show, so the window doesn't flash half-drawn

    def report_callback_exception(exc_type: type, exc: BaseException, tb: object) -> None:
        log.error("Unexpected error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))  # type: ignore
        messagebox.showerror(APP_NAME, f"Something went wrong:\n\n{exc}\n\nDetails were written to "
                                       f"{data_dir / 'logs' / 'app.log'}", parent=root)

    root.report_callback_exception = report_callback_exception  # type: ignore[method-assign]
    app = App(root, ctx)
    root.deiconify()
    try:
        root.mainloop()
    finally:
        # The window is gone; let the scan workers notice Stop before the database closes,
        # otherwise they fail with "Cannot operate on a closed database".
        app.cancel.set()
        app.closing.set()
        app.join_workers(timeout=10)
        cache.close()
        log.info("Dub Checker closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
