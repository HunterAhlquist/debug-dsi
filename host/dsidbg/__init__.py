# SPDX-License-Identifier: MIT
"""Host side of debug-dsi: the daemon the DSi connects to, the dsidbg command line and its MCP server."""
import hashlib
import os
import sys

DSIDBG_VERSION = "0.1.0"  # keep in sync with include/dsidbg.h

# True in the packaged app (PyInstaller), where the code is inside the executable.
FROZEN = bool(getattr(sys, "frozen", False))


def code_id():
    """Fingerprint of this package's source. The daemon keeps running across updates, so the CLI and MCP server
    compare it with the daemon's to spot (and replace) a daemon started from older code."""
    h = hashlib.sha1()
    if FROZEN:  # no source files: the executable itself identifies the code
        st = os.stat(sys.executable)
        h.update(("%s|%s|%d|%d" % (DSIDBG_VERSION, sys.executable, st.st_size, int(st.st_mtime))).encode())
        return h.hexdigest()[:12]
    here = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()[:12]


def control_socket(home):
    """Path of the daemon's control socket. Unix socket paths are limited to about 100 bytes, so a long home
    directory gets a short path in /tmp instead (named after the home, so each home keeps its own daemon)."""
    path = os.path.join(home, "ctl.sock")
    if len(path.encode()) < 100 or sys.platform.startswith("win"):
        return path
    digest = hashlib.sha1(os.path.abspath(home).encode()).hexdigest()[:12]
    return "/tmp/dsidbg-%d-%s.sock" % (os.getuid(), digest)
