# SPDX-License-Identifier: MIT
"""End-to-end tests: the real daemon and CLI against the simulator, which runs the same agent code as the DSi."""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from dsidbg import DSIDBG_VERSION, cli, proto  # noqa: E402

SIM = os.path.join(ROOT, "build", "dsidbg-sim")
DSIDBG = os.path.join(ROOT, "bin", "dsidbg")


def read(path):
    with open(path) as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SIM):
            raise unittest.SkipTest("build the simulator first: make sim")
        cls.tmp = tempfile.mkdtemp(prefix="dd")
        cls.home = os.path.join(cls.tmp, "h")
        cls.sd = os.path.join(cls.tmp, "sd")
        os.makedirs(cls.sd)
        os.environ["DSIDBG_HOME"] = cls.home
        os.environ["DSIDBG_SILENCE_TIMEOUT"] = "2"
        cls.port = free_port()
        cls.run_cli("serve", "--port", str(cls.port), "--bind", "127.0.0.1")
        with open(os.path.join(cls.home, "key")) as f:
            cls.key = f.read().strip()
        cls.sim = None

    @classmethod
    def tearDownClass(cls):
        cls.stop_sim()
        cls.run_cli("stop", check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_cli(cls, *args, check=True):
        r = subprocess.run([DSIDBG] + list(args), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, DSIDBG_HOME=cls.home))
        if check and r.returncode != 0:
            raise AssertionError("dsidbg %s failed: %s%s" % (" ".join(args), r.stdout, r.stderr))
        return r

    @classmethod
    def start_sim(cls, *extra, key=None, wait=True):
        cls.stop_sim()
        gen = cli.call({"cmd": "status"})["generation"]
        cls.sim = subprocess.Popen([SIM, "--host", "127.0.0.1", "--port", str(cls.port), "--key", key or cls.key,
                                    "--root", cls.sd] + list(extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        if wait:
            cli.call({"cmd": "wait", "newer_than": gen, "timeout": 10})

    @classmethod
    def stop_sim(cls):
        if cls.sim:
            if cls.sim.poll() is None:
                cls.sim.kill()
            cls.sim.communicate()
        cls.sim = None

    def log_contains(self, text, seconds=3):
        deadline = time.time() + seconds
        while time.time() < deadline:
            out = self.run_cli("log", "-n", "200").stdout
            if text in out:
                return out
            time.sleep(0.1)
        self.fail("%r not in log:\n%s" % (text, out))

    # -----------------------------------------------------------------------------------------------------------

    def test_01_connect_status_ping(self):
        self.start_sim()
        out = self.run_cli("status").stdout
        self.assertIn("sim sim-build", out)
        self.assertIn("lib=" + DSIDBG_VERSION, out)
        self.assertIn("ms", self.run_cli("ping").stdout)
        self.log_contains("sim started")

    def test_02_wrong_key_is_rejected(self):
        gen = cli.call({"cmd": "status"})["generation"]
        bad = subprocess.Popen([SIM, "--host", "127.0.0.1", "--port", str(self.port), "--key", "wrong",
                                "--root", self.sd, "--name", "intruder"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        try:
            with self.assertRaises(cli.CliError):
                cli.call({"cmd": "wait", "app": "intruder", "newer_than": gen, "timeout": 2})
        finally:
            bad.kill()
            bad.communicate()
        with open(os.path.join(self.home, "daemon.log")) as f:
            self.assertIn("rejected this host: the keys differ", f.read())
        self.assertEqual(cli.call({"cmd": "status"})["app"], "sim")

    def test_03_input(self):
        self.run_cli("press", "A+B@0+3", "--wait")
        self.log_contains("keys held=0x3")
        self.log_contains("keys held=0x0")
        self.run_cli("touch", "100", "50", "--wait")
        self.log_contains("touch=100,50")
        self.assertEqual(proto.parse_press("START@60+5", 6), (60, 5, proto.KEYS["START"]))
        self.assertEqual(proto.parse_press("A+B+10", 6), (0, 10, 3))
        self.assertEqual(proto.parse_press("A+B", 6), (0, 6, 3))

    def test_04_screenshot(self):
        out = os.path.join(self.tmp, "shot.png")
        self.run_cli("shot", "-o", out)
        with open(out, "rb") as f:
            png = f.read()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (256, 192))
        raw = zlib.decompress(png[png.index(b"IDAT") + 4:])
        # Last pixel of the first row: the simulator's gradient has full red there.
        self.assertEqual(raw[1 + 255 * 3], 255)

    def test_05_files(self):
        data = os.urandom(100000)
        local = os.path.join(self.tmp, "game.nds")
        with open(local, "wb") as f:
            f.write(data)
        self.run_cli("push", local, "--to", "sd:/games/game.nds")
        with open(os.path.join(self.sd, "games", "game.nds"), "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "games", "game.nds.part")))
        back = os.path.join(self.tmp, "back.nds")
        self.run_cli("get", "sd:/games/game.nds", "-o", back)
        with open(back, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertIn("100000  game.nds", self.run_cli("ls", "sd:/games").stdout)
        self.run_cli("rm", "sd:/games/game.nds")
        self.assertFalse(os.path.exists(os.path.join(self.sd, "games", "game.nds")))

    def test_06_only_sd_paths(self):
        # The CLI refuses, and so does the device when asked directly.
        self.assertNotEqual(self.run_cli("ls", "nand:/", check=False).returncode, 0)
        for path in ("nand:/title", "sd:/../x", "fat:/x", "/etc/passwd"):
            with self.assertRaisesRegex(cli.CliError, "sd:/"):
                cli.call({"cmd": "list", "path": path})
            with self.assertRaisesRegex(cli.CliError, "sd:/"):
                cli.call({"cmd": "put", "local": __file__, "remote": path})
            with self.assertRaisesRegex(cli.CliError, "sd:/"):
                cli.call({"cmd": "reboot", "path": path})

    def test_07_app_command_and_data(self):
        self.assertEqual(self.run_cli("cmd", "echo", "hello", "there").stdout.strip(), "hello there")
        self.assertNotEqual(self.run_cli("cmd", "nope", check=False).returncode, 0)
        time.sleep(1.1)
        out = self.run_cli("data", "1").stdout
        self.assertRegex(out, r"\d\d:\d\d:\d\d  [0-9a-f]{8}")

    def test_08_profiler(self):
        r = cli.call({"cmd": "prof_start"})
        time.sleep(0.5)
        r = cli.call({"cmd": "prof_stop"})
        self.assertGreater(r["samples"], 50)
        buckets = {name for name, _ in r["top"]}
        self.assertEqual(buckets, {"02000000", "02000100", "02000200", "02000300", "02000400"})

    def test_09_crash_report_and_reboot_from_panic(self):
        t0 = time.time()
        with self.assertRaisesRegex(cli.CliError, "the device crashed: data abort at pc 02001234"):
            cli.call({"cmd": "cmd", "text": "crash", "reply_timeout": 20})
        self.assertLess(time.time() - t0, 5)  # failed as soon as the report arrived, not at the timeout
        r = cli.call({"cmd": "crash", "wait": 5})
        self.assertEqual(r["crash"]["kind_name"], "data abort")
        self.assertEqual(r["crash"]["regs"][15], 0x02001234)
        self.assertEqual(r["crash"]["fault_addr"], 0x10000000)
        self.assertTrue(os.path.exists(r["crash"]["file"]))
        # The agent keeps serving the daemon after a crash: launch a build from the panic screen.
        os.makedirs(os.path.join(self.sd, "dsidbg", "apps"), exist_ok=True)
        with open(os.path.join(self.sd, "dsidbg", "apps", "next.nds"), "wb") as f:
            f.write(b"x")
        self.run_cli("launch", "sd:/dsidbg/apps/next.nds")
        self.assertEqual(self.sim.wait(5), 4)
        self.assertIn("SIM REBOOT sd:/dsidbg/apps/next.nds", self.sim.stdout.read())
        with open(os.path.join(self.sd, "dsidbg", "last.txt")) as f:
            self.assertEqual(f.read(), "sd:/dsidbg/apps/next.nds")

    def test_10_hang_watchdog(self):
        self.start_sim("--watchdog", "1")
        t0 = time.time()
        self.assertIn("hanging", self.run_cli("cmd", "hang").stdout)
        # --new waits for this hang, not the data abort reported earlier
        r = cli.call({"cmd": "crash", "after": t0, "wait": 5})
        self.assertEqual(r["crash"]["kind_name"], "hang (watchdog)")
        self.assertEqual(r["crash"]["msg"], "no dsidbg_frame() for 1 s")
        self.assertIn("hang (watchdog)", self.run_cli("crash").stdout)
        self.assertIn("no new crash", self.run_cli("crash", "--new", check=False).stderr)
        self.assertEqual(r["crash"]["regs"][15], 0x02004000)

    def test_11_saved_crash_is_uploaded(self):
        self.stop_sim()
        crash_dir = os.path.join(self.sd, "dsidbg", "crash")
        os.makedirs(crash_dir, exist_ok=True)
        regs = list(range(16))
        regs[15] = 0x0200BEEF
        blob = proto.CRASH_STRUCT.pack(2, *regs, 0x30, 0x0200BEEF, 77, 0x02FF3000, 4, 0, b"from sd") + b"\x01\x02\x03\x04"
        with open(os.path.join(crash_dir, "000.bin"), "wb") as f:
            f.write(blob)
        self.start_sim()
        deadline = time.time() + 5
        while time.time() < deadline:
            r = cli.call({"cmd": "crash"})
            if r["crash"]["from_file"]:
                break
            time.sleep(0.2)
        c = r["crash"]
        self.assertTrue(c["from_file"])
        self.assertEqual((c["kind_name"], c["regs"][15], c["frame"], c["msg"], c["stack"]),
                         ("undefined instruction", 0x0200BEEF, 77, "from sd", "01020304"))
        self.assertTrue(c["thumb"])
        self.assertTrue(os.path.exists(os.path.join(crash_dir, "000.bin.sent")))
        self.assertFalse(os.path.exists(os.path.join(crash_dir, "000.bin")))

    def test_12_logs_survive_failed_connects(self):
        # Start the simulator before anything listens on its port: its first lines must still arrive.
        port = free_port()
        sim = subprocess.Popen([SIM, "--host", "127.0.0.1", "--port", str(port), "--key", self.key, "--root", self.sd,
                                "--name", "early"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(2.5)  # at least one failed connect and retry
            gen = cli.call({"cmd": "status"})["generation"]
            relay = socket.socket()
            relay.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            relay.bind(("127.0.0.1", port))
            relay.listen(1)

            def forward():  # relay the late connection to the daemon
                import threading
                a, _ = relay.accept()
                b = socket.create_connection(("127.0.0.1", self.port))

                def pipe(x, y):
                    try:
                        while True:
                            d = x.recv(65536)
                            if not d:
                                break
                            y.sendall(d)
                    except OSError:
                        pass
                    for s in (x, y):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        s.close()
                threading.Thread(target=pipe, args=(a, b), daemon=True).start()
                threading.Thread(target=pipe, args=(b, a), daemon=True).start()

            import threading
            threading.Thread(target=forward, daemon=True).start()
            cli.call({"cmd": "wait", "app": "early", "newer_than": gen, "timeout": 10})
            logs = os.path.join(self.home, "logs")
            deadline, text = time.time() + 3, ""
            while time.time() < deadline and "sim started" not in text:
                time.sleep(0.1)
                text = "".join(read(os.path.join(logs, n)) for n in os.listdir(logs) if n.endswith("-early.log"))
            self.assertIn("sim started", text)
        finally:
            sim.kill()
            sim.communicate()
            relay.close()
        self.start_sim()

    def test_13_silent_device_is_dropped(self):
        # A console that resets never closes its connection; the daemon notices the silence.
        import signal
        self.start_sim()
        self.sim.send_signal(signal.SIGSTOP)
        try:
            deadline = time.time() + 6
            while time.time() < deadline and cli.call({"cmd": "status"})["connected"]:
                time.sleep(0.2)
            self.assertFalse(cli.call({"cmd": "status"})["connected"])
        finally:
            self.sim.send_signal(signal.SIGCONT)
        self.start_sim()

    def test_15_discovery_without_a_host(self):
        self.start_sim("--host", "")
        st = cli.call({"cmd": "status"})
        self.assertEqual(st["info"]["host_from"], "discovery")
        self.log_contains("found the computer at 127.0.0.1:%d by discovery" % self.port)

    def test_16_stale_host_falls_back_to_discovery(self):
        # The computer's address changed: the configured one doesn't answer, discovery finds the daemon.
        gen = cli.call({"cmd": "status"})["generation"]
        self.stop_sim()
        self.sim = subprocess.Popen([SIM, "--host", "127.0.0.2", "--port", str(self.port), "--key", self.key,
                                     "--root", self.sd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        w = cli.call({"cmd": "wait", "newer_than": gen, "timeout": 15})
        self.assertEqual(w["app"], "sim")
        self.assertEqual(cli.call({"cmd": "status"})["info"]["host_from"], "discovery")

    def test_17_discovery_ignores_other_keys(self):
        gen = cli.call({"cmd": "status"})["generation"]
        other = subprocess.Popen([SIM, "--host", "", "--port", str(self.port), "--key", "another key", "--root",
                                  self.sd, "--name", "stranger"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with self.assertRaises(cli.CliError):  # it hears the daemon but rejects its signed answers
                cli.call({"cmd": "wait", "app": "stranger", "newer_than": gen, "timeout": 3})
        finally:
            other.kill()
            other.communicate()
        self.assertIn("discovery request from 127.0.0.1", read(os.path.join(self.home, "daemon.log")))

    def test_14_status_json(self):
        st = json.loads(self.run_cli("status", "--json").stdout)
        self.assertTrue(st["connected"])
        deadline = time.time() + 3
        while time.time() < deadline and "fps" not in st.get("status", {}):
            time.sleep(0.3)
            st = cli.call({"cmd": "status"})
        self.assertGreater(float(st["status"]["fps"]), 20)


class McpClient:
    """Minimal MCP client over the server's stdio."""

    def __init__(self, env):
        self.proc = subprocess.Popen([DSIDBG, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        msg = json.loads(self.proc.stdout.readline())
        assert msg["id"] == self.next_id, msg
        return msg

    def tool(self, name, **args):
        r = self.request("tools/call", {"name": name, "arguments": args})["result"]
        texts = "".join(c.get("text", "") for c in r["content"] if c["type"] == "text")
        return r, texts

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        self.proc.stderr.close()


class EndToEndMcp(EndToEnd):
    """The MCP server against the simulator (reuses EndToEnd's daemon setup; only these tests run here)."""

    def setUp(self):
        if not self.sim or self.sim.poll() is not None:
            self.start_sim()
        self.mcp = McpClient(dict(os.environ, DSIDBG_HOME=self.home))

    def tearDown(self):
        self.mcp.close()

    def test_mcp_session(self):
        init = self.mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("sd:/", init["instructions"])
        names = {t["name"] for t in self.mcp.request("tools/list")["result"]["tools"]}
        self.assertTrue({"status", "press", "screenshot", "read_log", "push_file", "last_crash"} <= names)

        r, t = self.mcp.tool("status")
        self.assertIn("sim sim-build", t)
        self.mcp.tool("read_log")  # consume the backlog
        r, t = self.mcp.tool("press", presses=["A+B+3"])
        self.assertFalse(r.get("isError"), t)
        deadline = time.time() + 3
        seen = ""
        while "held=0x3" not in seen and time.time() < deadline:
            seen += self.mcp.tool("read_log")[1]
        self.assertIn("keys held=0x3", seen)
        r, t = self.mcp.tool("screenshot")
        img = next(c for c in r["content"] if c["type"] == "image")
        import base64
        self.assertEqual(img["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(img["data"])[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.mcp.tool("app_command", text="echo via mcp")[1], "via mcp")
        r, t = self.mcp.tool("list_files", sd_path="nand:/")
        self.assertTrue(r["isError"])
        self.assertIn("sd:/", t)
        r, t = self.mcp.tool("profile", seconds=0.5)
        self.assertIn("samples at 1000 Hz", t)

        # A crashing command fails at once and the report is available.
        r, t = self.mcp.tool("app_command", text="crash")
        self.assertTrue(r["isError"])
        self.assertIn("the device crashed: data abort", t)
        r, t = self.mcp.tool("last_crash", only_new=True, wait_seconds=5)
        self.assertIn("data abort", t)
        self.assertIn("02001234", t)
        r, t = self.mcp.tool("last_crash", only_new=True)
        self.assertTrue(r["isError"])
        self.stop_sim()

    def test_mcp_errors(self):
        r = self.mcp.request("tools/call", {"name": "nope", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("error", self.mcp.request("no/such/method"))
        r, t = self.mcp.tool("press", presses=["BOGUS"])
        self.assertTrue(r["isError"])
        self.assertIn("unknown button", t)


# Run only the MCP tests in this subclass, not EndToEnd's again.
for _name in [n for n in dir(EndToEnd) if n.startswith("test_")]:
    setattr(EndToEndMcp, _name, None)


class Install(unittest.TestCase):
    """mcp-install with fake claude/codex commands and a throwaway home directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddi")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.tmp, "calls.txt")
        for tool in ("claude", "codex"):
            path = os.path.join(self.bin, tool)
            with open(path, "w") as f:
                # `mcp get` succeeds only once `mcp add` has been called; every call is logged.
                f.write('#!/bin/sh\necho "%s $*" >> %s\n' % (tool, self.calls) +
                        'case "$2" in get) [ -f %s/%s.added ] && exit 0; exit 1;; '
                        'add) touch %s/%s.added;; remove) rm -f %s/%s.added;; esac\nexit 0\n'
                        % (self.tmp, tool, self.tmp, tool, self.tmp, tool))
            os.chmod(path, 0o755)
        if sys.platform == "darwin":
            self.desktop = os.path.join(self.tmp, "Library", "Application Support", "Claude",
                                        "claude_desktop_config.json")
        else:
            self.desktop = os.path.join(self.tmp, ".config", "Claude", "claude_desktop_config.json")
        os.makedirs(os.path.dirname(self.desktop))
        with open(self.desktop, "w") as f:
            json.dump({"preferences": {"keep": True}, "mcpServers": {"other": {"command": "x"}}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args, desktop_running="0"):
        env = dict(os.environ, HOME=self.tmp, PATH=self.bin + ":/usr/bin:/bin",
                   DSIDBG_CLAUDE_DESKTOP_RUNNING=desktop_running)
        env.pop("DSIDBG_HOME", None)
        r = subprocess.run([DSIDBG, "mcp-install"] + list(args), capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_install_and_remove(self):
        server = os.path.join(ROOT, "bin", "dsidbg")
        out = self.run_install()
        self.assertIn("added to Claude Code", out)
        self.assertIn("added to Codex", out)
        self.assertIn("added to Claude Desktop", out)
        with open(self.calls) as f:
            calls = f.read().splitlines()
        self.assertIn("claude mcp add -s user dsidbg -- %s mcp" % server, calls)
        self.assertIn("codex mcp add dsidbg -- %s mcp" % server, calls)
        with open(self.desktop) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["preferences"], {"keep": True})
        self.assertEqual(cfg["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(cfg["mcpServers"]["dsidbg"], {"command": server, "args": ["mcp"]})
        self.assertTrue(os.path.exists(self.desktop + ".bak"))

        # Installing again replaces the entries instead of failing on duplicates.
        self.run_install()
        with open(self.calls) as f:
            calls = f.read().splitlines()
        self.assertIn("claude mcp remove -s user dsidbg", calls)
        self.assertIn("codex mcp remove dsidbg", calls)

        self.run_install("--remove")
        with open(self.desktop) as f:
            cfg = json.load(f)
        self.assertNotIn("dsidbg", cfg["mcpServers"])
        self.assertIn("other", cfg["mcpServers"])

    def test_install_all_reports_each_harness(self):
        code = ("import json, sys; from dsidbg import install; "
                "print(json.dumps(install.install_all(skip_existing=True)))")
        env = dict(os.environ, HOME=self.tmp, PATH=self.bin + ":/usr/bin:/bin",
                   PYTHONPATH=os.path.join(ROOT, "host"), DSIDBG_CLAUDE_DESKTOP_RUNNING="0")
        env.pop("DSIDBG_HOME", None)

        def run():
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return {label: outcome for label, outcome, _ in json.loads(r.stdout)}

        self.assertEqual(run(), {"Claude Code": "installed", "Codex": "installed", "Claude Desktop": "installed"})
        self.assertEqual(run(), {"Claude Code": "already", "Codex": "already", "Claude Desktop": "already"})
        shutil.rmtree(os.path.dirname(self.desktop))  # a harness that isn't there is reported, not an error
        self.assertEqual(run()["Claude Desktop"], "skipped")

    def test_claude_desktop_left_alone_while_running(self):
        before = read(self.desktop)
        out = self.run_install("--target", "claude-desktop", desktop_running="1")
        self.assertIn("skipped: Claude Desktop is running", out)
        self.assertEqual(read(self.desktop), before)

    def test_dry_run_changes_nothing(self):
        with open(self.desktop) as f:
            before = f.read()
        out = self.run_install("--dry-run")
        self.assertIn("(dry run)", out)
        with open(self.desktop) as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.calls) and " add " in read(self.calls))


class SdSetup(unittest.TestCase):
    def test_config_merge_keeps_wifi(self):
        existing = "# mine\nhost=1.2.3.4\nport=1\nkey=old\nssid=Home\npass=secret\nextra=1\n"
        text, kept = cli.config_text("10.0.0.5", 7710, "newkey", existing)
        self.assertIn("host=10.0.0.5\nport=7710\nkey=newkey\n", text)
        self.assertIn("ssid=Home\npass=secret\nextra=1\n", text)
        self.assertNotIn("old", text)
        self.assertEqual(kept, ["extra", "pass", "ssid"])
        text, kept = cli.config_text("10.0.0.5", 7710, "k")
        self.assertIn("# ssid=MyNetwork", text)
        self.assertEqual(kept, [])
        text, _ = cli.config_text("h", 1, "k", existing, ssid="Other", password="pw")
        self.assertIn("ssid=Other\npass=pw", text)

    def test_setup_sd_card(self):
        tmp = tempfile.mkdtemp(prefix="ddc")
        old_home = os.environ.get("DSIDBG_HOME")
        os.environ["DSIDBG_HOME"] = os.path.join(tmp, "home")
        try:
            card = os.path.join(tmp, "card")
            os.makedirs(os.path.join(card, "dsidbg"))
            with open(os.path.join(card, "dsidbg", "config.ini"), "w") as f:
                f.write("host=old\nssid=Home\npass=pw\n")
            r = cli.setup_sd(card, host="192.168.9.9")
            cfg = read(os.path.join(card, "dsidbg", "config.ini"))
            self.assertIn("host=192.168.9.9", cfg)
            self.assertIn("ssid=Home", cfg)
            self.assertEqual(r["kept"], ["pass", "ssid"])
            self.assertEqual(read(r["home_copy"]), cfg)
            loader_built = os.path.exists(os.path.join(ROOT, "build", "dsidbg-loader.nds"))
            self.assertEqual(bool(r["loader"]), loader_built)
            with self.assertRaises(cli.CliError):
                cli.setup_sd(os.path.join(tmp, "missing"))
        finally:
            if old_home is None:
                os.environ.pop("DSIDBG_HOME", None)
            else:
                os.environ["DSIDBG_HOME"] = old_home
            shutil.rmtree(tmp, ignore_errors=True)


class StaleDaemon(unittest.TestCase):
    """A daemon started from older code is replaced by the current one."""

    def test_replaced(self):
        tmp = tempfile.mkdtemp(prefix="dds")
        try:
            old_pkg = os.path.join(tmp, "old")
            shutil.copytree(os.path.join(ROOT, "host", "dsidbg"), os.path.join(old_pkg, "dsidbg"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            with open(os.path.join(old_pkg, "dsidbg", "proto.py"), "a") as f:
                f.write("# older version\n")
            env = dict(os.environ, DSIDBG_HOME=os.path.join(tmp, "h"), PYTHONPATH=old_pkg)
            port = str(free_port())
            old = subprocess.Popen([sys.executable, "-m", "dsidbg", "serve", "--foreground", "--port", port,
                                    "--bind", "127.0.0.1"], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            os.environ["DSIDBG_HOME"] = env["DSIDBG_HOME"]
            try:
                deadline = time.time() + 5
                while True:
                    try:
                        before = cli.call({"cmd": "status"})
                        break
                    except cli.CliError:
                        if time.time() > deadline:
                            raise
                        time.sleep(0.1)
                from dsidbg import code_id
                self.assertNotEqual(before["code_id"], code_id())
                st, started = cli.ensure_daemon()
                self.assertTrue(started)
                self.assertEqual(st["code_id"], code_id())
                self.assertEqual(st["port"], int(port))  # same port as the daemon it replaced
                self.assertEqual(old.wait(5), 0)
                self.assertEqual(cli.ensure_daemon()[1], False)  # up to date now: left alone
            finally:
                try:
                    cli.call({"cmd": "shutdown"})
                except cli.CliError:
                    pass
                if old.poll() is None:
                    old.kill()
                    old.wait()
        finally:
            os.environ.pop("DSIDBG_HOME", None)
            shutil.rmtree(tmp, ignore_errors=True)


class Symbolization(unittest.TestCase):
    ELF = os.path.join(ROOT, "build", "nds", "dsidbg-demo.elf")

    def test_lookup_and_crash_text(self):
        if not os.path.exists(self.ELF):
            self.skipTest("build the DSi targets first: tools/docker.sh make nds")
        from dsidbg.symbols import Symbols, format_crash
        syms = Symbols(self.ELF)
        nm = shutil.which("arm-none-eabi-nm")
        addr = None
        if nm:
            for line in subprocess.run([nm, self.ELF], capture_output=True, text=True).stdout.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[2] == "dsidbg_frame":
                    addr = int(parts[0], 16) & ~1
        if addr is None:
            addr = next(a for a, n, s, f in syms.items if n == "dsidbg_frame")
        self.assertEqual(syms.describe(addr + 4), "dsidbg_frame+0x4")
        self.assertTrue(syms.is_code(addr))
        regs = [0] * 16
        regs[15] = addr + 4
        regs[14] = addr | 1
        crash = {"kind_name": "data abort", "msg": "", "regs": regs, "fault_addr": 0, "cpsr": 0x3F, "thumb": True,
                 "frame": 1, "stack_base": 0x02FF3F00, "stack": struct.pack("<II", 0, addr | 1).hex()}
        text = format_crash(crash, syms)
        self.assertIn("dsidbg_frame+0x4", text)
        self.assertIn("sp+4", text)


if __name__ == "__main__":
    unittest.main()
