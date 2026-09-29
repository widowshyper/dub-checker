"""Build the portable Windows app: ``dist/Dub Checker/`` and ``dist/Dub Checker.zip``.

Run it through build.bat, which prepares .venv first. PyInstaller is installed
into .venv here, on demand; it is not one of the app's requirements.
An existing ``dist/Dub Checker/UserData`` is kept; the zip never contains UserData.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dubchecker import APP_NAME, __version__  # noqa: E402

PACKAGING = ROOT / "packaging"
ASSETS = ROOT / "dubchecker" / "assets"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
TARGET = DIST / APP_NAME
ZIP_PATH = DIST / f"{APP_NAME}.zip"
EXE_NAME = "DubChecker"

VERSION_TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{name}'),
      StringStruct('FileDescription', '{name}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{exe}'),
      StringStruct('LegalCopyright', ''),
      StringStruct('OriginalFilename', '{exe}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def fail(message: str) -> None:
    print(f"\nBUILD FAILED: {message}\n", file=sys.stderr)
    sys.exit(1)


def ensure_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        print("Installing PyInstaller into .venv...", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "pyinstaller"])


def write_version_file() -> Path:
    numbers = tuple((list(int(p) for p in __version__.split(".")) + [0, 0, 0, 0])[:4])
    BUILD.mkdir(exist_ok=True)
    path = BUILD / "version_info.txt"
    path.write_text(VERSION_TEMPLATE.format(numbers=numbers, version=".".join(map(str, numbers)), name=APP_NAME,
                                            exe=EXE_NAME), encoding="utf-8")
    return path


def run_pyinstaller(version_file: Path) -> Path:
    if not (ASSETS / "icon.ico").exists():
        fail(f"{ASSETS / 'icon.ico'} is missing. Run packaging/make_icon.py (needs Pillow).")
    out = BUILD / "pyinstaller-dist"
    import PyInstaller.__main__
    PyInstaller.__main__.run([
        str(PACKAGING / "entry.py"), "--name", EXE_NAME, "--onedir", "--windowed", "--noconfirm", "--clean",
        "--distpath", str(out), "--workpath", str(BUILD / "pyinstaller-work"), "--specpath", str(BUILD),
        "--paths", str(ROOT), "--icon", str(ASSETS / "icon.ico"), "--version-file", str(version_file),
        "--collect-submodules", "dubchecker",
        "--collect-binaries", "pymediainfo",   # bundles MediaInfo.dll
        "--collect-data", "sv_ttk",
        "--add-data", f"{ASSETS}{os.pathsep}dubchecker/assets",
    ])
    return out / EXE_NAME


def check_not_running() -> None:
    """A running exe is locked; say so plainly instead of failing halfway through the copy."""
    exe = TARGET / f"{EXE_NAME}.exe"
    if not exe.exists():
        return
    try:
        with open(exe, "r+b"):
            pass
    except PermissionError:
        fail(f"{exe} is in use (Access Denied).\nClose Dub Checker, then run build.bat again.")


def install(built: Path) -> None:
    """Replace everything in dist/Dub Checker except UserData."""
    check_not_running()
    TARGET.mkdir(parents=True, exist_ok=True)
    try:
        for child in TARGET.iterdir():
            if child.name == "UserData":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        for child in built.iterdir():
            if child.is_dir():
                shutil.copytree(child, TARGET / child.name)
            else:
                shutil.copy2(child, TARGET / child.name)
        shutil.copy2(PACKAGING / "README.txt", TARGET / "README.txt")
        shutil.copy2(ROOT / "LICENSE", TARGET / "LICENSE.txt")  # .txt so it opens with a double-click
    except PermissionError as exc:
        fail(f"Access Denied while copying into {TARGET}:\n  {exc}\n"
             "Is Dub Checker (or a file inside that folder) still open? Close it and run build.bat again.")


def make_zip() -> None:
    ZIP_PATH.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(TARGET.rglob("*")):
            relative = path.relative_to(TARGET)
            if relative.parts[0] == "UserData" or path.is_dir():
                continue
            zf.write(path, Path(APP_NAME) / relative)


def main() -> None:
    if os.name != "nt":
        fail("The portable exe is built on Windows. On macOS and Linux, use run.sh instead.")
    ensure_pyinstaller()
    built = run_pyinstaller(write_version_file())
    install(built)
    make_zip()
    size = ZIP_PATH.stat().st_size / (1024 * 1024)
    print(f"\nBuilt {APP_NAME} {__version__}:\n  {TARGET}\n  {ZIP_PATH} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
