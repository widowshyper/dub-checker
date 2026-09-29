#!/usr/bin/env python3
"""Prepare a private Python environment (.venv) for Dub Checker and start it.

Called by run.bat / run.sh. Uses only the standard library, so it works on a
fresh Python install. Keep the syntax simple so that an old Python can still
print the "too old" message instead of a syntax error.

Usage: python bootstrap.py [--setup-only]
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

MIN_VERSION = (3, 10)
ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
HASH_FILE = VENV / "requirements.sha256"
MARKER = VENV / "created_for.txt"
IS_WINDOWS = os.name == "nt"


def say(message):
    print(message, flush=True)  # flush so our lines stay in order with pip's output


def fail(message):
    sys.stdout.flush()
    print("\n" + message + "\n", file=sys.stderr, flush=True)
    sys.exit(1)


def linux_distro():
    """Return the distro id plus its 'like' ids, e.g. ['ubuntu', 'debian']."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ids = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids.extend(value.strip().strip('"').lower().split())
    return ids


def install_hint(what):
    """A one-line install command for tkinter or venv on this system."""
    if sys.platform == "darwin":
        if what == "tk":
            return ("Install Python from https://www.python.org/downloads/ (it includes tkinter), "
                    "or with Homebrew run:  brew install python-tk@%d.%d" % sys.version_info[:2])
        return "Reinstall Python from https://www.python.org/downloads/"
    if IS_WINDOWS:
        return ("Run the Python installer again, choose Modify, and make sure "
                "'tcl/tk and IDLE' is ticked." if what == "tk" else
                "Reinstall Python from https://www.python.org/downloads/")
    ids = linux_distro()
    table = {
        "tk": [("debian", "sudo apt install python3-tk"), ("ubuntu", "sudo apt install python3-tk"),
               ("fedora", "sudo dnf install python3-tkinter"), ("rhel", "sudo dnf install python3-tkinter"),
               ("arch", "sudo pacman -S tk"), ("suse", "sudo zypper install python3-tk"),
               ("alpine", "sudo apk add py3-tkinter")],
        "venv": [("debian", "sudo apt install python3-venv"), ("ubuntu", "sudo apt install python3-venv"),
                 ("fedora", "sudo dnf install python3"), ("arch", "sudo pacman -S python"),
                 ("suse", "sudo zypper install python3"), ("alpine", "sudo apk add python3")],
    }
    for distro, command in table[what]:
        if any(distro in i for i in ids):
            return "Install it with:  " + command
    return "Install your distribution's python3-tk package." if what == "tk" else \
        "Install your distribution's python3-venv package."


def check_python():
    if sys.version_info < MIN_VERSION:
        fail("Dub Checker needs Python %d.%d or newer, but this is Python %d.%d.\n"
             "Download a newer one from https://www.python.org/downloads/"
             % (MIN_VERSION + tuple(sys.version_info[:2])))


def check_tkinter():
    try:
        import tkinter
        tkinter.Tcl()
    except Exception as exc:  # ImportError, or a broken Tcl install
        fail("Dub Checker needs tkinter (Python's window toolkit), but it isn't available:\n  %s\n\n%s"
             % (exc, install_hint("tk")))


def base_interpreter():
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def venv_python(gui=False):
    if IS_WINDOWS:
        return VENV / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return VENV / "bin" / "python3"


def expected_marker():
    return "project=%s\npython=%s\nversion=%d.%d.%d\n" % ((ROOT, base_interpreter()) + tuple(sys.version_info[:3]))


def _remove_readonly(func, path, _info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def ensure_venv():
    """Create .venv, or rebuild it when the folder moved or Python changed."""
    if VENV.exists():
        try:
            recorded = MARKER.read_text(encoding="utf-8")
        except OSError:
            recorded = ""
        if recorded == expected_marker() and venv_python().exists():
            return
        say("The private Python environment was made for a different folder or Python - rebuilding it...")
        shutil.rmtree(VENV, onerror=_remove_readonly)

    say("Creating a private Python environment in .venv (first run only)...")
    import venv
    try:
        venv.EnvBuilder(with_pip=True).create(str(VENV))
    except Exception as exc:
        shutil.rmtree(VENV, ignore_errors=True)
        fail("Couldn't create the Python environment:\n  %s\n\n%s" % (exc, install_hint("venv")))
    MARKER.write_text(expected_marker(), encoding="utf-8")


def ensure_requirements():
    """pip install -r requirements.txt, but only when that file changed."""
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    try:
        if HASH_FILE.read_text(encoding="utf-8").strip() == digest:
            return
    except OSError:
        pass
    say("Installing the packages Dub Checker uses (needs internet, first run only)...")
    code = subprocess.call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
                            "-r", str(REQUIREMENTS)])
    if code != 0:
        fail("Installing the required packages failed (see above). "
             "Check your internet connection and run this again.")
    HASH_FILE.write_text(digest, encoding="utf-8")


def launch():
    args = [str(venv_python(gui=True)), "-m", "dubchecker"]
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, cwd=str(ROOT), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.chdir(str(ROOT))
        os.execv(args[0], args)


def main(argv):
    check_python()
    say("Python %d.%d.%d at %s" % (tuple(sys.version_info[:3]) + (base_interpreter(),)))
    check_tkinter()
    ensure_venv()
    ensure_requirements()
    if "--setup-only" in argv:
        say("Setup complete.")
        return 0
    say("Starting Dub Checker...")
    launch()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
