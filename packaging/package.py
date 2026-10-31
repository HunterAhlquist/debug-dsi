# SPDX-License-Identifier: MIT
"""Packs the PyInstaller output into a release archive that holds only the app.

    python packaging/package.py macos|windows|linux  ->  dist/dsidbg-<platform>.zip (Linux: .tar.gz)

macOS: dsidbg.app at the top of the zip, so unzipping gives the app itself, ready for /Applications.
Windows and Linux: the dsidbg folder (the executable and its _internal folder). The loader and the license are
inside the app (see dsidbg.spec); the install steps are in the repository's README.
"""
import os
import shutil
import subprocess
import sys
import tarfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DIST = os.path.join(ROOT, "dist")
PLATFORMS = ("macos", "windows", "linux")


def main(platform):
    app = os.path.join(DIST, "dsidbg.app" if platform == "macos" else "dsidbg")
    loader = "Contents/Resources/dsidbg-loader.nds" if platform == "macos" else "_internal/dsidbg-loader.nds"
    if not os.path.isfile(os.path.join(app, loader)):
        raise SystemExit("%s has no bundled loader (%s): rebuild it with packaging/dsidbg.spec" % (app, loader))
    if platform == "macos":
        out = os.path.join(DIST, "dsidbg-macos.zip")
        # ditto keeps the bundle's symlinks and code signature intact.
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", app, out], check=True)
    elif platform == "windows":
        out = shutil.make_archive(os.path.join(DIST, "dsidbg-windows"), "zip", DIST, "dsidbg")
    else:
        out = os.path.join(DIST, "dsidbg-linux.tar.gz")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(app, arcname="dsidbg")
    print(out)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in PLATFORMS:
        raise SystemExit(__doc__)
    main(sys.argv[1])
