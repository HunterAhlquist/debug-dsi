# SPDX-License-Identifier: MIT
"""Wire protocol shared with the DSi agent. Must match src/internal.h and docs/protocol.md."""
import hashlib
import hmac
import struct

VERSION = 1
HEADER = struct.Struct("<IHH")  # payload length, type, seq
MAX_PAYLOAD = 16 * 1024 * 1024
DEFAULT_PORT = 7710
PUT_CHUNK = 16 * 1024  # the device accepts messages up to 20 KiB

HELLO, CHALLENGE, PROOF = 0x0001, 0x0002, 0x0003

PING = 0x0100
INPUT = 0x0101
INPUT_CLEAR = 0x0102
SHOT = 0x0103
REBOOT = 0x0104
PUT_BEGIN = 0x0105
PUT_DATA = 0x0106
PUT_END = 0x0107
GET = 0x0108
LIST = 0x0109
DELETE = 0x010A
PROF_START = 0x010B
PROF_STOP = 0x010C
APP_CMD = 0x010D
WATCHDOG = 0x010E

PONG = 0x0200
ACK = 0x0201
LOG = 0x0202
INFO = 0x0203
STATUS = 0x0204
SHOT_DATA = 0x0205
FILE_DATA = 0x0206
FILE_END = 0x0207
PROF = 0x0208
CRASH = 0x0209
DATA = 0x020A

INPUT_REPLACE = 1

# libnds KEY_* bits
KEYS = {
    "A": 1 << 0, "B": 1 << 1, "SELECT": 1 << 2, "START": 1 << 3,
    "RIGHT": 1 << 4, "LEFT": 1 << 5, "UP": 1 << 6, "DOWN": 1 << 7,
    "R": 1 << 8, "L": 1 << 9, "X": 1 << 10, "Y": 1 << 11,
    "TOUCH": 1 << 12, "LID": 1 << 13,
}

CRASH_KINDS = {1: "data abort", 2: "undefined instruction", 3: "hang (watchdog)", 4: "panic"}
CRASH_STRUCT = struct.Struct("<I16I6I36s")
CRASH_FROM_FILE = 1


def frame(msg_type, payload=b"", seq=0):
    return HEADER.pack(len(payload), msg_type, seq) + payload


def host_mac(key, nonce_d, nonce_h):
    return hmac.new(key, b"dsidbg-host" + nonce_d + nonce_h, hashlib.sha1).digest()


def dev_mac(key, nonce_h, nonce_d):
    return hmac.new(key, b"dsidbg-dev" + nonce_h + nonce_d, hashlib.sha1).digest()


def parse_keys(text):
    """'A+B' or 'a,start' -> bitmask."""
    mask = 0
    for name in text.replace(",", "+").split("+"):
        name = name.strip().upper()
        if not name:
            continue
        if name not in KEYS:
            raise ValueError("unknown button %r (known: %s)" % (name, " ".join(KEYS)))
        mask |= KEYS[name]
    return mask


def key_names(mask):
    return "+".join(n for n, b in KEYS.items() if mask & b) or "none"


def parse_press(spec, default_frames):
    """'KEYS[@DELAY][+FRAMES]' -> (delay, frames, keys). '+' inside KEYS joins buttons; the last '+N' with a number
    is the duration."""
    delay = 0
    frames = default_frames
    keys = spec
    if "@" in spec:
        keys, rest = spec.split("@", 1)
        if "+" in rest:
            d, f = rest.split("+", 1)
            delay, frames = int(d), int(f)
        else:
            delay = int(rest)
    else:
        parts = spec.rsplit("+", 1)
        if len(parts) == 2 and parts[1].isdigit():
            keys, frames = parts[0], int(parts[1])
    return delay, frames, parse_keys(keys)


def input_record(delay, frames, keys, flags=0, x=0, y=0):
    return struct.pack("<IHHIhh", delay, frames, flags, keys, x, y)


def parse_kv(payload):
    out = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def decode_crash(payload):
    fields = CRASH_STRUCT.unpack_from(payload)
    kind, regs = fields[0], list(fields[1:17])
    cpsr, fault, frame_no, stack_base, stack_len, flags, msg = fields[17:]
    stack = payload[CRASH_STRUCT.size:CRASH_STRUCT.size + stack_len]
    return {
        "kind": kind,
        "kind_name": CRASH_KINDS.get(kind, "unknown"),
        "regs": regs,
        "cpsr": cpsr,
        "thumb": bool(cpsr & 0x20),
        "fault_addr": fault,
        "frame": frame_no,
        "stack_base": stack_base,
        "stack": stack.hex(),
        "from_file": bool(flags & CRASH_FROM_FILE),
        "msg": msg.split(b"\0", 1)[0].decode("utf-8", "replace"),
    }
