# SPDX-License-Identifier: MIT
"""Host side of debug-dsi: the daemon the DSi connects to, the dsidbg command line and its MCP server."""
import hashlib
import os

DSIDBG_VERSION = "0.1.0"  # keep in sync with include/dsidbg.h


def code_id():
    """Fingerprint of this package's source. The daemon keeps running across updates, so the CLI and MCP server
    compare it with the daemon's to spot (and replace) a daemon started from older code."""
    h = hashlib.sha1()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()[:12]
