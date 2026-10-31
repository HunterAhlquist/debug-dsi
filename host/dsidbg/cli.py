# SPDX-License-Identifier: MIT
"""dsidbg command line. Run `dsidbg -h` for the list of commands."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import proto

START = time.time()
HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(HOST_DIR)


def home_dir():
    return os.environ.get("DSIDBG_HOME") or os.path.expanduser("~/.dsidbg")


class CliError(Exception):
    pass


def call(req, on_line=None):
    """Sends one request to the daemon. Streamed lines go to on_line; returns the final reply."""
    path = os.path.join(home_dir(), "ctl.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        raise CliError("the daemon is not running (start it with `dsidbg serve`)")
    f = s.makefile("rwb")
    f.write((json.dumps(req) + "\n").encode())
    f.flush()
    try:
        for line in f:
            msg = json.loads(line)
            if "ok" in msg:
                if not msg["ok"]:
                    raise CliError(msg.get("error", "failed"))
                return msg
            if on_line:
                on_line(msg)
    finally:
        s.close()
    raise CliError("the daemon closed the connection")


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sd_path(p):
    if not p.startswith("sd:/"):
        raise CliError("device paths must start with sd:/ (the agent never touches NAND): %s" % p)
    return p


# -- commands ---------------------------------------------------------------------------------------------------

def ensure_daemon(port=proto.DEFAULT_PORT, bind="0.0.0.0"):
    """Starts the daemon in the background unless it is running. Returns (status, started). Prints nothing."""
    try:
        return call({"cmd": "status"}), False
    except CliError:
        pass
    os.makedirs(home_dir(), exist_ok=True)
    env = dict(os.environ, PYTHONPATH=HOST_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
    with open(os.path.join(home_dir(), "daemon.out"), "a") as out:
        subprocess.Popen([sys.executable, "-m", "dsidbg", "serve", "--foreground", "--port", str(port),
                          "--bind", bind], env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        try:
            return call({"cmd": "status"}), True
        except CliError:
            pass
    raise CliError("the daemon didn't start; see %s" % os.path.join(home_dir(), "daemon.out"))


def do_serve(a):
    from .daemon import Daemon
    if a.foreground:
        Daemon(home_dir(), a.port, a.bind).run()
        return
    st, started = ensure_daemon(a.port, a.bind)
    if started:
        print("daemon listening on port %d (home %s)" % (st["port"], st["home"]))
    else:
        print("the daemon is already running")


def do_stop(a):
    try:
        call({"cmd": "shutdown"})
    except CliError:
        pass
    print("stopped")


def do_setup(a):
    from .daemon import load_key
    key = load_key(home_dir())
    lines = ["# dsidbg agent settings. Copy to sd:/dsidbg/config.ini on the DSi's SD card.",
             "host=%s" % (a.host or local_ip()), "port=%d" % a.port, "key=%s" % key]
    if a.ssid:
        lines += ["ssid=%s" % a.ssid, "pass=%s" % (a.password or "")]
    else:
        lines += ["# Without ssid/pass the DSi uses its saved Wi-Fi connections (System Settings > Internet).",
                  "# ssid=MyNetwork", "# pass=MyPassword"]
    text = "\n".join(lines) + "\n"
    path = os.path.join(home_dir(), "config.ini")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)
    print(text, end="")
    print("Saved to %s" % path)
    if a.sd:
        dst = os.path.join(a.sd, "dsidbg")
        os.makedirs(dst, exist_ok=True)
        shutil.copyfile(path, os.path.join(dst, "config.ini"))
        print("Wrote %s" % os.path.join(dst, "config.ini"))
        loader = os.path.join(REPO_DIR, "build", "dsidbg-loader.nds")
        if os.path.exists(loader):
            shutil.copyfile(loader, os.path.join(dst, "dsidbg-loader.nds"))
            print("Copied the loader to %s" % os.path.join(dst, "dsidbg-loader.nds"))
        else:
            print("Build the loader first (tools/docker.sh make nds) to copy it too.")


def do_status(a):
    st = call({"cmd": "status"})
    if a.json:
        print(json.dumps(st, indent=1))
        return
    if not st["connected"]:
        print("daemon on port %d; no device connected" % st["port"])
        return
    s = st.get("status", {})
    print("%s %s from %s, connected %ds" % (st["app"], st["build"], st["addr"], time.time() - st["connected_at"]))
    if s:
        print("frame %s  fps %s  uptime %ss  log drops %s" % (s.get("frame"), s.get("fps"),
                                                             int(s.get("uptime_ms", 0)) // 1000, s.get("log_drops")))
    info = st.get("info", {})
    if info:
        print(" ".join("%s=%s" % kv for kv in sorted(info.items())))


def do_wait(a):
    r = call({"cmd": "wait", "app": a.app, "newer_than": a.newer_than, "timeout": a.timeout})
    print("%s %s connected" % (r["app"], r["build"]))


def do_ping(a):
    print("%.1f ms" % call({"cmd": "ping", "timeout": a.timeout})["ms"])


def do_press(a):
    records = []
    for spec in a.specs:
        delay, frames, keys = proto.parse_press(spec, a.frames)
        records.append({"delay": delay + a.delay, "frames": frames, "keys": keys,
                        "flags": proto.INPUT_REPLACE if a.replace else 0})
    r = call({"cmd": "input", "records": records, "timeout": a.timeout})
    end = max(rec["delay"] + rec["frames"] for rec in records)
    print("queued at device %s, done in %d frames" % (r["reply"], end))
    if a.wait:
        time.sleep(end / 59.8 + 0.1)


def do_touch(a):
    rec = {"delay": a.delay, "frames": a.frames, "keys": proto.KEYS["TOUCH"], "x": a.x, "y": a.y}
    r = call({"cmd": "input", "records": [rec], "timeout": a.timeout})
    print("queued at device %s" % r["reply"])
    if a.wait:
        time.sleep((a.delay + a.frames) / 59.8 + 0.1)


def do_release(a):
    call({"cmd": "input_clear", "timeout": a.timeout})
    print("cleared pending input")


def do_shot(a):
    r = call({"cmd": "shot", "screen": 1 if a.bottom else 0, "out": os.path.abspath(a.out) if a.out else None,
              "timeout": a.timeout})
    print(r["file"])


def launch(path, wait, timeout):
    r = call({"cmd": "reboot", "path": path, "timeout": timeout})
    print("rebooting into %s" % (path or "Unlaunch's default app"))
    if wait:
        w = call({"cmd": "wait", "newer_than": r["generation"], "timeout": wait})
        print("%s %s connected" % (w["app"], w["build"]))


def do_push(a):
    remote = sd_path(a.to or "sd:/dsidbg/apps/" + os.path.basename(a.local))
    r = call({"cmd": "put", "local": os.path.abspath(a.local), "remote": remote, "timeout": a.timeout})
    print("%s: %d bytes in %.1f s (%.0f KiB/s)" % (remote, r["bytes"], r["seconds"], r["kib_per_s"]))
    if a.elf:
        call({"cmd": "elf", "app": a.app or os.path.splitext(os.path.basename(a.local))[0], "path": a.elf})
    if a.launch:
        launch(remote, a.wait, a.timeout)


def do_launch(a):
    launch(sd_path(a.path), a.wait, a.timeout)


def do_reboot(a):
    launch(None, a.wait, a.timeout)


def do_get(a):
    out = os.path.abspath(a.out or os.path.basename(a.remote))
    r = call({"cmd": "get", "remote": sd_path(a.remote), "out": out, "timeout": a.timeout})
    print("%s (%d bytes)" % (r["file"], r["bytes"]))


def do_ls(a):
    for e in call({"cmd": "list", "path": sd_path(a.path), "timeout": a.timeout})["entries"]:
        print("%s %10s  %s" % ("d" if e["dir"] else "-", "" if e["dir"] else e["size"], e["name"]))


def do_rm(a):
    print(call({"cmd": "delete", "path": sd_path(a.path), "timeout": a.timeout})["reply"])


def do_cmd(a):
    print(call({"cmd": "cmd", "text": " ".join(a.text), "timeout": a.timeout})["reply"])


def do_watchdog(a):
    print(call({"cmd": "watchdog", "seconds": a.seconds, "timeout": a.timeout})["reply"])


def print_prof(r):
    total = r["samples"] or 1
    print("%d samples at %d Hz (%.1f s); raw: %s" % (r["samples"], r["hz"], r["samples"] / (r["hz"] or 1000), r["file"]))
    for name, n in r["top"]:
        print("%6.2f%%  %6d  %s" % (100.0 * n / total, n, name))


def do_prof(a):
    if a.action == "start":
        print("profiling (%s)" % call({"cmd": "prof_start", "timeout": a.timeout})["reply"])
    elif a.action == "stop":
        print_prof(call({"cmd": "prof_stop", "elf": a.elf, "top": a.top, "timeout": a.timeout}))
    else:  # run
        call({"cmd": "prof_start", "timeout": a.timeout})
        time.sleep(a.seconds)
        print_prof(call({"cmd": "prof_stop", "elf": a.elf, "top": a.top, "timeout": a.timeout}))


def do_log(a):
    def show(msg):
        sys.stdout.write(msg["text"])
        sys.stdout.flush()
    try:
        call({"cmd": "log", "lines": a.lines, "follow": a.follow or bool(a.seconds), "follow_seconds": a.seconds},
             show)
    except KeyboardInterrupt:
        pass


def do_crash(a):
    r = call({"cmd": "crash", "elf": a.elf, "wait": a.wait, "index": a.index, "after": START if a.new else 0})
    if a.json:
        print(json.dumps(r["crash"], indent=1))
    else:
        print(r["text"])


def do_data(a):
    for item in call({"cmd": "data", "channel": a.channel, "count": a.count})["items"]:
        print("%s  %s" % (time.strftime("%H:%M:%S", time.localtime(item["time"])), item["hex"]))


def do_mcp(a):
    from .mcp import main as mcp_main
    mcp_main()


def do_mcp_install(a):
    from .install import do_mcp_install as run
    run(a)


def do_elf(a):
    r = call({"cmd": "elf", "app": a.app, "path": os.path.abspath(a.path) if a.path else None})
    for app, path in sorted(r["elves"].items()):
        print("%s  %s" % (app, path))


def main(argv=None):
    p = argparse.ArgumentParser(prog="dsidbg", description="Remote debugging for DSi homebrew.")
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, fn, help_text, timeout=True):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(fn=fn)
        if timeout:
            sp.add_argument("--timeout", type=float, default=0,
                            help="seconds to wait for a device to connect first (default: fail at once)")
        return sp

    sp = cmd("serve", do_serve, "start the daemon", timeout=False)
    sp.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    sp.add_argument("--bind", default="0.0.0.0")
    sp.add_argument("--foreground", action="store_true")
    cmd("stop", do_stop, "stop the daemon", timeout=False)
    sp = cmd("setup", do_setup, "write the DSi's config.ini (host address, key, Wi-Fi)", timeout=False)
    sp.add_argument("--host", help="this computer's address as seen by the DSi (default: detected)")
    sp.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    sp.add_argument("--ssid", help="Wi-Fi network (default: the DSi's saved connections)")
    sp.add_argument("--pass", dest="password")
    sp.add_argument("--sd", help="mounted SD card: also write config.ini and the loader there")
    sp = cmd("status", do_status, "show the connected device", timeout=False)
    sp.add_argument("--json", action="store_true")
    sp = cmd("wait", do_wait, "wait for a device to connect", timeout=False)
    sp.add_argument("--app")
    sp.add_argument("--newer-than", type=int, default=0, help="connection generation that doesn't count")
    sp.add_argument("--timeout", type=float, default=60)
    cmd("ping", do_ping, "round trip to the device")

    sp = cmd("press", do_press, "press buttons: KEYS[@DELAY][+FRAMES], e.g. A, A+B+10, START@60+5")
    sp.add_argument("specs", nargs="+")
    sp.add_argument("--frames", type=int, default=6, help="default duration (frames)")
    sp.add_argument("--delay", type=int, default=0, help="frames before the first press")
    sp.add_argument("--replace", action="store_true", help="ignore the real buttons meanwhile")
    sp.add_argument("--wait", action="store_true", help="return after the presses end")
    sp = cmd("touch", do_touch, "touch the bottom screen at X Y")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.add_argument("--frames", type=int, default=6)
    sp.add_argument("--delay", type=int, default=0)
    sp.add_argument("--wait", action="store_true")
    cmd("release", do_release, "cancel pending presses")
    sp = cmd("shot", do_shot, "screenshot (PNG)")
    sp.add_argument("-o", "--out")
    sp.add_argument("--bottom", action="store_true")

    sp = cmd("push", do_push, "copy a file to the SD card (default sd:/dsidbg/apps/NAME)")
    sp.add_argument("local")
    sp.add_argument("--to")
    sp.add_argument("--launch", action="store_true", help="reboot into it afterwards")
    sp.add_argument("--wait", type=float, default=0, help="with --launch: wait up to N s for it to connect")
    sp.add_argument("--elf", help="remember this ELF for symbolizing the app's crashes and profiles")
    sp.add_argument("--app", help="app name for --elf (default: file name)")
    sp = cmd("launch", do_launch, "reboot into an .nds on the SD card")
    sp.add_argument("path")
    sp.add_argument("--wait", type=float, default=0)
    sp = cmd("reboot", do_reboot, "reboot into Unlaunch's default app (the loader)")
    sp.add_argument("--wait", type=float, default=0)
    sp = cmd("get", do_get, "copy a file from the SD card")
    sp.add_argument("remote")
    sp.add_argument("-o", "--out")
    sp = cmd("ls", do_ls, "list an SD card directory")
    sp.add_argument("path", nargs="?", default="sd:/")
    sp = cmd("rm", do_rm, "delete a file on the SD card")
    sp.add_argument("path")
    sp = cmd("cmd", do_cmd, "send a text command to the app")
    sp.add_argument("text", nargs="+")
    sp = cmd("watchdog", do_watchdog, "set the hang watchdog (seconds, 0 = off)")
    sp.add_argument("seconds", type=int)

    sp = cmd("prof", do_prof, "sampling profiler: start | stop | run SECONDS")
    sp.add_argument("action", choices=["start", "stop", "run"])
    sp.add_argument("seconds", type=float, nargs="?", default=5)
    sp.add_argument("--elf")
    sp.add_argument("--top", type=int, default=30)
    sp = cmd("log", do_log, "show device logs", timeout=False)
    sp.add_argument("-n", "--lines", type=int, default=100)
    sp.add_argument("-f", "--follow", action="store_true")
    sp.add_argument("--for", dest="seconds", type=float, default=0, help="follow for N seconds")
    sp = cmd("crash", do_crash, "show the last crash report", timeout=False)
    sp.add_argument("--elf")
    sp.add_argument("--wait", type=float, default=0, help="wait up to N s for a crash")
    sp.add_argument("--new", action="store_true", help="only a crash reported after this command started")
    sp.add_argument("--index", type=int, default=-1)
    sp.add_argument("--json", action="store_true")
    sp = cmd("data", do_data, "show app data sent with dsidbg_send()", timeout=False)
    sp.add_argument("channel", type=int)
    sp.add_argument("-n", "--count", type=int, default=1)
    sp = cmd("elf", do_elf, "remember an app's ELF for symbolization (no PATH: list)", timeout=False)
    sp.add_argument("app", nargs="?")
    sp.add_argument("path", nargs="?")

    cmd("mcp", do_mcp, "run the MCP server on stdio (started by LLM harnesses)", timeout=False)
    sp = cmd("mcp-install", do_mcp_install, "register the MCP server with Claude Code, Codex and Claude Desktop",
             timeout=False)
    sp.add_argument("--target", choices=["all", "claude-code", "codex", "claude-desktop"], default="all",
                    help="harness to configure (default: every one found)")
    sp.add_argument("--scope", choices=["user", "project", "local"], default="user",
                    help="Claude Code scope (default: user, i.e. all projects)")
    sp.add_argument("--name", default="dsidbg", help="server name in the harness")
    sp.add_argument("--remove", action="store_true", help="unregister instead")
    sp.add_argument("--dry-run", action="store_true", help="show what would change")

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (CliError, ValueError, OSError) as e:
        print("dsidbg: %s" % e, file=sys.stderr)
        return 1
    return 0
