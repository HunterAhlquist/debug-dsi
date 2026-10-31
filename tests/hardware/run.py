#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Hardware test runner: drives a DSi connected to the dsidbg daemon through the scriptable parts of
docs/testing.md and writes a report.

    python3 tests/hardware/run.py                    # quick suite, about 5 minutes
    python3 tests/hardware/run.py --suite full       # adds 16 MiB files, 20 daemon restarts, fuzzing, log flood
    python3 tests/hardware/run.py --suite soak --soak-minutes 480
    python3 tests/hardware/run.py --suite stress --rounds 10
    python3 tests/hardware/run.py --only files,crash_abort
    python3 tests/hardware/run.py --list

Needs: the daemon (or the tray app) running, the DSi on and connected (the loader is fine), and
build/dsidbg-demo.nds with its ELF (`tools/docker.sh make nds`). The runner copies the demo to
sd:/dsidbg/apps/ and the DSi reboots several times. Test files go to sd:/dsidbg/test/ and are deleted.

Results: build/hwtest/<time>/report.md and report.json, with screenshots. The exit status is 0 only if every test
that ran passed.
"""
import argparse
import json
import os
import random
import struct
import sys
import threading
import time
import traceback
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "host"))

from dsidbg import DSIDBG_VERSION, cli, proto  # noqa: E402

DEMO_NDS = os.path.join(ROOT, "build", "dsidbg-demo.nds")
DEMO_ELF = os.path.join(ROOT, "build", "nds", "dsidbg-demo.elf")
DEMO_SD = "sd:/dsidbg/apps/dsidbg-demo.nds"
TEST_DIR = "sd:/dsidbg/test"
DEMO, LOADER = "dsidbg-demo", "dsidbg-loader"


class Fail(Exception):
    pass


class Unreachable(Exception):
    """The DSi stopped answering: the rest of the run can't continue."""


def check(cond, msg):
    if not cond:
        raise Fail(msg)


# -- daemon and device helpers ------------------------------------------------------------------------------------

def call(cmd, **kw):
    return cli.call(dict(cmd=cmd, **kw))


def status():
    return call("status")


def wait_app(app, newer=0, timeout=90):
    try:
        call("wait", app=app, newer_than=newer, timeout=timeout)
    except cli.CliError as e:
        raise Unreachable("%s didn't connect within %d s (%s): look at the DSi's screen" % (app, timeout, e))


def daemon_up(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return status()
        except cli.CliError:
            time.sleep(0.3)
    raise Unreachable("the daemon didn't come back")


def launch_demo(timeout=90):
    gen = status()["generation"]
    call("reboot", path=DEMO_SD)
    wait_app(DEMO, newer=gen, timeout=timeout)


def ensure_demo():
    st = daemon_up()
    if not st["connected"]:
        wait_app("", timeout=60)  # any app; then switch below
        st = status()
    if st.get("app") != DEMO:
        launch_demo()


class Log:
    """Reads the device log from a mark onwards."""

    def __init__(self):
        self.last = call("log", lines=0)["last"]

    def text(self):
        parts = []
        r = cli.call({"cmd": "log", "since": self.last, "lines": 100000}, lambda m: parts.append(m["text"]))
        self.last = r["last"]
        return "".join(parts)

    def wait_for(self, needle, timeout=5):
        seen, deadline = "", time.time() + timeout
        while time.time() < deadline:
            seen += self.text()
            if needle in seen:
                return seen
            time.sleep(0.2)
        raise Fail("%r not in the log (got %r)" % (needle, seen[-300:]))


def crash_after(t0, wait=20, elf=None):
    return call("crash", after=t0, wait=wait, elf=elf or DEMO_ELF)


def png_info(path):
    """(width, height, distinct colours) of an RGB PNG written by the daemon."""
    with open(path, "rb") as f:
        data = f.read()
    w, h = struct.unpack(">II", data[16:24])
    raw = zlib.decompress(data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8])
    colours = set()
    stride = 1 + w * 3
    for y in range(h):
        row = raw[y * stride + 1:(y + 1) * stride]
        colours.update(row[i:i + 3] for i in range(0, len(row), 3))
    return w, h, len(colours)


# -- tests --------------------------------------------------------------------------------------------------------

TESTS = []


def test(name, suites=("quick", "full"), needs_demo=True):
    def register(fn):
        TESTS.append({"name": name, "fn": fn, "suites": suites, "needs_demo": needs_demo,
                      "doc": (fn.__doc__ or "").strip().splitlines()[0]})
        return fn
    return register


@test("setup", needs_demo=False)
def t_setup(ctx):
    """Copy this build's demo to the card, launch it, check the agent's version and mode."""
    for f in (DEMO_NDS, DEMO_ELF):
        check(os.path.exists(f), "%s is missing: run tools/docker.sh make nds" % f)
    daemon_up()
    st = status()
    if not st["connected"]:
        wait_app("", timeout=60)
    call("put", local=DEMO_NDS, remote=DEMO_SD)
    call("elf", app=DEMO, path=DEMO_ELF)
    launch_demo()
    info = status()["info"]
    check(info.get("lib") == DSIDBG_VERSION, "agent %s, host %s" % (info.get("lib"), DSIDBG_VERSION))
    ctx["mode"] = info.get("mode")
    # Files an interrupted earlier run left behind (a crash mid-upload leaves a .part) would fail this run's checks.
    try:
        old = [e["name"] for e in call("list", path=TEST_DIR)["entries"] if not e["dir"]]
    except cli.CliError:
        old = []  # no test directory yet
    for name in old:
        call("delete", path="%s/%s" % (TEST_DIR, name))
    detail = {"mode": info.get("mode"), "device_ip": info.get("ip"), "host_from": info.get("host_from")}
    if old:
        detail["removed_leftovers"] = old
    return detail


@test("ping")
def t_ping(ctx):
    """20 round trips."""
    times = sorted(call("ping")["ms"] for _ in range(20))
    check(times[10] < 200, "median %.1f ms" % times[10])
    return {"min_ms": times[0], "median_ms": times[10], "max_ms": times[-1]}


@test("screenshot")
def t_screenshot(ctx):
    """Top screen three times (a 3D scene with many colours); the bottom screen must be refused clearly."""
    sizes, took = [], []
    for i in range(3):
        out = os.path.join(ctx["out"], "shot%d.png" % i)
        t0 = time.time()
        call("shot", screen=0, out=out)
        took.append(time.time() - t0)
        w, h, colours = png_info(out)
        check((w, h) == (256, 192), "size %dx%d" % (w, h))
        check(colours > 1, "only the background colour: blank capture?")
        sizes.append(colours)
    # The triangle spins: edge-on it is a thin line of a few colours, so only one of the shots must show it whole.
    check(max(sizes) > 10, "at most %d colours in three shots: the 3D scene is missing" % max(sizes))
    try:
        call("shot", screen=1)
        raise Fail("bottom screen didn't fail")
    except cli.CliError as e:
        check("screenshot hook" in str(e), "unexpected error: %s" % e)
    return {"colours": sizes, "seconds": [round(t, 2) for t in took]}


@test("input")
def t_input(ctx):
    """Buttons and touch reach the app (the demo logs them)."""
    log = Log()
    call("input", records=[{"delay": 0, "frames": 4, "keys": proto.parse_keys("A+B")}])
    log.wait_for("keys 0x0003")
    call("input", records=[{"delay": 0, "frames": 4, "keys": proto.KEYS["TOUCH"], "x": 100, "y": 50}])
    log.wait_for("touch 100,50")
    call("input", records=[{"delay": 30, "frames": 600, "keys": proto.KEYS["X"]}])
    call("input_clear")
    time.sleep(1)
    check("0x0400" not in log.text(), "release didn't cancel a pending press")
    return {}


@test("commands")
def t_commands(ctx):
    """App commands and their errors; the data channel."""
    check(call("cmd", text="echo hello DSi")["reply"] == "hello DSi", "echo")
    try:
        call("cmd", text="no-such-command")
        raise Fail("unknown command accepted")
    except cli.CliError as e:
        check("commands:" in str(e), "unexpected error: %s" % e)
    first = call("data", channel=1, count=1)["items"]
    time.sleep(1.5)
    second = call("data", channel=1, count=1)["items"]
    check(first and second, "no data on channel 1")
    a, b = (struct.unpack("<I", bytes.fromhex(i[0]["hex"]))[0] for i in (first, second))
    check(b > a, "frame counter didn't advance (%d, %d)" % (a, b))
    return {"frames_per_1.5s": b - a}


def round_trip(ctx, size, name):
    data = os.urandom(size)
    local = os.path.join(ctx["out"], "file.bin")
    back = os.path.join(ctx["out"], "back.bin")
    with open(local, "wb") as f:
        f.write(data)
    remote = "%s/%s" % (TEST_DIR, name)
    up = call("put", local=local, remote=remote)
    t0 = time.time()
    call("get", remote=remote, out=back)
    down = time.time() - t0
    with open(back, "rb") as f:
        check(f.read() == data, "%d bytes came back different" % size)
    call("delete", path=remote)
    return up, down


@test("files")
def t_files(ctx):
    """Round trips across chunk and sector boundaries, up to 1 MiB; listing; nothing left behind."""
    sizes = [0, 1, 511, 512, 513, 4095, 4096, 4097, 8191, 8192, 8193, 32768, 100003, 1048576]
    speeds = {}
    for size in sizes:
        up, down = round_trip(ctx, size, "hw_%d.bin" % size)
        if size == 1048576:
            speeds = {"up_kib_s": up["kib_per_s"], "down_kib_s": round(size / 1024 / down, 1)}
    names = [e["name"] for e in call("list", path=TEST_DIR)["entries"]]
    check(not [n for n in names if n.startswith("hw_")], "files left behind: %s" % names)
    return dict(sizes=len(sizes), **speeds)


@test("paths")
def t_paths(ctx):
    """Anything outside sd:/ is refused by the agent itself."""
    bad = ["nand:/", "nand:/title", "fat:/x", "sd:/../x", "sd:/dsidbg/../../x", "/etc/passwd", ""]
    for path in bad:
        for req in ({"cmd": "list", "path": path or "nand:/"}, {"cmd": "get", "remote": path, "out": os.devnull},
                    {"cmd": "delete", "path": path}, {"cmd": "reboot", "path": path or "nand:/x"}):
            try:
                cli.call(req)
                raise Fail("%s %r accepted" % (req["cmd"], path))
            except cli.CliError as e:
                check("sd:/" in str(e) or "not found" in str(e), "%s %r: %s" % (req["cmd"], path, e))
    return {"refused": len(bad) * 4}


@test("profiler")
def t_profiler(ctx):
    """Two seconds of samples with the demo busy; symbolized."""
    call("cmd", text="busy 5")
    try:
        call("prof_start")
        time.sleep(2)
        r = call("prof_stop", elf=DEMO_ELF, top=5)
    finally:
        call("cmd", text="busy 0")
    names = [n for n, _ in r["top"]]
    check(r["samples"] >= 1500, "%d samples" % r["samples"])
    check("main" in names, "main not in the top functions: %s" % names)
    return {"samples": r["samples"], "top": r["top"][:3]}


def crash_test(ctx, command, kind):
    t0 = time.time()
    try:
        call("cmd", text=command, reply_timeout=20)
        raise Fail("%s didn't crash the demo" % command)
    except cli.CliError as e:
        check("the device crashed: %s" % kind in str(e), "unexpected error: %s" % e)
    text = crash_after(t0)["text"]
    check(text.startswith(kind), "report kind: %s" % text.splitlines()[0])
    check("main.c:" in text, "no source line in the report")
    launch_demo()  # from the crash screen
    return {"report": text.splitlines()[1].strip()}


@test("crash_abort")
def t_crash_abort(ctx):
    """Data abort: immediate error, symbolized report, relaunch from the crash screen."""
    return crash_test(ctx, "abort", "data abort")


@test("crash_undef")
def t_crash_undef(ctx):
    """Undefined instruction: same as crash_abort."""
    return crash_test(ctx, "undef", "undefined instruction")


@test("hang")
def t_hang(ctx):
    """Hang watchdog (3 s): report, then relaunch from the hang screen."""
    call("watchdog", seconds=3)
    t0 = time.time()
    check("hanging" in call("cmd", text="hang")["reply"], "hang command")
    text = crash_after(t0, wait=15)["text"]
    check(text.startswith("hang (watchdog)"), "report kind: %s" % text.splitlines()[0])
    check("main.c:" in text, "no source line in the report")
    launch_demo()
    return {"report": text.splitlines()[1].strip()}


@test("reboot")
def t_reboot(ctx):
    """Reboot to Unlaunch's default app (the loader), then back into the demo."""
    gen = status()["generation"]
    call("reboot")
    wait_app(LOADER, newer=gen, timeout=90)
    launch_demo()
    return {}


def churn(ctx, rounds):
    big = os.path.join(ctx["out"], "churn.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(1048576))
    worst = 0.0
    for i in range(rounds):
        upload = threading.Thread(target=lambda: _quiet(call, "put", local=big, remote=TEST_DIR + "/churn.bin"))
        upload.start()
        time.sleep(random.uniform(0.5, 3.0))
        _quiet(call, "shutdown")
        upload.join()
        t0 = time.time()
        time.sleep(0.3)
        _quiet(cli.ensure_daemon)  # restart it (if the tray app beats us to it, ours exits: the port is taken)
        daemon_up()
        wait_app(DEMO, timeout=45)
        worst = max(worst, time.time() - t0)
    names = [e["name"] for e in call("list", path=TEST_DIR)["entries"]]
    check(not [n for n in names if n.endswith(".part")], "partial uploads left: %s" % names)
    if "churn.bin" in names:
        call("delete", path=TEST_DIR + "/churn.bin")
    return {"rounds": rounds, "worst_reconnect_s": round(worst, 1)}


def _quiet(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (cli.CliError, OSError):
        return None


@test("churn", suites=("quick",))
def t_churn(ctx):
    """Daemon restarted 3 times during uploads: the DSi reconnects each time."""
    return churn(ctx, 3)


@test("churn_long", suites=("full",))
def t_churn_long(ctx):
    """Daemon restarted 20 times during uploads."""
    return churn(ctx, 20)


def fps_during(fn):
    """Runs fn() while sampling the app's frame rate each second. -> (result, [fps samples])."""
    samples, done = [], threading.Event()

    def sample():
        while not done.wait(1.0):
            st = _quiet(status)
            if st and st.get("status"):
                samples.append(float(st["status"].get("fps", 0)))
    t = threading.Thread(target=sample)
    t.start()
    try:
        return fn(), samples
    finally:
        done.set()
        t.join()


@test("transfer_fps")
def t_transfer_fps(ctx):
    """The app keeps running during a 1 MiB upload and download (frame rate sampled each second)."""
    data = os.urandom(1048576)
    local, back = os.path.join(ctx["out"], "fps.bin"), os.path.join(ctx["out"], "fps_back.bin")
    with open(local, "wb") as f:
        f.write(data)
    remote = TEST_DIR + "/hw_fps.bin"
    _, up = fps_during(lambda: call("put", local=local, remote=remote))
    _, down = fps_during(lambda: call("get", remote=remote, out=back))
    call("delete", path=remote)
    with open(back, "rb") as f:
        check(f.read() == data, "download differs")
    # The first sample can still cover the frame before the transfer started.
    up_min, down_min = min(up[1:] or up), min(down[1:] or down)
    check(up_min >= 30, "upload: the app dropped to %.1f fps" % up_min)
    check(down_min >= 30, "download: the app dropped to %.1f fps" % down_min)
    return {"upload_min_fps": up_min, "download_min_fps": down_min}


@test("files_large", suites=("full",))
def t_files_large(ctx):
    """16 MiB round trip."""
    up, down = round_trip(ctx, 16 * 1048576, "hw_large.bin")
    return {"up_kib_s": up["kib_per_s"], "down_kib_s": round(16 * 1024 / down, 1)}


@test("upload_stress", suites=("stress",))
def t_upload_stress(ctx):
    """--rounds uploads of 16 MiB in a row (the size that has hung the DSi), each checked by size and CRC."""
    local = os.path.join(ctx["out"], "stress.bin")
    with open(local, "wb") as f:
        f.write(os.urandom(16 * 1048576))
    speeds = []
    for i in range(ctx["args"].rounds):
        t0 = time.time()
        try:
            up = call("put", local=local, remote="%s/hw_stress.bin" % TEST_DIR)
        except cli.CliError as e:
            raise Fail("round %d failed after %.0f s: %s: look at the DSi's screen" % (i + 1, time.time() - t0, e))
        speeds.append(up["kib_per_s"])
        print("    round %d: %.1f KiB/s" % (i + 1, up["kib_per_s"]), flush=True)
    call("delete", path="%s/hw_stress.bin" % TEST_DIR)
    return {"rounds": len(speeds), "min_kib_s": min(speeds), "max_kib_s": max(speeds)}


@test("fuzz", suites=("full",))
def t_fuzz(ctx):
    """Malformed and random messages: the agent answers with errors or ignores them, and never crashes."""
    t0 = time.time()

    def raw(msg_type, payload=b"", wait=2):
        return call("raw", type=msg_type, payload=payload.hex(), wait=wait)

    def ack_status(r):
        acks = [x for x in r["replies"] if x["type"] == proto.ACK]
        return struct.unpack("<i", bytes.fromhex(acks[0]["payload"])[:4])[0] if acks else None

    cases = [
        ("unknown type", 0x0199, b"", -1),
        ("input, odd size", proto.INPUT, b"\x01" * 15, -1),
        ("put data without begin", proto.PUT_DATA, b"x" * 100, None),
        ("put end without begin", proto.PUT_END, b"", -1),
        ("put begin, short", proto.PUT_BEGIN, b"\x01\x02\x03", -1),
        ("get, empty path", proto.GET, b"", -1),
        ("list, 300-char path", proto.LIST, b"sd:/" + b"a" * 296, -1),
        ("delete, directory", proto.DELETE, b"sd:/dsidbg", -1),
        ("watchdog, short", proto.WATCHDOG, b"\x01\x00", -1),
        ("shot, screen 7", proto.SHOT, b"\x07", -1),
        ("reboot, missing file", proto.REBOOT, b"sd:/dsidbg/no-such-file.nds", -1),
        ("app command, 2000 chars", proto.APP_CMD, b"x" * 2000, -1),
        ("app command, binary", proto.APP_CMD, bytes(range(256)), -1),
    ]
    results = []
    for name, msg_type, payload, want in cases:
        got = ack_status(raw(msg_type, payload))
        results.append((name, got))
        check(got == want, "%s: status %s, expected %s" % (name, got, want))
    pong = raw(proto.PING, os.urandom(10000))
    check(any(x["type"] == proto.PONG and len(x["payload"]) == 20000 for x in pong["replies"]), "10 KB ping")
    rng = random.Random(1234)
    # Random payloads only for messages that can't reboot, delete or change the watchdog.
    safe = [proto.PING, proto.INPUT, proto.SHOT, proto.GET, proto.LIST, proto.APP_CMD, 0x0150]
    for _ in range(200):
        raw(rng.choice(safe), bytes(rng.randrange(256) for _ in range(rng.randrange(64))), wait=0.3)
    call("input_clear")
    call("watchdog", seconds=15)
    check(call("ping")["ms"] < 1000, "ping after fuzzing")
    try:
        crash_after(t0, wait=0)
        raise Fail("the agent crashed during fuzzing")
    except cli.CliError:
        pass
    # Last: a message longer than the agent accepts must drop the connection cleanly, then reconnect.
    gen = status()["generation"]
    _quiet(call, "raw", type=proto.PING, payload=(b"x" * (13 * 1024)).hex(), wait=1)
    wait_app(DEMO, newer=gen, timeout=45)
    return {"cases": len(cases), "random": 200}


@test("log_flood", suites=("full",))
def t_log_flood(ctx):
    """The demo logs every frame for 10 s: it keeps its frame rate, and the lines arrive."""
    log = Log()
    call("cmd", text="spam 10")
    fps = []
    for _ in range(11):
        time.sleep(1)
        s = status()["status"]
        fps.append(float(s.get("fps", 0)))
    lines = log.text().count("spam ")
    drops = int(status()["status"].get("log_drops", 0))
    check(min(fps[1:-1]) >= 45, "fps dropped to %.1f" % min(fps[1:-1]))
    check(lines + drops >= 500, "%d lines + %d drops for about 600 frames" % (lines, drops))
    return {"min_fps": min(fps[1:-1]), "lines": lines, "log_drops": drops}


@test("soak", suites=("soak",))
def t_soak(ctx):
    """Stays connected for --soak-minutes: status each second, screenshot and ping each minute, heap stable."""
    minutes = ctx["args"].soak_minutes
    gen0 = status()["generation"]
    heap, end, next_shot, disconnects = [], time.time() + minutes * 60, 0, 0
    while time.time() < end:
        st = status()
        if not st["connected"] or st["generation"] != gen0:
            disconnects += 1
            wait_app(DEMO, timeout=60)
            gen0 = status()["generation"]
            continue
        heap.append(int(st["status"].get("heap_kib", 0)))
        if time.time() >= next_shot:
            call("shot", screen=0, out=os.path.join(ctx["out"], "soak.png"))
            call("ping")
            next_shot = time.time() + 60
        time.sleep(1)
    early = heap[: max(1, len(heap) // 10)]
    late = heap[-max(1, len(heap) // 10):]
    growth = sum(late) / len(late) - sum(early) / len(early)
    check(disconnects == 0, "%d disconnects" % disconnects)
    check(growth < 8, "heap grew %.1f KiB" % growth)
    return {"minutes": minutes, "heap_start_kib": early[0], "heap_end_kib": late[-1], "disconnects": disconnects}


@test("discovery", suites=("discovery",))
def t_discovery(ctx):
    """With an unreachable address on the card, the DSi finds this computer by discovery. Restores the config."""
    backup = os.path.join(ctx["out"], "config.ini.backup")
    call("get", remote="sd:/dsidbg/config.ini", out=backup)
    with open(backup) as f:
        original = f.read()
    stale = "".join(("host=192.0.2.1\n" if l.startswith("host=") else l + "\n") for l in original.splitlines())
    staged = os.path.join(ctx["out"], "config.ini.stale")
    with open(staged, "w") as f:
        f.write(stale)
    print("    (if this test can't restore it: dsidbg setup --sd <card>, or copy %s back)" % backup)
    call("put", local=staged, remote="sd:/dsidbg/config.ini")
    try:
        launch_demo(timeout=120)
        check(status()["info"].get("host_from") == "discovery", "connected without discovery")
    finally:
        call("put", local=backup, remote="sd:/dsidbg/config.ini")
    return {}


# -- runner -------------------------------------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", choices=["quick", "full", "soak", "discovery", "stress"], default="quick")
    p.add_argument("--only", help="comma-separated test names (setup always runs)")
    p.add_argument("--soak-minutes", type=float, default=60)
    p.add_argument("--rounds", type=int, default=10, help="upload_stress: 16 MiB uploads in a row")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if args.list:
        for t in TESTS:
            print("%-12s %-22s %s" % (t["name"], ",".join(t["suites"]), t["doc"]))
        return 0

    chosen = [t for t in TESTS if args.suite in t["suites"] or t["name"] == "setup"]
    if args.only:
        names = set(args.only.split(",")) | {"setup"}
        chosen = [t for t in TESTS if t["name"] in names]
    out = os.path.join(ROOT, "build", "hwtest", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    ctx = {"out": out, "args": args}
    results, stop = [], None
    started = time.time()
    print("dsidbg hardware tests: suite %s, %d tests, report in %s" % (args.suite, len(chosen), out))
    for t in chosen:
        if stop:
            results.append({"name": t["name"], "result": "not run", "detail": stop, "seconds": 0})
            continue
        t0 = time.time()
        try:
            if t["needs_demo"]:
                ensure_demo()
            detail = t["fn"](ctx) or {}
            res = {"name": t["name"], "result": "pass", "detail": detail}
        except Fail as e:
            res = {"name": t["name"], "result": "FAIL", "detail": str(e)}
        except Unreachable as e:
            res = {"name": t["name"], "result": "FAIL", "detail": str(e)}
            stop = "device unreachable after %s" % t["name"]
        except Exception as e:  # a bug in the runner or an unexpected daemon error
            res = {"name": t["name"], "result": "ERROR", "detail": "%s: %s" % (type(e).__name__, e)}
            traceback.print_exc()
        res["seconds"] = round(time.time() - t0, 1)
        results.append(res)
        print("  %-12s %-6s %5.1fs  %s" % (res["name"], res["result"], res["seconds"],
                                          json.dumps(res["detail"]) if isinstance(res["detail"], dict) else res["detail"]))

    st = _quiet(status) or {}
    meta = {"suite": args.suite, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "minutes": round((time.time() - started) / 60, 1), "host_version": DSIDBG_VERSION,
            "device": st.get("info", {}), "mode": ctx.get("mode")}
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump({"meta": meta, "results": results}, f, indent=1)
    passed = sum(r["result"] == "pass" for r in results)
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write("# dsidbg hardware test report\n\n")
        f.write("- Suite: %s, started %s, %s minutes\n" % (meta["suite"], meta["started"], meta["minutes"]))
        f.write("- Host %s; device lib %s, mode %s, IP %s\n" % (
            DSIDBG_VERSION, meta["device"].get("lib"), meta["device"].get("mode"), meta["device"].get("ip")))
        f.write("- %d of %d passed\n\n| Test | Result | Seconds | Details |\n| --- | --- | --- | --- |\n" % (
            passed, len(results)))
        for r in results:
            d = json.dumps(r["detail"]) if isinstance(r["detail"], dict) else str(r["detail"])
            f.write("| %s | %s | %s | %s |\n" % (r["name"], r["result"], r["seconds"], d.replace("|", "\\|")))
    print("%d of %d passed; report: %s" % (passed, len(results), os.path.join(out, "report.md")))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
