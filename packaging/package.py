# SPDX-License-Identifier: MIT
"""Packs the PyInstaller output and the DSi loader into a release archive.

    python packaging/package.py macos|windows|linux  ->  dist/dsidbg-<platform>.zip (Linux: .tar.gz)
"""
import os
import shutil
import subprocess
import sys
import tarfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DIST = os.path.join(ROOT, "dist")

README = {
    "macos": """dsidbg for macOS

1. Move dsidbg.app to your Applications folder, then open it. The first time, macOS may refuse because the app
   isn't notarized: right-click dsidbg.app, choose Open, and confirm. It appears in the menu bar (a small DS).
2. Put the DSi's SD card in this Mac and choose "Set Up SD Card..." in the menu. It writes dsidbg/config.ini and
   copies the loader (also included here as dsidbg-loader.nds).
3. Choose "Install MCP" to give Claude Code, Codex and Claude Desktop the dsidbg tools.

The command line is inside the app: /Applications/dsidbg.app/Contents/MacOS/dsidbg -h
(or "Open Terminal" in the menu, which puts it on the PATH).
""",
    "windows": """dsidbg for Windows

1. Unzip this folder somewhere permanent, then open dsidbg\\dsidbg.exe. Windows SmartScreen may warn because the
   app isn't signed: choose More info, then Run anyway. It appears in the system tray (a small DS).
2. Put the DSi's SD card in this PC and choose "Set Up SD Card..." in the tray menu. It writes dsidbg\\config.ini
   and copies the loader (also included here as dsidbg-loader.nds).
3. Choose "Install MCP" to give Claude Code, Codex and Claude Desktop the dsidbg tools.

The command line: dsidbg\\dsidbg.exe -h (or "Open Terminal" in the menu, which puts it on the PATH).
""",
    "linux": """dsidbg for Linux

1. Extract this archive somewhere permanent and run dsidbg/dsidbg. It appears in the system tray (a small DS) on
   desktops with a tray (X11; GNOME needs an AppIndicator/tray extension). Dialogs need zenity or kdialog.
2. Mount the DSi's SD card and choose "Set Up SD Card..." in the tray menu. It writes dsidbg/config.ini and copies
   the loader (also included here as dsidbg-loader.nds).
3. Choose "Install MCP" to give Claude Code, Codex and Claude Desktop the dsidbg tools.

The command line: dsidbg/dsidbg -h (or "Open Terminal" in the menu, which puts it on the PATH).
""",
}


def main(platform):
    stage_root = os.path.join(DIST, "stage")
    shutil.rmtree(stage_root, ignore_errors=True)
    stage = os.path.join(stage_root, "dsidbg-" + platform)
    os.makedirs(stage)
    app = "dsidbg.app" if platform == "macos" else "dsidbg"
    shutil.copytree(os.path.join(DIST, app), os.path.join(stage, app), symlinks=True)
    shutil.copyfile(os.path.join(ROOT, "build", "dsidbg-loader.nds"), os.path.join(stage, "dsidbg-loader.nds"))
    with open(os.path.join(stage, "README.txt"), "w", newline="\r\n" if platform == "windows" else "\n") as f:
        f.write(README[platform])
    shutil.copyfile(os.path.join(ROOT, "LICENSE"), os.path.join(stage, "LICENSE.txt"))

    if platform == "macos":
        out = os.path.join(DIST, "dsidbg-macos.zip")
        # ditto keeps the bundle's symlinks and code signature intact.
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", stage, out], check=True)
    elif platform == "windows":
        out = shutil.make_archive(os.path.join(DIST, "dsidbg-windows"), "zip", stage_root, "dsidbg-windows")
    else:
        out = os.path.join(DIST, "dsidbg-linux.tar.gz")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(stage, arcname="dsidbg-linux")
    print(out)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in README:
        raise SystemExit(__doc__)
    main(sys.argv[1])
