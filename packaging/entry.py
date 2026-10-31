# SPDX-License-Identifier: MIT
"""Entry point of the packaged dsidbg app.

Opened with no arguments (from Finder, Explorer or a file manager) it runs the menu bar / system tray app. With
arguments it is the dsidbg command line, including `dsidbg mcp` (the MCP server) and `dsidbg serve` (the daemon).
"""
import sys

from dsidbg.cli import main

# macOS passes -psn_... to apps opened from Finder on some versions.
args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
if not args:
    # Windows: detach so the console window that opened with the app closes again.
    args = ["tray"] if sys.platform.startswith("win") else ["tray", "--foreground"]
sys.exit(main(args))
