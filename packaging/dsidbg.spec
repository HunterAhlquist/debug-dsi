# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# PyInstaller recipe for the dsidbg app: the menu bar / tray app, CLI, daemon and MCP server in one executable,
# with the DSi loader bundled. Build from the repo root after `make nds`:
#
#     pyinstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/dsidbg.spec
#
# Needs pyinstaller, pystray and pillow. Output: dist/dsidbg.app on macOS, dist/dsidbg/ elsewhere.
#
# macOS signing: set DSIDBG_CODESIGN_IDENTITY to a "Developer ID Application" identity (its name or hash) and every
# binary is signed with it, with the hardened runtime and a secure timestamp, as notarization requires (then run
# packaging/notarize.py; the release workflow does both). Without it the app is signed ad hoc and Gatekeeper asks
# before the first launch.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
sys.path.insert(0, os.path.join(ROOT, "host"))
from dsidbg import DSIDBG_VERSION  # noqa: E402
from dsidbg.tray import icon_image  # noqa: E402

LOADER = os.path.join(ROOT, "build", "dsidbg-loader.nds")
if not os.path.exists(LOADER):
    raise SystemExit("build/dsidbg-loader.nds is missing: build the DSi targets first (make nds)")

MAC, WIN = sys.platform == "darwin", sys.platform.startswith("win")
IDENTITY = (os.environ.get("DSIDBG_CODESIGN_IDENTITY") or None) if MAC else None

# App icon: the tray icon, scaled up.
icon_dir = os.path.join(ROOT, "build", "pyinstaller-icon")
os.makedirs(icon_dir, exist_ok=True)
big = icon_image("connected").resize((512, 512))
icon = None
if MAC:
    icon = os.path.join(icon_dir, "dsidbg.icns")
    big.save(icon)
elif WIN:
    icon = os.path.join(icon_dir, "dsidbg.ico")
    big.save(icon, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

# pystray picks its backend at run time; name it so it gets bundled.
backends = ["pystray._darwin"] if MAC else ["pystray._win32"] if WIN else ["pystray._xorg"]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[os.path.join(ROOT, "host")],
    # Inside the app (Contents/Resources on macOS), added before the bundle is signed: "Set Up SD Card..." copies the
    # loader from here, so the download is the app alone.
    datas=[(LOADER, "."), (os.path.join(ROOT, "LICENSE"), ".")],
    hiddenimports=backends + ["dsidbg.daemon", "dsidbg.install", "dsidbg.mcp", "dsidbg.native", "dsidbg.tray"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dsidbg",
    # Windows needs a console executable for the CLI and the MCP server's stdio; the tray detaches from it.
    console=not MAC,
    icon=icon,
    codesign_identity=IDENTITY,  # the hardened runtime needs no entitlements (checked: CLI, MCP server, tray)
)
coll = COLLECT(exe, a.binaries, a.datas, name="dsidbg")
if MAC:
    app = BUNDLE(
        coll,
        name="dsidbg.app",
        icon=icon,
        bundle_identifier="io.github.debug-dsi.dsidbg",
        version=DSIDBG_VERSION,
        info_plist={
            "LSUIElement": True,  # menu bar only: no Dock icon
            "CFBundleShortVersionString": DSIDBG_VERSION,
            "NSHumanReadableCopyright": "MIT License",
        },
    )
