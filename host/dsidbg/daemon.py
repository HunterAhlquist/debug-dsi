# SPDX-License-Identifier: MIT
"""The dsidbg daemon: accepts the DSi's connection and serves CLI requests on a local Unix socket.

Files under the home directory (DSIDBG_HOME, default ~/.dsidbg):
  key           shared secret (also in the DSi's sd:/dsidbg/config.ini)
  ctl.sock      control socket for the CLI (in /tmp when this path is too long for a socket)
  daemon.log    the daemon's own messages
  logs/         device logs, one file per connection
  crashes/      crash reports (.json, and .txt when an ELF is known)
  prof/         raw profiler samples
  elves.json    app name -> ELF path, for automatic symbolization
"""
import collections
import json
import os
import queue
import secrets
import socket
import struct
import threading
import time
import zlib

from . import code_id, control_socket, proto
from .png import rgb555_to_png
from .symbols import Symbols, format_crash

REPLY_TIMEOUT = 15.0
# The device sends STATUS about once a second. A console that resets or loses power never closes its TCP
# connection, so a connection that stays silent this long while no command is running is treated as gone.
SILENCE_TIMEOUT = float(os.environ.get("DSIDBG_SILENCE_TIMEOUT", "10"))


class DeviceError(Exception):
    pass


class Device:
    def __init__(self, daemon, sock, addr, generation):
        self.daemon = daemon
        self.sock = sock
        self.addr = addr
        self.generation = generation
        self.app = self.build = ""
        self.info = {}
        self.status = {}
        self.connected_at = time.time()
        self.alive = True
        self.send_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.waiters = {}
        self.waiters_lock = threading.Lock()
        self.seq = 0
        self.log_file = None
        self.last_rx = time.time()

    # -- I/O ---------------------------------------------------------------------------------------------------
    def recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("device closed the connection")
            buf += chunk
        return bytes(buf)

    def recv_msg(self):
        length, msg_type, seq = proto.HEADER.unpack(self.recv_exact(proto.HEADER.size))
        if length > proto.MAX_PAYLOAD:
            raise ConnectionError("message too large")
        return msg_type, seq, self.recv_exact(length)

    def send(self, msg_type, payload=b"", seq=0):
        with self.send_lock:
            self.sock.sendall(proto.frame(msg_type, payload, seq))

    def handshake(self, key):
        self.sock.settimeout(10)
        msg_type, _, p = self.recv_msg()
        if msg_type != proto.HELLO or len(p) < 20:
            raise ConnectionError("expected HELLO")
        version, = struct.unpack_from("<H", p)
        if version != proto.VERSION:
            raise ConnectionError("protocol version %d, expected %d" % (version, proto.VERSION))
        nonce_d = p[4:20]
        names = p[20:].split(b"\0")
        self.app = names[0].decode("utf-8", "replace") if names else ""
        self.build = names[1].decode("utf-8", "replace") if len(names) > 1 else ""
        nonce_h = secrets.token_bytes(16)
        self.send(proto.CHALLENGE, nonce_h + proto.host_mac(key, nonce_d, nonce_h))
        try:
            msg_type, _, p = self.recv_msg()
        except ConnectionError:
            # The device checks the host first and hangs up if the host's proof doesn't match its key.
            raise ConnectionError("device %r rejected this host: the keys differ" % self.app)
        if msg_type != proto.PROOF or not secrets.compare_digest(p, proto.dev_mac(key, nonce_h, nonce_d)):
            raise ConnectionError("device %r failed authentication: the keys differ" % self.app)
        self.sock.settimeout(None)

    # -- requests ----------------------------------------------------------------------------------------------
    def new_seq(self):
        with self.waiters_lock:
            self.seq = self.seq % 0xFFFF + 1
            q = queue.Queue()
            self.waiters[self.seq] = q
            return self.seq, q

    def done_seq(self, seq):
        with self.waiters_lock:
            self.waiters.pop(seq, None)

    def request(self, msg_type, payload=b"", timeout=REPLY_TIMEOUT, until=(proto.ACK, proto.PONG)):
        """Sends a command and collects replies with its seq until one of `until` arrives. -> list of (type, p)."""
        seq, q = self.new_seq()
        try:
            self.send(msg_type, payload, seq)
            replies = []
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise DeviceError("no reply from the device within %.0f s" % timeout)
                try:
                    item = q.get(timeout=min(left, 0.5))
                except queue.Empty:
                    if not self.alive:
                        raise DeviceError("device disconnected")
                    continue
                if item is None:
                    raise DeviceError("device disconnected")
                if item[0] == "crash":
                    c = item[1]
                    raise DeviceError("the device crashed: %s at pc %08x (report: `dsidbg crash`, or the last_crash tool)"
                                      % (c["kind_name"], c["regs"][15]))
                replies.append(item)
                if item[0] in until:
                    return replies
                deadline = time.time() + timeout  # streamed replies (file data) extend the deadline
        finally:
            self.done_seq(seq)

    def ack(self, msg_type, payload=b"", timeout=REPLY_TIMEOUT):
        """Request whose answer is an ACK. Raises on a negative status; returns the reply text."""
        replies = self.request(msg_type, payload, timeout)
        t, p = replies[-1]
        if t == proto.PONG:
            return p
        status, = struct.unpack_from("<i", p)
        text = p[4:].decode("utf-8", "replace")
        if status < 0:
            raise DeviceError(text or "device error %d" % status)
        return text

    # -- reader ------------------------------------------------------------------------------------------------
    def reader(self):
        d = self.daemon
        try:
            while True:
                msg_type, seq, p = self.recv_msg()
                self.last_rx = time.time()
                if seq:
                    with self.waiters_lock:
                        q = self.waiters.get(seq)
                    if q is not None:
                        q.put((msg_type, p))
                        continue
                if msg_type == proto.HELLO:
                    # A device that restarted can reach us through a NAT entry of its old connection.
                    raise ConnectionError("new HELLO on an open connection (the device restarted)")
                if msg_type == proto.LOG:
                    d.add_log(self, p.decode("utf-8", "replace"))
                elif msg_type == proto.INFO:
                    self.info = proto.parse_kv(p)
                    d.note("info from %s: %s" % (self.app, self.info))
                    with d.lock:
                        d.lock.notify_all()
                elif msg_type == proto.STATUS:
                    self.status = proto.parse_kv(p)
                    self.status["_time"] = time.time()
                elif msg_type == proto.PROF:
                    d.add_prof(p)
                elif msg_type == proto.CRASH:
                    d.add_crash(self, p)
                elif msg_type == proto.DATA:
                    channel, = struct.unpack_from("<H", p)
                    d.add_data(channel, p[4:])
        except (OSError, ConnectionError, struct.error) as e:
            d.note("device %s disconnected: %s" % (self.app, e))
        finally:
            self.close()

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the reader thread; close() alone may not
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        with self.waiters_lock:
            for q in self.waiters.values():
                q.put(None)
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.daemon.device_gone(self)


class Daemon:
    def __init__(self, home, port=proto.DEFAULT_PORT, bind="0.0.0.0"):
        self.home = home
        self.port = port
        self.bind = bind
        for sub in ("logs", "crashes", "prof", "shots"):
            os.makedirs(os.path.join(home, sub), exist_ok=True)
        self.key = load_key(home).encode()
        self.device = None
        self.generation = 0
        self.lock = threading.Condition()
        self.logs = collections.deque(maxlen=5000)  # (index, time, text)
        self.log_index = 0
        self.crashes = []
        self.data = collections.defaultdict(lambda: collections.deque(maxlen=256))
        self.prof_samples = []
        self.prof_hz = 0
        self.stopping = False
        self.daemon_log = open(os.path.join(home, "daemon.log"), "a", buffering=1)
        self.code_id = code_id()

    def note(self, text):
        self.daemon_log.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))

    # -- device events -----------------------------------------------------------------------------------------
    def add_log(self, dev, text):
        if dev.log_file is None:
            name = "%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(dev.connected_at)), dev.app)
            dev.log_file = open(os.path.join(self.home, "logs", name), "a", buffering=1)
        dev.log_file.write(text)
        with self.lock:
            self.log_index += 1
            self.logs.append((self.log_index, time.time(), text))
            self.lock.notify_all()

    def add_prof(self, p):
        hz, = struct.unpack_from("<I", p)
        n = (len(p) - 4) // 4
        with self.lock:
            self.prof_hz = hz
            self.prof_samples.extend(struct.unpack_from("<%dI" % n, p, 4))

    def add_data(self, channel, payload):
        with self.lock:
            self.data[channel].append((time.time(), payload))
            self.lock.notify_all()

    def add_crash(self, dev, p):
        crash = proto.decode_crash(p)
        crash.update(app=dev.app, build=dev.build, time=time.time())
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(self.home, "crashes", "%s-%s" % (stamp, dev.app))
        with open(base + ".json", "w") as f:
            json.dump(crash, f, indent=1)
        crash["file"] = base + ".json"
        elf = self.elf_for(dev.app)
        if elf:
            try:
                with open(base + ".txt", "w") as f:
                    f.write(format_crash(crash, Symbols(elf)) + "\n")
            except (OSError, ValueError) as e:
                self.note("can't symbolize crash: %s" % e)
        self.note("crash from %s: %s pc=%08x" % (dev.app, crash["kind_name"], crash["regs"][15]))
        with self.lock:
            self.crashes.append(crash)
            self.lock.notify_all()
        # Commands waiting for a reply won't get one: fail them now instead of at their timeout.
        with dev.waiters_lock:
            for q in dev.waiters.values():
                q.put(("crash", crash))
        self.add_log(dev, "*** %s: %s (pc %08x)\n" % (crash["kind_name"], crash["msg"], crash["regs"][15]))

    def elf_for(self, app):
        try:
            with open(os.path.join(self.home, "elves.json")) as f:
                return json.load(f).get(app)
        except (OSError, ValueError):
            return None

    def device_gone(self, dev):
        with self.lock:
            if self.device is dev:
                self.device = None
            self.lock.notify_all()

    # -- device listener ---------------------------------------------------------------------------------------
    def listen_devices(self):
        """Binds the device port. Raises OSError if another daemon already has it."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(4)
        self.note("listening for devices on %s:%d" % (self.bind, self.port))
        return srv

    def serve_devices(self, srv):
        while not self.stopping:
            sock, addr = srv.accept()
            threading.Thread(target=self.accept_device, args=(sock, addr), daemon=True).start()

    def accept_device(self, sock, addr):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            self.generation += 1
            gen = self.generation
        dev = Device(self, sock, addr, gen)
        try:
            dev.handshake(self.key)
        except (OSError, ConnectionError, struct.error) as e:
            self.note("rejected %s: %s" % (addr[0], e))
            sock.close()
            return
        with self.lock:
            old, self.device = self.device, dev
            self.lock.notify_all()
        if old:
            old.close()
            with self.lock:
                self.device = dev
        self.note("device %s (%s) connected from %s" % (dev.app, dev.build, addr[0]))
        dev.reader()

    # -- control socket ----------------------------------------------------------------------------------------
    def serve_control(self):
        path = control_socket(self.home)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(8)
        while not self.stopping:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn,), daemon=True).start()

    def handle_client(self, conn):
        f = conn.makefile("rwb")

        def reply(obj):
            f.write((json.dumps(obj) + "\n").encode())
            f.flush()

        try:
            req = json.loads(f.readline() or b"{}")
            handler = getattr(self, "cmd_" + req.get("cmd", ""), None)
            if handler is None:
                reply({"ok": False, "error": "unknown command %r" % req.get("cmd")})
                return
            result = handler(req, reply)
            if result is not None:
                result.setdefault("ok", True)
                reply(result)
        except DeviceError as e:
            reply({"ok": False, "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # report anything else to the CLI rather than killing the thread silently
            reply({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        finally:
            try:
                f.close()
                conn.close()
            except OSError:
                pass

    def need_device(self, req=None, timeout=None):
        timeout = (req or {}).get("timeout", 0) if timeout is None else timeout
        deadline = time.time() + timeout
        with self.lock:
            while not (self.device and self.device.alive):
                left = deadline - time.time()
                if left <= 0:
                    raise DeviceError("no device connected")
                self.lock.wait(left)
            return self.device

    def check_silence(self):
        while not self.stopping:
            time.sleep(1)
            dev = self.device
            if dev and dev.alive and not dev.waiters and time.time() - dev.last_rx > SILENCE_TIMEOUT:
                self.note("device %s silent for %.0f s, dropping the connection" % (dev.app, SILENCE_TIMEOUT))
                dev.close()

    def serve_discovery(self):
        """Answers DSi discovery broadcasts (UDP on the daemon's port) so a DSi finds this computer after its address
        changes. The answer is signed with the shared key."""
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp.bind((self.bind, self.port))
        except OSError as e:
            self.note("discovery unavailable: %s" % e)
            return
        last_note = {}
        while not self.stopping:
            try:
                data, addr = udp.recvfrom(64)
                reply = proto.discover_reply(self.key, data, self.port)
                if reply is None:
                    continue
                udp.sendto(reply, addr)
                if time.time() - last_note.get(addr[0], 0) > 60:  # a searching DSi asks every second
                    self.note("discovery request from %s" % addr[0])
                    last_note[addr[0]] = time.time()
            except OSError:
                time.sleep(0.5)

    def run(self):
        # Take the device port first: if another daemon has it (two started at once), leave without touching the
        # control socket, which the CLI would otherwise reach while the DSi talks to the other daemon.
        try:
            srv = self.listen_devices()
        except OSError as e:
            self.note("not starting: port %d is taken (%s); another daemon is probably running" % (self.port, e))
            raise SystemExit("dsidbg: port %d is taken; another daemon is probably running" % self.port)
        threading.Thread(target=self.serve_devices, args=(srv,), daemon=True).start()
        threading.Thread(target=self.serve_discovery, daemon=True).start()
        threading.Thread(target=self.check_silence, daemon=True).start()
        self.serve_control()

    # -- commands ----------------------------------------------------------------------------------------------
    def cmd_status(self, req, reply):
        dev = self.device
        out = {"port": self.port, "bind": self.bind, "home": self.home, "generation": self.generation,
               "code_id": self.code_id, "connected": bool(dev and dev.alive)}
        if dev and dev.alive:
            out.update(app=dev.app, build=dev.build, addr=dev.addr[0], connected_at=dev.connected_at,
                       info=dev.info, status=dev.status)
        return out

    def cmd_wait(self, req, reply):
        """Waits for a device (optionally a given app, optionally newer than a connection generation)."""
        app, newer, timeout = req.get("app"), req.get("newer_than", 0), req.get("timeout", 60)
        deadline = time.time() + timeout
        with self.lock:
            while True:
                dev = self.device
                # The device sends INFO right after the handshake; wait for it so callers see a complete status.
                if dev and dev.alive and dev.info and dev.generation > newer and (not app or dev.app == app):
                    return {"app": dev.app, "build": dev.build, "generation": dev.generation}
                left = deadline - time.time()
                if left <= 0:
                    raise DeviceError("timed out waiting for %s" % (app or "a device"))
                self.lock.wait(left)

    def cmd_ping(self, req, reply):
        dev = self.need_device(req)
        t0 = time.time()
        with dev.cmd_lock:
            dev.ack(proto.PING, b"ping")
        return {"ms": round((time.time() - t0) * 1000, 1)}

    def cmd_input(self, req, reply):
        dev = self.need_device(req)
        payload = b"".join(proto.input_record(r["delay"], r["frames"], r["keys"], r.get("flags", 0),
                                              r.get("x", 0), r.get("y", 0)) for r in req["records"])
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.INPUT, payload)}

    def cmd_input_clear(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.INPUT_CLEAR)}

    def cmd_shot(self, req, reply):
        dev = self.need_device(req)
        screen = req.get("screen", 0)
        with dev.cmd_lock:
            replies = dev.request(proto.SHOT, bytes([screen]), until=(proto.ACK, proto.SHOT_DATA))
        t, p = replies[-1]
        if t == proto.ACK:
            raise DeviceError(p[4:].decode("utf-8", "replace"))
        w, h = struct.unpack_from("<HH", p, 2)
        out = req.get("out") or os.path.join(self.home, "shots", "%s-%s.png" % (
            time.strftime("%Y%m%d-%H%M%S"), "top" if screen == 0 else "bottom"))
        with open(out, "wb") as f:
            f.write(rgb555_to_png(p[8:], w, h))
        return {"file": out, "width": w, "height": h}

    def cmd_reboot(self, req, reply):
        dev = self.need_device(req)
        path = req.get("path") or ""
        with dev.cmd_lock:
            text = dev.ack(proto.REBOOT, path.encode())
        return {"reply": text, "generation": dev.generation}

    def cmd_put(self, req, reply):
        dev = self.need_device(req)
        with open(req["local"], "rb") as f:
            data = f.read()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        t0 = time.time()
        with dev.cmd_lock:
            dev.ack(proto.PUT_BEGIN, struct.pack("<II", len(data), crc) + req["remote"].encode())
            for off in range(0, len(data), proto.PUT_CHUNK):
                dev.send(proto.PUT_DATA, data[off:off + proto.PUT_CHUNK])
            text = dev.ack(proto.PUT_END, timeout=60)
        dt = time.time() - t0
        return {"reply": text, "bytes": len(data), "seconds": round(dt, 2),
                "kib_per_s": round(len(data) / 1024 / dt, 1) if dt else 0}

    def cmd_get(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            replies = dev.request(proto.GET, req["remote"].encode(), timeout=30, until=(proto.ACK, proto.FILE_END))
        t, p = replies[-1]
        if t == proto.ACK:
            raise DeviceError(p[4:].decode("utf-8", "replace"))
        status, size, crc = struct.unpack_from("<iII", p)
        data = b"".join(rp for rt, rp in replies if rt == proto.FILE_DATA)
        if status < 0 or len(data) != size or zlib.crc32(data) & 0xFFFFFFFF != crc:
            raise DeviceError("transfer failed (status %d, %d/%d bytes)" % (status, len(data), size))
        with open(req["out"], "wb") as f:
            f.write(data)
        return {"file": req["out"], "bytes": size}

    def cmd_list(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            text = dev.ack(proto.LIST, req.get("path", "sd:/").encode())
        entries = []
        for line in text.splitlines():
            kind, size, name = line.split("\t", 2)
            entries.append({"dir": kind == "D", "size": int(size), "name": name})
        return {"entries": entries}

    def cmd_delete(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.DELETE, req["path"].encode())}

    def cmd_cmd(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.APP_CMD, req["text"].encode(), timeout=req.get("reply_timeout", 30))}

    def cmd_watchdog(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.WATCHDOG, struct.pack("<I", req["seconds"]))}

    def cmd_prof_start(self, req, reply):
        dev = self.need_device(req)
        with self.lock:
            self.prof_samples = []
        with dev.cmd_lock:
            return {"reply": dev.ack(proto.PROF_START)}

    def cmd_prof_stop(self, req, reply):
        dev = self.need_device(req)
        with dev.cmd_lock:
            dev.ack(proto.PROF_STOP, timeout=30)
        with self.lock:
            samples, self.prof_samples = self.prof_samples, []
        path = os.path.join(self.home, "prof", "%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), dev.app))
        with open(path, "w") as f:
            f.write("\n".join("%08x" % s for s in samples) + "\n")
        out = {"samples": len(samples), "hz": self.prof_hz, "file": path}
        elf = req.get("elf") or self.elf_for(dev.app)
        counts = collections.Counter()
        if elf:
            syms = Symbols(elf)
            for s in samples:
                hit = syms.lookup(s & ~1)
                counts[hit[0] if hit else "?"] += 1
        else:
            for s in samples:
                counts["%08x" % (s & ~0xFF)] += 1  # 256-byte buckets without symbols
        out["top"] = [[name, n] for name, n in counts.most_common(req.get("top", 30))]
        return out

    def cmd_log(self, req, reply):
        """Streams log text with its index. `since` (an index) returns only newer entries; otherwise the last `lines`
        entries. With `follow`, keeps streaming new ones. The final reply carries `last`, the newest index."""
        with self.lock:
            entries = list(self.logs)
            last = self.log_index
        if req.get("since") is not None:
            backlog = [e for e in entries if e[0] > req["since"]][-req.get("lines", 1000):]
        else:
            backlog = entries[-req.get("lines", 100):] if req.get("lines", 100) else []
        for idx, _, text in backlog:
            reply({"text": text, "index": idx})
        if not req.get("follow"):
            return {"done": True, "last": last}
        deadline = time.time() + req["follow_seconds"] if req.get("follow_seconds") else None
        while not deadline or time.time() < deadline:
            with self.lock:
                self.lock.wait(0.5)
                new = [e for e in self.logs if e[0] > last]
            for idx, _, text in new:
                reply({"text": text, "index": idx})
                last = idx
        return {"done": True, "last": last}

    def cmd_crash(self, req, reply):
        """The last crash; with `after` (a time), only crashes reported since then. `wait` waits for one."""
        after = req.get("after", 0)
        deadline = time.time() + req.get("wait", 0)
        with self.lock:
            while True:
                crashes = [c for c in self.crashes if c["time"] >= after]
                if crashes or time.time() >= deadline:
                    break
                self.lock.wait(0.5)
        if not crashes:
            raise DeviceError("no %scrash reported (saved reports: %s)" % (
                "new " if after else "", os.path.join(self.home, "crashes")))
        crash = crashes[req.get("index", -1)]
        elf = req.get("elf") or self.elf_for(crash["app"])
        text = format_crash(crash, Symbols(elf) if elf else None)
        return {"crash": crash, "text": text, "count": len(crashes)}

    def cmd_data(self, req, reply):
        with self.lock:
            items = list(self.data.get(req["channel"], []))[-req.get("count", 1):]
        return {"items": [{"time": t, "hex": p.hex()} for t, p in items]}

    def cmd_elf(self, req, reply):
        path = os.path.join(self.home, "elves.json")
        try:
            with open(path) as f:
                table = json.load(f)
        except (OSError, ValueError):
            table = {}
        if req.get("path"):
            Symbols(req["path"])  # validate
            table[req["app"]] = os.path.abspath(req["path"])
            with open(path, "w") as f:
                json.dump(table, f, indent=1)
        return {"elves": table}

    def cmd_shutdown(self, req, reply):
        reply({"ok": True})
        self.stopping = True
        os._exit(0)


def load_key(home):
    path = os.path.join(home, "key")
    if not os.path.exists(path):
        os.makedirs(home, exist_ok=True)
        with open(path, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()
