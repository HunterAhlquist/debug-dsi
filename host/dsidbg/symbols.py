# SPDX-License-Identifier: MIT
"""Symbolization of DSi addresses: an ELF32 symbol table reader (standard library only), plus file:line through
arm-none-eabi-addr2line when it is installed."""
import bisect
import glob
import os
import shutil
import struct
import subprocess


# Where ARM toolchains usually live. Apps started from Finder, the Start menu or at login don't get the shell's PATH,
# so the daemon they start wouldn't find addr2line (crash reports then lack file:line).
TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "/opt/wonderful/toolchain/gcc-arm-none-eabi/bin",
             "/opt/devkitpro/devkitARM/bin", "/Applications/ArmGNUToolchain/*/arm-none-eabi/bin",
             "C:/Program Files*/Arm GNU Toolchain*/*/bin"]


def find_tool(name):
    """`name` from PATH, $DEVKITARM/bin, or the usual toolchain folders (newest version first); None if absent."""
    found = shutil.which(name)
    if found:
        return found
    dirs = [os.path.join(os.environ["DEVKITARM"], "bin")] if os.environ.get("DEVKITARM") else []
    for pattern in TOOL_DIRS:
        dirs += sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
    return shutil.which(name, path=os.pathsep.join(dirs)) if dirs else None


class Symbols:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            raise ValueError("%s is not a little-endian ELF32 file" % path)
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum = struct.unpack_from("<HH", data, 0x2E)
        sections = [struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)]
        funcs = {}
        for sh in sections:
            if sh[1] != 2:  # SHT_SYMTAB
                continue
            strtab = sections[sh[6]]
            for i in range(sh[5] // 16):
                name_off, value, size, info, _, shndx = struct.unpack_from("<IIIBBH", data, sh[4] + i * 16)
                kind = info & 0xF
                if kind not in (1, 2) or shndx == 0 or not size:  # objects and functions with a size
                    continue
                end = data.index(b"\0", strtab[4] + name_off)
                name = data[strtab[4] + name_off:end].decode("utf-8", "replace")
                if name.startswith("$"):
                    continue
                addr = value & ~1 if kind == 2 else value
                funcs.setdefault(addr, (name, size, kind == 2))
        self.items = sorted((a, n, s, f) for a, (n, s, f) in funcs.items())
        self.addrs = [i[0] for i in self.items]

    def lookup(self, addr):
        """-> (name, offset, is_function) or None."""
        i = bisect.bisect_right(self.addrs, addr) - 1
        if i < 0:
            return None
        start, name, size, func = self.items[i]
        if addr >= start + size:
            return None
        return name, addr - start, func

    def describe(self, addr):
        hit = self.lookup(addr)
        if not hit:
            return "?"
        name, off, _ = hit
        return "%s+0x%x" % (name, off) if off else name

    def is_code(self, addr):
        hit = self.lookup(addr)
        return bool(hit and hit[2])

    def lines(self, addrs):
        """addr -> 'file:line' via addr2line, if available."""
        tool = find_tool("arm-none-eabi-addr2line")
        if not tool or not addrs:
            return {}
        try:
            out = subprocess.run([tool, "-e", self.path] + ["0x%x" % a for a in addrs], capture_output=True,
                                 text=True, timeout=30).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return {}
        return {a: l.rsplit("/", 1)[-1] for a, l in zip(addrs, out) if not l.startswith("??")}


def format_crash(crash, syms=None):
    regs = crash["regs"]
    lines = ["%s%s: %s" % ("[saved on SD] " if crash.get("from_file") else "", crash["kind_name"], crash["msg"])]
    if crash.get("main_blocked"):
        lines.append("every thread was waiting: the registers are where the main thread waits (pc = its resume "
                     "address), not the interrupted code (addr)")
    pc = regs[15]
    where = ""
    if syms:
        where = "  " + syms.describe(pc)
        loc = syms.lines([pc, regs[14] & ~1])
        if pc in loc:
            where += "  (%s)" % loc[pc]
    lines.append("pc   %08x%s" % (pc, where))
    lines.append("addr %08x   cpsr %08x (%s, mode %02x)   frame %d" % (
        crash["fault_addr"], crash["cpsr"], "thumb" if crash["thumb"] else "arm", crash["cpsr"] & 0x1F, crash["frame"]))
    if syms:
        lines.append("lr   %08x  %s" % (regs[14], syms.describe(regs[14] & ~1)))
    for i in range(0, 16, 4):
        lines.append("  ".join("r%-2d %08x" % (j, regs[j]) for j in range(i, i + 4)))
    stack = bytes.fromhex(crash["stack"])
    if syms and stack:
        # Heuristic backtrace: words on the stack that point into functions are likely return addresses.
        lines.append("stack (%d bytes at %08x), code addresses:" % (len(stack), crash["stack_base"]))
        hits = []
        for off in range(0, len(stack) - 3, 4):
            w, = struct.unpack_from("<I", stack, off)
            if syms.is_code(w & ~1):
                hits.append((off, w))
        loc = syms.lines([w & ~1 for _, w in hits[:32]])
        for off, w in hits[:32]:
            extra = loc.get(w & ~1, "")
            lines.append("  sp+%-4x %08x  %s%s" % (off, w, syms.describe(w & ~1), "  (%s)" % extra if extra else ""))
    elif stack:
        lines.append("stack: %d bytes at %08x (pass --elf to symbolize)" % (len(stack), crash["stack_base"]))
    return "\n".join(lines)
