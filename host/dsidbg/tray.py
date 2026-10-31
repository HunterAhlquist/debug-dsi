# SPDX-License-Identifier: MIT
"""System tray / menu bar app: keeps the daemon running and shows whether a DSi is connected.

Menu: status (DSi and daemon addresses), Set Up SD Card..., Install MCP, Open Terminal, Quit (stops the daemon).
Needs pystray and Pillow (`dsidbg tray --setup` installs them in a private environment); everything else is native.
"""
import os
import sys
import threading
import time
import traceback

from . import FROZEN, cli, code_id, install, native

POLL_SECONDS = 2
COLORS = {"connected": (52, 199, 89), "waiting": (255, 159, 10), "down": (255, 69, 58)}


def icon_image(state):
    """A small DS (two screens) with a status light: green connected, orange waiting for a DSi, red no daemon."""
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body, screen = (128, 128, 136, 255), (230, 230, 236, 255)
    for top in (4, 34):  # upper and lower halves of the clamshell
        d.rounded_rectangle((8, top, 56, top + 26), radius=5, fill=body)
        d.rectangle((16, top + 5, 48, top + 21), fill=screen)
    d.ellipse((40, 40, 62, 62), fill=COLORS[state] + (255,), outline=(255, 255, 255, 255), width=3)
    return img


class Tray:
    def __init__(self):
        import pystray
        self.lines = ["Starting the daemon...", ""]
        self.state = "down"
        self.stopping = False
        self.code = code_id()  # to notice when the code on disk changes under us
        self.lock = threading.Lock()  # one menu action at a time (each may show a dialog)
        item = pystray.MenuItem
        self.icon = pystray.Icon("dsidbg", icon_image("down"), "dsidbg", menu=pystray.Menu(
            item(lambda _: self.lines[0], None, enabled=False),
            item(lambda _: self.lines[1], None, enabled=False, visible=lambda _: bool(self.lines[1])),
            pystray.Menu.SEPARATOR,
            item("Set Up SD Card...", self.action(self.setup_sd)),
            item("Install MCP", self.action(self.install_mcp)),
            item("Open Terminal", self.action(self.open_terminal)),
            pystray.Menu.SEPARATOR,
            item("Quit", self.quit)))
        if sys.platform == "darwin":
            try:  # menu bar only: no Dock icon
                from AppKit import NSApplication
                NSApplication.sharedApplication().setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
            except Exception:
                pass

    # -- status ------------------------------------------------------------------------------------------------
    def poll(self):
        while not self.stopping:
            if self.code_changed():
                self.restart()
            try:
                st, _ = cli.ensure_daemon()  # also restarts a daemon that died or runs older code
                daemon = "Daemon: %s:%d" % (cli.local_ip(), st["port"])
                if st["connected"]:
                    state, lines = "connected", ["DSi connected: %s (%s)" % (st["addr"], st["app"]), daemon]
                else:
                    state, lines = "waiting", ["No DSi connected", daemon]
            except Exception as e:  # keep polling whatever happens
                state, lines = "down", ["Daemon not running", str(e)[:70]]
            if (state, lines) != (self.state, self.lines):
                self.state, self.lines = state, lines
                self.on_main(self.refresh)
            time.sleep(POLL_SECONDS)

    def code_changed(self):
        """True when the repo's code was updated since this tray started. A running Python process keeps its
        modules in memory but imports others from disk later, so mixing the two breaks (e.g. Set Up SD Card)."""
        if FROZEN or self.lock.locked():  # the packaged app can't change; never restart during a menu action
            return False
        try:
            return code_id() != self.code
        except OSError:
            return False

    def restart(self):
        """Replaces this process with a fresh tray running the current code."""
        self.stopping = True
        try:
            os.remove(os.path.join(cli.home_dir(), "tray.pid"))  # Windows' exec makes a new process
        except OSError:
            pass
        cmd = cli.self_command("tray", "--foreground")
        print("%s dsidbg tray: code changed, restarting" % time.strftime("%Y-%m-%d %H:%M:%S"), file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(cmd[0], cmd, cli.self_env())

    def refresh(self):
        self.icon.icon = icon_image(self.state)
        self.icon.title = "dsidbg: " + self.lines[0]
        self.icon.update_menu()

    @staticmethod
    def on_main(fn):
        """Runs fn on the UI thread. macOS requires it for status item changes (it kills the process otherwise);
        pystray doesn't do it for us there."""
        if sys.platform == "darwin":
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
        else:
            fn()

    # -- menu actions ------------------------------------------------------------------------------------------
    def action(self, fn):
        """Runs a menu action on a worker thread (dialogs would block the menu) and reports failures."""
        def run():
            if not self.lock.acquire(blocking=False):
                return
            try:
                fn()
            except Exception as e:
                traceback.print_exc()
                native.message("dsidbg", str(e), error=True)
            finally:
                self.lock.release()
        return lambda icon, item: threading.Thread(target=run, daemon=True).start()

    def setup_sd(self):
        path = native.pick_folder("Choose the root folder of the DSi's SD card")
        if not path:
            return
        if os.path.basename(path).lower() == "dsidbg":  # picked the card's dsidbg folder instead of its root
            path = os.path.dirname(path)
        r = cli.setup_sd(path)
        msg = ["Wrote %s" % r["config"], "Computer: %s, port %d, with this computer's key." % (r["host"], r["port"])]
        if r["kept"]:
            msg.append("Kept the card's other settings: %s." % ", ".join(r["kept"]))
        else:
            msg.append("Wi-Fi: the DSi uses its saved connections (or add ssid= and pass= to the file).")
        if r["loader"]:
            msg.append("\nCopied the loader to %s. Launch it once on the DSi; in TWiLight Menu++, turn on "
                       "\"Last played ROM on startup\" so it comes back after every reboot." % r["loader"])
        else:
            msg.append("\nThe loader isn't built yet (tools/docker.sh make nds). Set up the card again after "
                       "building it.")
        native.message("SD card set up", "\n".join(msg))

    def install_mcp(self):
        words = {"installed": "installed", "already": "already installed, skipped", "removed": "removed",
                 "skipped": "not found on this computer, skipped", "failed": "failed"}
        results = install.install_all(skip_existing=True)
        lines = []
        for label, outcome, msg in results:
            if outcome == "skipped" and msg != "not found on this computer":
                lines.append("%s: skipped. %s." % (label, msg[0].upper() + msg[1:]))
            else:
                lines.append("%s: %s%s" % (label, words[outcome], ": " + msg if outcome == "failed" else ""))
        if any(o == "installed" for _, o, _ in results):
            lines.append("\nRestart open Claude Code and Codex sessions; quit and reopen Claude Desktop.")
        native.message("Install MCP", "\n".join(lines), error=any(o == "failed" for _, o, _ in results))

    def open_terminal(self):
        native.open_terminal(*cli.terminal_location())

    def quit(self, icon, item):
        self.stopping = True
        try:
            cli.call({"cmd": "shutdown"})
        except cli.CliError:
            pass
        icon.stop()

    def run(self):
        def setup(icon):  # pystray calls this on a separate thread
            self.on_main(lambda: setattr(icon, "visible", True))
            threading.Thread(target=self.poll, daemon=True).start()
        self.icon.run(setup=setup)


def single_instance():
    """Returns False if another tray app is running (pid file in the daemon's home)."""
    path = os.path.join(cli.home_dir(), "tray.pid")
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        if pid != os.getpid():
            os.kill(pid, 0)  # raises if the process is gone
            return False
    except (OSError, ValueError):
        pass
    os.makedirs(cli.home_dir(), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    return True


def main():
    if not single_instance():
        print("dsidbg tray is already running", file=sys.stderr)
        return 1
    Tray().run()
    return 0
