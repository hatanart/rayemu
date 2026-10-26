"""ROM sets: which EPROM files make up a game, and how they are found.

The main board (PCB 5003) holds four EPROMs of 64 KB each, one after the
other in the address space. Every chip has its own label, for example

    6458-7.7 J
    5003/IC 38
    27.09-94 (14E2)

i.e. part number and version, board/socket, date, and in brackets the
16-bit sum of the chip's bytes. Each chip is one file in roms/, named after
its label and socket: 6458-7.7_ic38.bin.

A ROM set is a text file roms/<set name>.ray that lists the chips:

    title: POKERI (Finnish), ...
    00000  IC30  6457-7.7_ic30.bin  A8EF
    ...                                      (address socket file sum|-)

The game's battery-backed RAM is kept next to it as roms/<set name>.nvram.
A single 256 KB file (all four chips joined) also works: --rom FILE.
"""

import os

CHIP_SIZE = 0x10000
ROM_SIZE = 4 * CHIP_SIZE
DEFAULT = "pokeri-8460-1.1"


class RomSet:
    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.dir = os.path.dirname(path)
        self.title = self.name
        self.chips = []                       # (address, socket, file, sum or None)
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.lower().startswith("title:"):
                    self.title = line[6:].strip()
                    continue
                parts = line.split()
                try:
                    addr = int(parts[0], 16)
                    sock, fn = parts[1], parts[2]
                    want = None if len(parts) < 4 or parts[3] == "-" else int(parts[3], 16)
                except (IndexError, ValueError):
                    raise SystemExit("%s line %d: expected 'address socket file sum', got %r"
                                     % (path, n, line))
                self.chips.append((addr, sock, fn, want))
        if not self.chips:
            raise SystemExit("%s: no chips listed" % path)

    @property
    def nvram(self):
        return os.path.join(self.dir, self.name + ".nvram")

    def check(self):
        """[(address, socket, file, label sum, actual sum or None if missing)]"""
        out = []
        for addr, sock, fn, want in self.chips:
            try:
                with open(os.path.join(self.dir, fn), "rb") as f:
                    got = chip_sum(f.read())
            except OSError:
                got = None
            out.append((addr, sock, fn, want, got))
        return out

    def load(self):
        data = bytearray(b"\xff" * ROM_SIZE)
        for addr, sock, fn, want in self.chips:
            p = os.path.join(self.dir, fn)
            if not os.path.isfile(p):
                raise SystemExit("ROM set %s: missing %s (%s).\nThe ROMs are not included: put "
                                 "the chip files listed in %s into %s (--list-roms checks them)."
                                 % (self.name, fn, sock, self.path, self.dir))
            with open(p, "rb") as f:
                chip = f.read()
            if addr + len(chip) > ROM_SIZE:
                raise SystemExit("%s: %s does not fit at %05X" % (self.path, fn, addr))
            if want is not None and chip_sum(chip) != want:
                print("warning: %s sum %04X, label says %04X" % (fn, chip_sum(chip), want))
            data[addr:addr + len(chip)] = chip
        return bytes(data)


def chip_sum(data):
    return sum(data) & 0xFFFF


def sets(rom_dir):
    if not os.path.isdir(rom_dir):
        return []
    return [RomSet(os.path.join(rom_dir, n)) for n in sorted(os.listdir(rom_dir))
            if n.lower().endswith(".ray")]


def resolve(arg, base):
    """--rom (a set name, a .ray file or a 256 KB file) ->
    (title, rom bytes, nvram path, description)."""
    rom_dir = os.path.join(base, "roms")
    if arg is None:
        p = os.path.join(rom_dir, DEFAULT + ".ray")
        found = [p] if os.path.isfile(p) else [s.path for s in sets(rom_dir)]
        if not found:
            raise SystemExit("No ROM set (.ray file) in %s" % rom_dir)
        arg = found[0]
    for p in (os.path.join(rom_dir, arg + ".ray"), arg, os.path.join(rom_dir, arg)):
        if not os.path.isfile(p):
            continue
        if p.lower().endswith(".ray"):
            s = RomSet(p)
            return s.name, s.load(), s.nvram, s.title
        with open(p, "rb") as f:
            data = f.read()
        if len(data) != ROM_SIZE:
            raise SystemExit("%s: expected a 256 KB image, got %d bytes" % (p, len(data)))
        desc = "single file, chip sums " + " ".join(
            "%04X" % chip_sum(data[i * CHIP_SIZE:(i + 1) * CHIP_SIZE]) for i in range(4))
        return (os.path.splitext(os.path.basename(p))[0], data,
                os.path.splitext(p)[0] + ".nvram", desc)
    raise SystemExit("ROM %r not found. ROM sets: %s" % (
        arg, ", ".join(s.name for s in sets(rom_dir)) or "none"))


def list_roms(base):
    rom_dir = os.path.join(base, "roms")
    found = sets(rom_dir)
    if not found:
        print("no ROM sets (.ray files) in", rom_dir)
    for s in found:
        print("%s  %s" % (s.name, s.title))
        for addr, sock, fn, want, got in s.check():
            if got is None:
                state = "missing"
            elif want is None:
                state = "sum %04X (not on label)" % got
            else:
                state = "ok" if got == want else "sum %04X, label says %04X" % (got, want)
            print("    %05X %-4s %-20s %-4s  %s" % (addr, sock, fn,
                                                     "-" if want is None else "%04X" % want, state))
