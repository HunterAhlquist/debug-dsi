# SPDX-License-Identifier: MIT
"""MCP server: exposes the daemon's device commands as tools for LLM harnesses (Claude Code, Codex, ...).

Speaks MCP's JSON-RPC 2.0 over stdio, one message per line, with the standard library only. Each tool call is one
request to the daemon (started in the background if it isn't running). Nothing but protocol messages may go to
stdout; diagnostics go to stderr.
"""
import base64
import json
import os
import sys
import time
import traceback

from . import DSIDBG_VERSION, cli, proto

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """\
Tools for a Nintendo DSi running homebrew with the dsidbg agent, connected over Wi-Fi to this computer's dsidbg
daemon. Typical loop: push_file(local .nds, launch=true) -> screenshot / press / read_log -> last_crash when a
command reports that the device crashed. Device paths always start with sd:/ (the SD card); nothing else is allowed.
Frames are counted by the app's dsidbg_frame() calls, about 60 per second. Buttons: A B X Y L R START SELECT UP DOWN
LEFT RIGHT. push_file with launch, launch and reboot restart the console."""


class ToolError(Exception):
    pass


def text(s):
    return {"type": "text", "text": s}


def _wait_new(generation, wait, app=None):
    w = cli.call({"cmd": "wait", "newer_than": generation, "timeout": wait, "app": app})
    return "%s %s connected" % (w["app"], w["build"])


def _sd(path):
    if not isinstance(path, str) or not path.startswith("sd:/") or ".." in path:
        raise ToolError("device paths must start with sd:/ and must not contain '..' (got %r)" % (path,))
    return path


class Server:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.log_since = None
        self.crash_after = time.time()
        self.tools = self.define_tools()

    # -- tool definitions ----------------------------------------------------------------------------------------
    def define_tools(self):
        def tool(name, description, props=None, required=(), handler=None, read_only=False, destructive=False):
            schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
            if required:
                schema["required"] = list(required)
            return {"name": name, "description": description, "inputSchema": schema,
                    "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive},
                    "_handler": handler}

        num = lambda d, **kw: dict({"type": "number", "description": d}, **kw)
        integer = lambda d, **kw: dict({"type": "integer", "description": d}, **kw)
        string = lambda d, **kw: dict({"type": "string", "description": d}, **kw)
        boolean = lambda d, **kw: dict({"type": "boolean", "description": d}, **kw)
        elf = string("ELF of the running app, to name functions (default: the one remembered for the app)")
        wait = num("Seconds to wait for the console to reconnect afterwards (0 = don't wait)", default=60, minimum=0)

        return [
            tool("status", "Whether a DSi is connected; its app, build, frame rate and agent settings.",
                 handler=self.t_status, read_only=True),
            tool("wait_for_device", "Wait until a DSi connects (optionally a given app, or a new connection after a "
                 "restart).",
                 {"app": string("Only this app name, e.g. dsidbg-loader"),
                  "after_restart": boolean("Only a connection newer than the current one", default=False),
                  "timeout_seconds": num("How long to wait", default=60, minimum=0)},
                 handler=self.t_wait, read_only=True),
            tool("press", "Press buttons on the DSi. Each entry is KEYS[@DELAY][+FRAMES]: 'A' (6 frames), "
                 "'A+B+10' (A and B for 10 frames), 'START@60+5' (in 60 frames, for 5). Buttons: A B X Y L R START "
                 "SELECT UP DOWN LEFT RIGHT.",
                 {"presses": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                              "description": "Press specs, e.g. [\"DOWN\", \"A@10\"]"},
                  "replace_real_input": boolean("Ignore the physical buttons while these are held", default=False),
                  "wait": boolean("Return only after the presses have finished", default=True)},
                 ("presses",), self.t_press),
            tool("touch", "Touch the bottom screen at (x, y).",
                 {"x": integer("0-255", minimum=0, maximum=255), "y": integer("0-191", minimum=0, maximum=191),
                  "frames": integer("How long to hold", default=6, minimum=1),
                  "delay_frames": integer("Frames before touching", default=0, minimum=0),
                  "wait": boolean("Return only after the touch has ended", default=True)},
                 ("x", "y"), self.t_touch),
            tool("release_input", "Cancel all pending injected presses and touches.", handler=self.t_release),
            tool("screenshot", "Capture a DSi screen (256x192) as an image. The bottom screen needs the app's "
                 "screenshot hook.",
                 {"screen": string("Which screen", enum=["top", "bottom"], default="top")},
                 handler=self.t_screenshot, read_only=True),
            tool("read_log", "The app's log output (dsidbg_log, printf when captured, crash notices). By default "
                 "only lines that arrived since the previous read_log call.",
                 {"only_new": boolean("Only lines since the previous call", default=True),
                  "max_lines": integer("Most recent lines to return at most", default=200, minimum=1)},
                 handler=self.t_log, read_only=True),
            tool("push_file", "Copy a local file to the DSi's SD card. With launch=true, reboot the console into "
                 "it (an .nds build) and wait for it to connect.",
                 {"local_path": string("File on this computer"),
                  "sd_path": string("Destination, default sd:/dsidbg/apps/<file name>"),
                  "launch": boolean("Reboot into the file afterwards", default=False),
                  "wait_seconds": wait,
                  "elf_path": string("ELF matching the build; remembered for crash reports and profiles")},
                 ("local_path",), self.t_push, destructive=True),
            tool("launch", "Reboot the DSi into an .nds file on the SD card (through Unlaunch) and wait for it to "
                 "connect.", {"sd_path": string("e.g. sd:/dsidbg/apps/game.nds"), "wait_seconds": wait},
                 ("sd_path",), self.t_launch, destructive=True),
            tool("reboot", "Reboot the DSi into Unlaunch's default app (normally the dsidbg loader).",
                 {"wait_seconds": wait}, handler=self.t_reboot, destructive=True),
            tool("list_files", "List a directory on the SD card.",
                 {"sd_path": string("Directory", default="sd:/")}, handler=self.t_ls, read_only=True),
            tool("get_file", "Copy a file from the SD card to this computer.",
                 {"sd_path": string("File on the SD card"), "local_path": string("Destination on this computer")},
                 ("sd_path", "local_path"), self.t_get),
            tool("delete_file", "Delete a file (not a directory) on the SD card.",
                 {"sd_path": string("File on the SD card")}, ("sd_path",), self.t_rm, destructive=True),
            tool("app_command", "Send a text command to the running app's command handler and return its reply.",
                 {"text": string("Command line, app-defined")}, ("text",), self.t_cmd),
            tool("profile", "Sample where the CPU spends its time for some seconds (1 kHz) and return the top "
                 "functions.",
                 {"seconds": num("How long to sample", default=5, minimum=0.1, maximum=120),
                  "elf_path": elf, "top": integer("Functions to list", default=25, minimum=1)},
                 handler=self.t_profile, read_only=True),
            tool("last_crash", "The most recent crash or hang report: kind, registers, source line and a "
                 "backtrace guess from the stack.",
                 {"only_new": boolean("Only a crash reported since the previous last_crash call", default=False),
                  "wait_seconds": num("Wait up to this long for one", default=0, minimum=0),
                  "elf_path": elf},
                 handler=self.t_crash, read_only=True),
            tool("set_watchdog", "Seconds without dsidbg_frame() before the app is reported as hung (0 = off).",
                 {"seconds": integer("Seconds", minimum=0)}, ("seconds",), self.t_watchdog),
            tool("read_data", "App-defined binary data sent with dsidbg_send(), as hex.",
                 {"channel": integer("Channel number", minimum=0), "count": integer("Most recent items", default=1)},
                 ("channel",), self.t_data, read_only=True),
        ]

    # -- handlers ------------------------------------------------------------------------------------------------
    def t_status(self, a):
        st = cli.call({"cmd": "status"})
        if not st["connected"]:
            return [text("No DSi connected (daemon on port %d). Turn the console on with the dsidbg loader, or "
                         "wait_for_device." % st["port"])]
        s, info = st.get("status", {}), st.get("info", {})
        lines = ["%s %s from %s, connected %ds" % (st["app"], st["build"], st["addr"], time.time() - st["connected_at"])]
        if s:
            lines.append("frame %s, %s fps, uptime %ss" % (s.get("frame"), s.get("fps"), int(s.get("uptime_ms", 0)) // 1000))
        if info:
            lines.append(" ".join("%s=%s" % kv for kv in sorted(info.items())))
        return [text("\n".join(lines))]

    def t_wait(self, a):
        newer = cli.call({"cmd": "status"})["generation"] if a.get("after_restart") else 0
        return [text(_wait_new(newer, a.get("timeout_seconds", 60), a.get("app")))]

    def t_press(self, a):
        records, end = [], 0
        for spec in a["presses"]:
            delay, frames, keys = proto.parse_press(spec, 6)
            records.append({"delay": delay, "frames": frames, "keys": keys,
                            "flags": proto.INPUT_REPLACE if a.get("replace_real_input") else 0})
            end = max(end, delay + frames)
        r = cli.call({"cmd": "input", "records": records})
        if a.get("wait", True):
            time.sleep(end / 59.8 + 0.1)
        return [text("pressed %s (device %s, %d frames)" % (", ".join(a["presses"]), r["reply"], end))]

    def t_touch(self, a):
        frames, delay = a.get("frames", 6), a.get("delay_frames", 0)
        cli.call({"cmd": "input", "records": [{"delay": delay, "frames": frames, "keys": proto.KEYS["TOUCH"],
                                               "x": a["x"], "y": a["y"]}]})
        if a.get("wait", True):
            time.sleep((delay + frames) / 59.8 + 0.1)
        return [text("touched %d,%d for %d frames" % (a["x"], a["y"], frames))]

    def t_release(self, a):
        cli.call({"cmd": "input_clear"})
        return [text("cleared pending input")]

    def t_screenshot(self, a):
        screen = a.get("screen", "top")
        r = cli.call({"cmd": "shot", "screen": 1 if screen == "bottom" else 0})
        with open(r["file"], "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return [{"type": "image", "data": data, "mimeType": "image/png"},
                text("%s screen, %dx%d, saved to %s" % (screen, r["width"], r["height"], r["file"]))]

    def t_log(self, a):
        lines, max_lines = [], a.get("max_lines", 200)
        req = {"cmd": "log", "lines": max_lines}
        if a.get("only_new", True) and self.log_since is not None:
            req["since"] = self.log_since
        r = cli.call(req, lambda m: lines.append(m["text"]))
        self.log_since = r.get("last", self.log_since)
        body = "".join(lines)
        return [text(body if body else "(no new log lines)")]

    def t_push(self, a):
        local = os.path.abspath(os.path.expanduser(a["local_path"]))
        remote = _sd(a.get("sd_path") or "sd:/dsidbg/apps/" + os.path.basename(local))
        r = cli.call({"cmd": "put", "local": local, "remote": remote})
        out = ["%s: %d bytes in %.1f s" % (remote, r["bytes"], r["seconds"])]
        if a.get("elf_path"):
            app = os.path.splitext(os.path.basename(local))[0]
            cli.call({"cmd": "elf", "app": app, "path": os.path.abspath(os.path.expanduser(a["elf_path"]))})
            out.append("ELF remembered for app %r" % app)
        if a.get("launch"):
            out.append(self._reboot(remote, a.get("wait_seconds", 60)))
        return [text("\n".join(out))]

    def _reboot(self, path, wait):
        r = cli.call({"cmd": "reboot", "path": path})
        msg = "rebooting into %s" % (path or "Unlaunch's default app")
        if wait:
            msg += "; " + _wait_new(r["generation"], wait)
        return msg

    def t_launch(self, a):
        return [text(self._reboot(_sd(a["sd_path"]), a.get("wait_seconds", 60)))]

    def t_reboot(self, a):
        return [text(self._reboot(None, a.get("wait_seconds", 60)))]

    def t_ls(self, a):
        entries = cli.call({"cmd": "list", "path": _sd(a.get("sd_path", "sd:/"))})["entries"]
        rows = ["%s %10s  %s" % ("d" if e["dir"] else "-", "" if e["dir"] else e["size"], e["name"]) for e in entries]
        return [text("\n".join(rows) or "(empty)")]

    def t_get(self, a):
        out = os.path.abspath(os.path.expanduser(a["local_path"]))
        r = cli.call({"cmd": "get", "remote": _sd(a["sd_path"]), "out": out})
        return [text("%s (%d bytes)" % (r["file"], r["bytes"]))]

    def t_rm(self, a):
        return [text(cli.call({"cmd": "delete", "path": _sd(a["sd_path"])})["reply"])]

    def t_cmd(self, a):
        return [text(cli.call({"cmd": "cmd", "text": a["text"]})["reply"])]

    def t_profile(self, a):
        cli.call({"cmd": "prof_start"})
        time.sleep(a.get("seconds", 5))
        r = cli.call({"cmd": "prof_stop", "elf": a.get("elf_path"), "top": a.get("top", 25)})
        total = r["samples"] or 1
        rows = ["%d samples at %d Hz" % (r["samples"], r["hz"])]
        rows += ["%6.2f%%  %6d  %s" % (100.0 * n / total, n, name) for name, n in r["top"]]
        return [text("\n".join(rows))]

    def t_crash(self, a):
        req = {"cmd": "crash", "wait": a.get("wait_seconds", 0), "elf": a.get("elf_path")}
        if a.get("only_new"):
            req["after"] = self.crash_after
        r = cli.call(req)
        self.crash_after = time.time()
        return [text(r["text"])]

    def t_watchdog(self, a):
        return [text(cli.call({"cmd": "watchdog", "seconds": a["seconds"]})["reply"])]

    def t_data(self, a):
        items = cli.call({"cmd": "data", "channel": a["channel"], "count": a.get("count", 1)})["items"]
        rows = ["%s  %s" % (time.strftime("%H:%M:%S", time.localtime(i["time"])), i["hex"]) for i in items]
        return [text("\n".join(rows) or "(no data on this channel yet)")]

    # -- protocol ------------------------------------------------------------------------------------------------
    def send(self, msg):
        self.out.write(json.dumps(msg) + "\n")
        self.out.flush()

    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification (initialized, cancelled, ...): nothing to answer
            return
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "dsidbg", "version": DSIDBG_VERSION},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{k: v for k, v in t.items() if k != "_handler"} for t in self.tools]}
            elif method == "tools/call":
                result = self.call_tool(msg.get("params") or {})
            else:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method %s" % method}})
                return
            self.send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:  # never let one request kill the server
            traceback.print_exc(file=sys.stderr)
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})

    def call_tool(self, params):
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in self.tools if t["name"] == name), None)
        if tool is None:
            return {"content": [text("unknown tool %r" % name)], "isError": True}
        try:
            cli.ensure_daemon()
            return {"content": tool["_handler"](args)}
        except (cli.CliError, ToolError, ValueError, OSError, KeyError) as e:
            return {"content": [text("%s" % e)], "isError": True}

    def run(self, inp=sys.stdin):
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                self.handle(m)


def main():
    Server().run()
