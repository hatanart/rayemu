"""Hitachi HD63484 ACRTC (Advanced CRT Controller).

Ported from the 2010 rayemux C++ code (itself based on MAME / Takeda Toshiya),
with a cleaner status register / interrupt model:

  status bits: 0 WFE write FIFO empty, 1 WFR write FIFO ready,
               2 RFR read FIFO ready,  3 RFF read FIFO full,
               5 CED command end,      7 CER command error
  CCR low byte holds the interrupt enables for the same bits, so
  irq = status & (CCR & 0xff).

Commands execute instantly when their last parameter word arrives.
"""

import math

import numpy as np
from array import array

INSTR_NAME = [
    "undef", "ORG", "WPR", "RPR", "undef", "undef", "WPTN", "RPTN",
    "undef", "DRD", "DWT", "DMOD", "undef", "undef", "undef", "undef",
    "undef", "RD", "WT", "MOD", "undef", "undef", "CLR", "SCLR",
    "CPY", "CPY", "CPY", "CPY", "SCPY", "SCPY", "SCPY", "SCPY",
    "AMOVE", "RMOVE", "ALINE", "RLINE", "ARCT", "RRCT", "APLL", "RPLL",
    "APLG", "RPLG", "CRCL", "ELPS", "AARC", "RARC", "AEARC", "REARC",
    "AFRCT", "RFRCT", "PAINT", "DOT", "PTN", "PTN", "PTN", "PTN",
    "AGCPY", "AGCPY", "AGCPY", "AGCPY", "RGCPY", "RGCPY", "RGCPY", "RGCPY",
]

INSTR_LEN = [
    0, 3, 2, 1, 0, 0, -1, 2,
    0, 3, 3, 3, 0, 0, 0, 0,
    0, 1, 2, 2, 0, 0, 4, 4,
    5, 5, 5, 5, 5, 5, 5, 5,
    3, 3, 3, 3, 3, 3, -2, -2,
    -2, -2, 2, 4, 5, 5, 7, 7,
    3, 3, 1, 1, 2, 2, 2, 2,
    5, 5, 5, 5, 5, 5, 5, 5,
]

VRAM_WORDS = 0x80000
ADDR_MASK = VRAM_WORDS - 1


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class HD63484:
    def __init__(self):
        self.vram = array('H', bytes(2 * VRAM_WORDS))
        self.regs = [0] * 128
        self.unsupported = {}
        self.cmd_log = None       # set to a list to record commands
        self.cmd_count = 0
        # Command timing. clock() returns the current CPU cycle; cpu_per_cycle
        # is CPU cycles per ACRTC 2CLK cycle (0 = commands finish instantly).
        self.clock = None
        self.cpu_per_cycle = 0.0
        self.dots = 0
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.ch = 0
        self.reg8cnt = 0
        self._r8latch = 0
        self.fifo = []            # words of the command being assembled
        self.pending = []         # words waiting while a command executes
        self.busy_until = 0       # CPU cycle when the current command ends
        self.readfifo = []
        self.error = False
        self.org = 0
        self.org_dpd = 0
        self.rwp = 0
        self.rwp_dn = 0
        self.cl0 = self.cl1 = self.ccmp = self.edg = 0
        self.mask = 0xFFFF
        self.ppy = self.pzcy = self.ppx = self.pzcx = 0
        self.psy = self.psx = self.pey = self.pzy = self.pex = self.pzx = 0
        self.xmin = self.ymin = self.xmax = self.ymax = 0
        self.cpx = self.cpy = 0
        self.pattern = [0] * 16
        self.raster = 0

    @property
    def ccr(self):
        return self.regs[1]

    @property
    def mwr1(self):
        return self.regs[0xCA >> 1] & 0xFFF

    def reg(self, r):
        return self.regs[(r >> 1) & 0x7F]

    # ------------------------------------------------------------------
    def _busy(self):
        if not self.busy_until:
            return False
        now = self.clock()
        if now < self.busy_until:
            return True
        self.busy_until = 0
        # the command finished: feed waiting words to the drawing processor
        while self.pending and not self.busy_until:
            self._consume(self.pending.pop(0))
            if self.busy_until and self.clock() >= self.busy_until:
                self.busy_until = 0
        return bool(self.busy_until)

    def status(self):
        # While idle, words go straight to the drawing processor, so the
        # write FIFO looks empty even during a long command (WPTN etc.).
        # While a command executes, words wait in the 16-word FIFO.
        busy = self._busy()
        s = 0
        if not self.pending:
            s |= 0x01                      # WFE
        if len(self.pending) < 16:
            s |= 0x02                      # WFR
        if not busy and not self.pending and not self.fifo:
            s |= 0x20                      # CED
        if self.readfifo:
            s |= 0x04
        if len(self.readfifo) >= 8:
            s |= 0x08
        if self.error:
            s |= 0x80
        return s

    @property
    def irq(self):
        return (self.status() & self.ccr & 0xFF) != 0

    # ------------------------------------------------------------------
    # bus interface; offset 0 = address/status, offset 2 = data
    def write8(self, offset, data):
        # The address register selects a byte: even = high byte, odd = low
        # byte of register ch >> 1. With bit 7 set it steps one byte per
        # access. Only the FIFO (ch 0) pairs bytes into a word.
        data &= 0xFF
        if (offset & 2) == 0:
            self.ch = data
            self.reg8cnt = 0
            return
        ch = self.ch
        r = (ch >> 1) & 0x7F
        if ch == 0:
            if self.reg8cnt == 0:
                self.regs[0] = (self.regs[0] & 0x00FF) | (data << 8)
                self.reg8cnt = 1
            else:
                self.regs[0] = (self.regs[0] & 0xFF00) | data
                self.reg8cnt = 0
                self._fifo_push(self.regs[0])
            return
        if ch & 1:
            self.regs[r] = (self.regs[r] & 0xFF00) | data
        else:
            self.regs[r] = (self.regs[r] & 0x00FF) | (data << 8)
        if ch & 0x80:
            self.ch = ((ch + 1) & 0x7F) | 0x80
        self._reg_written(r)

    def write16(self, offset, data):
        data &= 0xFFFF
        if (offset & 2) == 0:
            self.ch = data & 0xFF
            self.reg8cnt = 0
            return
        ch = self.ch
        r = (ch >> 1) & 0x7F
        if ch & 1:
            self.regs[r] = (self.regs[r] & 0xFF00) | (data >> 8)
            r2 = ((ch + 1) >> 1) & 0x7F
            self.regs[r2] = (self.regs[r2] & 0x00FF) | ((data & 0xFF) << 8)
        else:
            self.regs[r] = data
        self._reg_written(r)
        self._autoinc()
        if self.ch == 0:
            self._fifo_push(self.regs[0])

    def _autoinc(self):
        if self.ch & 0x80:
            self.ch = ((self.ch + 2) & 0x7F) | 0x80

    def _reg_written(self, r):
        if r == 1 and (self.regs[1] & 0x8000):
            # ABT: abort current command, flush FIFO
            self.fifo = []
            self.pending = []
            self.busy_until = 0
            self.error = False
            self.regs[1] &= 0x7FFF

    def read16(self, offset):
        if (offset & 2) == 0:
            s = self.status()
            return (s << 8) | s
        self.reg8cnt = 0
        if self.ch == 0:
            if self.readfifo:
                return self.readfifo.pop(0)
            return 0
        if self.ch == 0x80:
            self.raster = (self.raster + 1) & 0xFFF
            return self.raster
        return self.regs[(self.ch >> 1) & 0x7F]

    def read8(self, offset):
        if (offset & 2) == 0:
            return self.status()
        ch = self.ch
        if ch == 0:
            # two consecutive byte reads of the FIFO give high, then low byte
            if self.reg8cnt == 0:
                self._r8latch = self.read16(offset)
                self.reg8cnt = 1
                return self._r8latch >> 8
            self.reg8cnt = 0
            return self._r8latch & 0xFF
        if ch == 0x80 and not (ch & 1):
            v = self.read16(offset)
        else:
            v = self.regs[(ch >> 1) & 0x7F]
        if ch & 0x80:
            self.ch = ((ch + 1) & 0x7F) | 0x80
        return v & 0xFF if ch & 1 else v >> 8

    # ------------------------------------------------------------------
    def _fifo_push(self, word):
        if self.pending or (self.busy_until and self._busy()):
            self.pending.append(word & 0xFFFF)
            return
        self._consume(word)

    def _consume(self, word):
        self.fifo.append(word & 0xFFFF)
        f = self.fifo
        ln = INSTR_LEN[f[0] >> 10]
        if ln == -1:
            if len(f) < 2:
                return
            ln = f[1] + 2
        elif ln == -2:
            if len(f) < 2:
                return
            ln = 2 * f[1] + 2
        if ln == 0:
            self.unsupported[f[0]] = self.unsupported.get(f[0], 0) + 1
            self.error = True
            self.fifo = []
            return
        if len(f) >= ln:
            self.fifo = []
            self.cmd_count += 1
            if self.cmd_log is not None:
                self.cmd_log.append(tuple(f))
            if self.cpu_per_cycle:
                d0 = self.dots
                self.execute(f)
                cost = self.cost(f, self.dots - d0)
                self.busy_until = self.clock() + int(cost * self.cpu_per_cycle) + 1
            else:
                self.execute(f)

    def cost(self, f, dots):
        """Execution time in 2CLK cycles, from the command table in the
        HD63484 user's manual (p. 173). dots = pixels the command drew."""
        op = f[0]
        P = 6 if op & 4 else 4
        top = op >> 12
        if top < 4:                                   # register access
            if (op & 0xFFF0) == 0x1800:
                return 4 * f[1] + 8                   # WPTN
            return 8
        if top < 8:                                   # word transfers
            if op in (0x4400, 0x4800) or (op & 0xFFFC) == 0x4C00:
                return 12                             # RD / WT / MOD
            x, y = abs(s16(f[-2])) + 1, abs(s16(f[-1])) + 1
            if op == 0x5800:
                return (2 * x + 8) * y + 12           # CLR
            if (op & 0xFFFC) == 0x5C00:
                return (4 * x + 6) * y + 12           # SCLR
            return (6 * x + 10) * y + 12              # CPY / SCPY
        if (op & 0xF800) == 0x8000:
            return 56                                 # AMOVE / RMOVE
        if (op & 0xF800) in (0x8800, 0x9000):
            return P * dots + 54                      # lines, rectangles
        if (op & 0xF000) == 0x9000 or (op & 0xF800) == 0xA000:
            return P * dots + 16 * f[1] + 20          # polylines, polygons
        if (op & 0xFE00) in (0xA800, 0xB000, 0xB400):
            return 8 * dots + 66                      # circle, arcs
        if (op & 0xF800) == 0xA800 or (op & 0xF800) == 0xB800:
            return 10 * dots + 96                     # ellipse, ellipse arcs
        if (op & 0xFFF0) in (0xC000, 0xC400):         # filled rectangles
            if (op & 0xFFF8) == 0xC400:
                a, b = abs(s16(f[1])) + 1, abs(s16(f[2])) + 1
            else:
                a, b = abs(s16(f[1]) - self.cpx) + 1, abs(s16(f[2]) - self.cpy) + 1
            return (P * a + 8) * b + 18
        if (op & 0xFE00) == 0xC800:
            return 20 * dots + 100                    # PAINT
        if (op & 0xFFF8) == 0xCC00:
            return 8                                  # DOT
        if (op & 0xF000) == 0xD000:
            return P * dots + 10 * (((f[1] >> 8) & 15) + 1) + 20   # PTN
        if top >= 14:                                 # AGCPY / RGCPY
            a, b = abs(s16(f[3])) + 1, abs(s16(f[4])) + 1
            return ((P + 2) * a + 10) * b + 70
        return 20

    # ------------------------------------------------------------------
    def execute(self, f):
        op = f[0]
        if op == 0x0400:                         # ORG
            self.org = (((f[1] & 0xFF) << 12) | ((f[2] & 0xFFF0) >> 4)) & ADDR_MASK
            self.org_dpd = f[2] & 0xF
        elif (op & 0xFFE0) == 0x0800:            # WPR
            self._wpr(op & 0x1F, f[1])
        elif (op & 0xFFF0) == 0x1800:            # WPTN
            start = op & 0xF
            n = min(f[1], 16)
            for i in range(n):
                self.pattern[(start + i) & 15] = f[2 + i]
        elif op == 0x4400:                       # RD
            self.readfifo.append(self.vram[self.rwp])
            self.rwp = (self.rwp + 1) & ADDR_MASK
        elif op == 0x4800:                       # WT
            self.vram[self.rwp] = f[1]
            self.rwp = (self.rwp + 1) & ADDR_MASK
        elif op == 0x5800 or (op & 0xFFFC) == 0x5C00:   # CLR / SCLR
            self.doclr16(op, f[1], s16(f[2]), s16(f[3]))
        elif (op & 0xF0FF) == 0x6000 or (op & 0xF0FC) == 0x7000:  # CPY / SCPY
            src = ((f[1] & 0xFF) << 12) | ((f[2] & 0xFFF0) >> 4)
            self.docpy16(op, src, s16(f[3]), s16(f[4]))
        elif op == 0x8000:                       # AMOVE
            self.cpx = s16(f[1]); self.cpy = s16(f[2])
        elif op == 0x8400:                       # RMOVE
            self.cpx += s16(f[1]); self.cpy += s16(f[2])
        elif (op & 0xFF00) == 0x8800:            # ALINE
            self.line(self.cpx, self.cpy, s16(f[1]), s16(f[2]), op & 7, True)
            self.cpx = s16(f[1]); self.cpy = s16(f[2])
        elif (op & 0xFF00) == 0x8C00:            # RLINE
            ex, ey = self.cpx + s16(f[1]), self.cpy + s16(f[2])
            self.line(self.cpx, self.cpy, ex, ey, op & 7, True)
            self.cpx, self.cpy = ex, ey
        elif (op & 0xFFF8) == 0x9000:            # ARCT
            x1, y1 = s16(f[1]), s16(f[2])
            self._rect(self.cpx, self.cpy, x1, y1, op & 7)
            self.cpx, self.cpy = x1, y1
        elif (op & 0xFFF8) == 0x9400:            # RRCT
            x1, y1 = self.cpx + s16(f[1]), self.cpy + s16(f[2])
            self._rect(self.cpx, self.cpy, x1, y1, op & 7)
            self.cpx, self.cpy = x1, y1
        elif (op & 0xFFF8) in (0x9800, 0x9C00):  # APLL / RPLL
            rel = (op & 0xFFF8) == 0x9C00
            sx, sy = self.cpx, self.cpy
            for n in range(f[1]):
                ex, ey = s16(f[2 + n * 2]), s16(f[3 + n * 2])
                if rel:
                    ex += sx; ey += sy
                self.line(sx, sy, ex, ey, op & 7, True)
                sx, sy = ex, ey
            self.cpx, self.cpy = sx, sy
        elif (op & 0xFFF8) in (0xA000, 0xA400):  # APLG / RPLG
            rel = (op & 0xFFF8) == 0xA400
            sx, sy = self.cpx, self.cpy
            for n in range(f[1]):
                ex, ey = s16(f[2 + n * 2]), s16(f[3 + n * 2])
                if rel:
                    ex += sx; ey += sy
                self.line(sx, sy, ex, ey, op & 7, False)
                sx, sy = ex, ey
            self.line(sx, sy, self.cpx, self.cpy, op & 7, False)
        elif (op & 0xFE00) == 0xA800:            # CRCL
            self.circle(self.cpx, self.cpy, f[1] & 0x1FFF, op & 7)
        elif (op & 0xFE00) == 0xAC00:            # ELPS
            self.ellipse(self.cpx, self.cpy, f[1], f[2], f[3], op & 7)
        elif (op & 0xFE00) == 0xB400:            # RARC
            self.rarc(self.cpx, self.cpy, s16(f[1]), s16(f[2]), s16(f[3]), s16(f[4]),
                      1 if op & 0x100 else 0, op & 7)
            self.cpx += s16(f[3]); self.cpy += s16(f[4])
        elif (op & 0xFE00) == 0xBC00:            # REARC
            self.rearc(self.cpx, self.cpy, f[1], f[2], s16(f[3]), s16(f[4]),
                       s16(f[5]), s16(f[6]), op & 0x100, op & 7)
            self.cpx += s16(f[5]); self.cpy += s16(f[6])
        elif (op & 0xFFF8) == 0xC000:            # AFRCT
            self.fill_rect(self.cpx, self.cpy, s16(f[1]), s16(f[2]), op & 7)
        elif (op & 0xFFF8) == 0xC400:            # RFRCT
            dx, dy = s16(f[1]), s16(f[2])
            self.fill_rect(self.cpx, self.cpy, self.cpx + dx, self.cpy + dy, op & 7)
            self.cpy += dy - 1 if dy < 0 else dy + 1
        elif op == 0xC800:                       # PAINT
            self.paint(self.cpx, self.cpy, self.cl0)
        elif (op & 0xFFF8) == 0xCC00:            # DOT
            self.dot(self.cpx, self.cpy, op & 7, self.cl0)
        elif (op & 0xF000) == 0xD000:            # PTN
            self.ptn(op)
        elif (op & 0xF018) == 0xE000:            # AGCPY
            self.agcpy(op, s16(f[1]), s16(f[2]), self.cpx, self.cpy, s16(f[3]), s16(f[4]))
            self._gcpy_move(op, s16(f[3]), s16(f[4]))
        elif (op & 0xF018) == 0xF000:            # RGCPY
            self.agcpy(op, self.cpx + s16(f[1]), self.cpy + s16(f[2]),
                       self.cpx, self.cpy, s16(f[3]), s16(f[4]))
            self._gcpy_move(op, s16(f[3]), s16(f[4]))
        else:
            self.unsupported[op] = self.unsupported.get(op, 0) + 1

    def _wpr(self, r, v):
        if r == 0: self.cl0 = v
        elif r == 1: self.cl1 = v
        elif r == 2: self.ccmp = v
        elif r == 3: self.edg = v
        elif r == 4: self.mask = v
        elif r == 5:
            self.ppy = (v >> 12) & 15; self.pzcy = (v >> 8) & 15
            self.ppx = (v >> 4) & 15; self.pzcx = v & 15
        elif r == 6:
            self.psy = (v >> 12) & 15; self.psx = (v >> 4) & 15
        elif r == 7:
            self.pey = (v >> 12) & 15; self.pzy = (v >> 8) & 15
            self.pex = (v >> 4) & 15; self.pzx = v & 15
        elif r == 8: self.xmin = v
        elif r == 9: self.ymin = v
        elif r == 10: self.xmax = v
        elif r == 11: self.ymax = v
        elif r == 12:
            self.rwp = ((self.rwp & 0xFFF) | ((v & 0xFF) << 12)) & ADDR_MASK
            self.rwp_dn = (v & 0xC000) >> 14
        elif r == 13:
            self.rwp = ((self.rwp & 0xFF000) | ((v & 0xFFF0) >> 4)) & ADDR_MASK

    def _gcpy_move(self, op, ax, ay):
        d = op & 0x700
        if d in (0x000, 0x200):
            self.cpy += abs(ay)
        elif d in (0x100, 0x300):
            self.cpy -= abs(ay)
        elif d in (0x400, 0x500):
            self.cpx += abs(ax)
        else:
            self.cpx -= abs(ax)

    # ------------------------------------------------------------------
    # word-level operations
    def doclr16(self, op, fill, ax0, ay0):
        vram = self.vram
        mw = self.mwr1
        dst = self.rwp
        mode = op & 3
        ay = ay0
        while True:
            ax = ax0
            while True:
                a = dst & ADDR_MASK
                if mode == 0: vram[a] = fill
                elif mode == 1: vram[a] |= fill
                elif mode == 2: vram[a] &= fill
                else: vram[a] ^= fill
                if ax == 0:
                    break
                if ax > 0:
                    dst += 1; ax -= 1
                else:
                    dst -= 1; ax += 1
            if ay0 < 0:
                dst = dst + mw - ax0
                if ay == 0: break
                ay += 1
            else:
                dst = dst - mw - ax0
                if ay == 0: break
                ay -= 1
        self.rwp = dst & ADDR_MASK

    def docpy16(self, op, src, ax0, ay0):
        vram = self.vram
        mw = self.mwr1
        dst = self.rwp
        d = op & 0x700
        if d == 0x000: s1, s2 = 1, -mw - ax0
        elif d == 0x100: s1, s2 = 1, mw - ax0
        elif d == 0x200: s1, s2 = -1, -mw - ax0
        elif d == 0x300: s1, s2 = -1, mw - ax0
        elif d == 0x400: s1, s2 = -mw, 1 - ay0 * -mw
        elif d == 0x500: s1, s2 = mw, 1 - ay0 * mw
        elif d == 0x600: s1, s2 = -mw, -1 + ay0 * -mw
        else: s1, s2 = mw, -1 + ay0 * mw
        mode = op & 7
        ccmp = self.ccmp & 0xFF
        ax, ay = ax0, ay0
        vert = op & 0x800
        while True:
            while True:
                a = dst & ADDR_MASK
                sv = vram[src & ADDR_MASK]
                if mode == 0: vram[a] = sv
                elif mode == 1: vram[a] |= sv
                elif mode == 2: vram[a] &= sv
                elif mode == 3: vram[a] ^= sv
                elif mode == 4:
                    if vram[a] == ccmp: vram[a] = sv
                elif mode == 5:
                    if vram[a] != ccmp: vram[a] = sv
                elif mode == 6:
                    if vram[a] < sv: vram[a] = sv
                else:
                    if vram[a] > sv: vram[a] = sv
                if vert:
                    if ay == 0: break
                    src += -mw if ay0 > 0 else mw
                    dst += s1
                    ay += -1 if ay0 > 0 else 1
                else:
                    if ax == 0: break
                    src += 1 if ax > 0 else -1
                    dst += s1
                    ax += -1 if ax > 0 else 1
            if vert:
                ay = ay0
                if ax0 < 0:
                    src = src - 1 + ay * mw
                    dst += s2
                    if ax == 0: break
                    ax += 1
                else:
                    src = src + 1 - ay * mw
                    dst += s2
                    if ax == 0: break
                    ax -= 1
            else:
                ax = ax0
                if ay0 < 0:
                    src = src + mw - ax
                    dst += s2
                    if ay == 0: break
                    ay += 1
                else:
                    src = src - mw - ax
                    dst += s2
                    if ay == 0: break
                    ay -= 1
        self.rwp = dst & ADDR_MASK

    # ------------------------------------------------------------------
    # pixel operations (4 bits per pixel is what this board uses, but
    # the graphic bit mode from CCR is honoured)
    def _bpp(self):
        gbm = (self.ccr >> 8) & 7
        return (1, 2, 4, 8, 16)[gbm] if gbm <= 4 else 4

    def _first_pixel(self):
        gbm = (self.ccr >> 8) & 7
        if gbm >= 4:
            return 0
        return (self.org_dpd & 0xF) >> gbm

    def dot(self, x, y, opm, color):
        self.dots += 1
        bpp = self._bpp()
        ppw = 16 // bpp
        bitmask = (1 << bpp) - 1
        x += self._first_pixel()
        x_int, x_mod = divmod(x, ppw)
        sh = x_mod * bpp
        color &= bitmask
        bm = bitmask << sh
        cs = color << sh
        dst = (self.org + x_int - y * self.mwr1) & ADDR_MASK
        vram = self.vram
        v = vram[dst]
        nbm = ~bm & 0xFFFF
        if opm == 0:
            vram[dst] = (v & nbm) | cs
        elif opm == 1:
            vram[dst] = v | cs
        elif opm == 2:
            vram[dst] = v & ((v & nbm) | cs)
        elif opm == 3:
            vram[dst] = v ^ cs
        else:
            cur = (v >> sh) & bitmask
            if opm == 4: ok = cur == (self.ccmp & bitmask)
            elif opm == 5: ok = cur != (self.ccmp & bitmask)
            elif opm == 6: ok = cur < (self.cl0 & bitmask)
            else: ok = cur > (self.cl0 & bitmask)
            if ok:
                vram[dst] = (v & nbm) | cs

    def get_pixel(self, x, y):
        bpp = self._bpp()
        ppw = 16 // bpp
        x_int, x_mod = divmod(x, ppw)
        dst = (self.org + x_int - y * self.mwr1) & ADDR_MASK
        return (self.vram[dst] >> (x_mod * bpp)) & ((1 << bpp) - 1)

    def agcpy(self, op, sx, sy, dx, dy, ax0, ay0):
        """Graphic copy, pixel by pixel. Fast path for the common case."""
        d = op & 0x700
        vert = op & 0x800
        opm = op & 7
        if opm == 0 and self._bpp() == 4 and self._first_pixel() == 0:
            if not vert:
                self._agcpy_fast(d, sx, sy, dx, dy, ax0, ay0)
                return
            if d >= 0x400 and self._agcpy_vfast(d, sx, sy, dx, dy, ax0, ay0):
                return
        axn = 1 if ax0 >= 0 else -1
        ayn = 1 if ay0 >= 0 else -1
        if vert:
            table = {
                0x000: (1, 0, -ayn * ay0, 1), 0x100: (1, 0, -ayn * ay0, -1),
                0x200: (-1, 0, ayn * ay0, 1), 0x300: (-1, 0, ayn * ay0, -1),
                0x400: (0, 1, 1, -ayn * ay0), 0x500: (0, -1, 1, ayn * ay0),
                0x600: (0, 1, -1, -ayn * ay0), 0x700: (0, -1, -1, ayn * ay0)}
            ss1x, ss1y, ss2x, ss2y = 0, ayn, axn, -ay0
        else:
            table = {
                0x000: (1, 0, -axn * ax0, 1), 0x100: (1, 0, -axn * ax0, -1),
                0x200: (-1, 0, axn * ax0, 1), 0x300: (-1, 0, axn * ax0, -1),
                0x400: (0, 1, 1, axn * ax0), 0x500: (0, -1, 1, -axn * ax0),
                0x600: (0, 1, -1, axn * ax0), 0x700: (0, -1, -1, -axn * ax0)}
            ss1x, ss1y, ss2x, ss2y = axn, 0, -ax0, ayn
        d1x, d1y, d2x, d2y = table[d]
        ax, ay = ax0, ay0
        xs, ys, xd, yd = sx, sy, dx, dy
        dot, gp = self.dot, self.get_pixel
        while True:
            while True:
                dot(xd, yd, opm, gp(xs, ys))
                if vert:
                    if ay == 0: break
                    ay += -1 if ay0 > 0 else 1
                else:
                    if ax == 0: break
                    ax += -1 if ax > 0 else 1
                xs += ss1x; ys += ss1y; xd += d1x; yd += d1y
            xs += ss2x; ys += ss2y; xd += d2x; yd += d2y
            if vert:
                ay = ay0
                if ax == 0: break
                ax += 1 if ax0 < 0 else -1
            else:
                ax = ax0
                if ay == 0: break
                ay += 1 if ay0 < 0 else -1

    def _agcpy_vfast(self, d, sx, sy, dx, dy, ax0, ay0):
        """Vertical-scan replace-mode copy at 4bpp whose destination columns
        are vertical (DSD 4-7), one column at a time with numpy, in the
        chip's column order. Returns False (nothing done) if a column would
        read pixels it has itself just written; the slow path handles that."""
        axn = 1 if ax0 >= 0 else -1
        ayn = 1 if ay0 >= 0 else -1
        d1y = 1 if d in (0x400, 0x600) else -1      # destination step within a column
        d2x = 1 if d in (0x400, 0x500) else -1      # destination step between columns
        ncol = abs(ax0) + 1
        n = abs(ay0) + 1
        j = np.arange(n)
        ys = sy + j * ayn                          # same rows for every column
        yd = dy + j * d1y
        # a column reads pixel j after writing pixels 0..j-1: conflict only if a
        # source pixel equals an earlier destination pixel of the same column
        if any(sx + i * axn == dx + i * d2x for i in range(ncol)):
            pos = {int(y): k for k, y in enumerate(yd)}
            for k, y in enumerate(ys):
                w = pos.get(int(y))
                if w is not None and w < k:
                    return False
        V = np.frombuffer(self.vram, dtype=np.uint16)
        org, mw = self.org, self.mwr1
        srow = org - ys * mw
        drow = org - yd * mw
        for i in range(ncol):
            xs = sx + i * axn
            xd = dx + i * d2x
            sa = (srow + (xs >> 2)) & ADDR_MASK
            pix = (V[sa] >> ((xs & 3) << 2)) & 15
            da = (drow + (xd >> 2)) & ADDR_MASK
            sh = (xd & 3) << 2
            V[da] = (V[da] & (~(15 << sh) & 0xFFFF)) | (pix << sh)
        return True

    def _agcpy_fast(self, d, sx, sy, dx, dy, ax0, ay0):
        """Horizontal-scan replace-mode copy at 4bpp, same result as the slow path."""
        axn = 1 if ax0 >= 0 else -1
        ayn = 1 if ay0 >= 0 else -1
        table = {
            0x000: (1, 0, 0, 1), 0x100: (1, 0, 0, -1),
            0x200: (-1, 0, 0, 1), 0x300: (-1, 0, 0, -1),
            0x400: (0, 1, 1, 0), 0x500: (0, -1, 1, 0),
            0x600: (0, 1, -1, 0), 0x700: (0, -1, -1, 0)}
        d1x, d1y, d2x, d2y = table[d]
        vram = self.vram
        org = self.org
        mw = self.mwr1
        nx = abs(ax0) + 1
        ny = abs(ay0) + 1
        for j in range(ny):
            ys = sy + j * ayn
            srow = org - ys * mw
            # start of this destination line
            xd0 = dx + j * d2x
            yd0 = dy + j * d2y
            xs = sx
            xd, yd = xd0, yd0
            for i in range(nx):
                w = vram[(srow + (xs >> 2)) & ADDR_MASK]
                c = (w >> ((xs & 3) << 2)) & 15
                a = (org + (xd >> 2) - yd * mw) & ADDR_MASK
                sh = (xd & 3) << 2
                vram[a] = (vram[a] & ~(15 << sh) & 0xFFFF) | (c << sh)
                xs += axn
                xd += d1x
                yd += d1y

    def ptn(self, op):
        ax0 = (self.pex - self.psx) - 2
        ay0 = self.pey - self.psy
        if op & 0x800:
            self.unsupported[op] = self.unsupported.get(op, 0) + 1
            return
        axn = 1 if ax0 >= 0 else -1
        d = op & 0x700
        table = {0x000: (1, 0, -axn * ax0, 1), 0x200: (0, 1, -1, -axn * ax0),
                 0x400: (-1, 0, axn * ax0, -1), 0x600: (0, -1, 1, axn * ax0)}
        if d not in table:
            self.unsupported[op] = self.unsupported.get(op, 0) + 1
            return
        d1x, d1y, d2x, d2y = table[d]
        ss1x, ss1y, ss2x, ss2y = 1, 0, -ax0, 1
        xs, ys, xd, yd = self.psx, self.psy, self.cpx, self.cpy
        ax, ay = ax0, ay0
        mode = (op >> 3) & 3
        opm = op & 7
        while True:
            while True:
                bit = (self.pattern[ys & 15] >> (xs & 15)) & 1 if 0 <= xs < 16 else 0
                if mode == 0:
                    self.dot(xd, yd, opm, self.cl1 if bit else self.cl0)
                elif mode == 1:
                    if bit: self.dot(xd, yd, opm, self.cl1)
                elif mode == 2:
                    if not bit: self.dot(xd, yd, opm, self.cl0)
                if ax == 0: break
                ax += -1 if ax > 0 else 1
                xs += ss1x; ys += ss1y; xd += d1x; yd += d1y
            xs += ss2x; ys += ss2y; xd += d2x; yd += d2y
            ax = ax0
            if ay == 0: break
            ay += 1 if ay0 < 0 else -1
        h = self.pey - self.psy
        if d == 0x000: self.cpy += h + 1 if h > 0 else -h
        elif d == 0x200: self.cpx += h if h > 0 else -h
        elif d == 0x400: self.cpy -= h if h > 0 else -h
        elif d == 0x600: self.cpx -= h if h > 0 else -h

    # ------------------------------------------------------------------
    # figures
    def line(self, x0, y0, x1, y1, opm, nolastpix):
        old = self.get_pixel(x1, y1)
        dy = y1 - y0
        dx = x1 - x0
        sy = 1
        sx = 1
        if dy < 0: dy, sy = -dy, -1
        if dx < 0: dx, sx = -dx, -1
        dy <<= 1
        dx <<= 1
        dot, c = self.dot, self.cl0
        dot(x0, y0, opm, c)
        if dx > dy:
            frac = dy - (dx >> 1)
            while x0 != x1:
                if frac >= 0:
                    y0 += sy; frac -= dx
                x0 += sx; frac += dy
                dot(x0, y0, opm, c)
        else:
            frac = dx - (dy >> 1)
            while y0 != y1:
                if frac >= 0:
                    x0 += sx; frac -= dy
                y0 += sy; frac += dx
                dot(x0, y0, opm, c)
        if nolastpix:
            self.dot(x1, y1, 0, old)

    def _rect(self, x0, y0, x1, y1, opm):
        self.line(x0, y0, x1, y0, opm, False)
        self.line(x1, y0, x1, y1, opm, False)
        self.line(x1, y1, x0, y1, opm, False)
        self.line(x0, y1, x0, y0, opm, False)

    def fill_rect(self, x0, y0, x1, y1, opm):
        stx = 1 if x1 >= x0 else -1
        sty = 1 if y1 >= y0 else -1
        c = self.cl0
        if opm == 0 and self._bpp() == 4 and self._first_pixel() == 0:
            vram, org, mw = self.vram, self.org, self.mwr1
            c &= 15
            for y in range(y0, y1 + sty, sty):
                row = org - y * mw
                for x in range(x0, x1 + stx, stx):
                    a = (row + (x >> 2)) & ADDR_MASK
                    sh = (x & 3) << 2
                    vram[a] = (vram[a] & ~(15 << sh) & 0xFFFF) | (c << sh)
            return
        for x in range(x0, x1 + stx, stx):
            for y in range(y0, y1 + sty, sty):
                self.dot(x, y, opm, c)

    def circle(self, xc, yc, r, opm):
        dot, c = self.dot, self.cl0
        for px, py in ((0, r), (0, -r), (r, 0), (-r, 0)):
            dot(xc + px, yc + py, opm, c)
        x = 1
        y = int(math.sqrt(max(r * r - 1, 0)) + 0.5)
        while x < y:
            for px, py in ((x, y), (x, -y), (-x, y), (-x, -y),
                           (y, x), (y, -x), (-y, x), (-y, -x)):
                dot(xc + px, yc + py, opm, c)
            x += 1
            y = int(math.sqrt(max(r * r - x * x, 0)) + 0.5)
        if x == y:
            for px, py in ((x, y), (x, -y), (-x, y), (-x, -y)):
                dot(xc + px, yc + py, opm, c)

    def _ellipse_points(self, A, B, dx):
        """Integer points of B*x*x + A*y*y = B*dx*dx around (0, 0) (ELPS).

        A midpoint walk scaled by four, evaluated against the true equation,
        so the second axis dx*sqrt(B/A) is never rounded to a whole ellipse.
        Method from Vesuri's RAY Pokeri Amiga port
        (github.com/vesuri/raypokeri, Hd63484::curve). Same points as
        cray's ellipse_points().
        """
        if not A or not B:
            return {(0, 0)}
        a, b, R = A, B, B * dx * dx
        # y axis rounded to the nearest whole pixel; it starts the walk
        qy = 0
        bit = 1 << 15
        while bit:
            if a * (qy | bit) ** 2 <= R:
                qy |= bit
            bit >>= 1
        if 4 * R >= a * (2 * qy + 1) ** 2:
            qy += 1
        pts = set()
        # flat part: the start includes the residual a*qy*qy - R, since the
        # rounded axis needn't lie on the curve
        qx, ddx, ddy = 0, 0, 2 * a * qy
        d = 4 * b - 4 * a * qy + a + 4 * (a * qy * qy - R)
        while ddx < ddy:
            pts.update(((qx, qy), (-qx, qy), (qx, -qy), (-qx, -qy)))
            qx += 1
            ddx += 2 * b
            if d < 0:
                d += 4 * ddx + 4 * b
            else:
                qy -= 1
                ddy -= 2 * a
                d += 4 * ddx - 4 * ddy + 4 * b
        # steep part
        d = b * (2 * qx + 1) ** 2 + 4 * a * (qy - 1) ** 2 - 4 * R
        while qy >= 0:
            pts.update(((qx, qy), (-qx, qy), (qx, -qy), (-qx, -qy)))
            qy -= 1
            ddy -= 2 * a
            if d > 0:
                d += 4 * a - 4 * ddy
            else:
                qx += 1
                ddx += 2 * b
                d += 4 * ddx - 4 * ddy + 4 * a
        return pts

    def ellipse(self, cx, cy, A, B, dx, opm):
        """ELPS: full ellipse B*x*x + A*y*y = B*dx*dx around (cx, cy)."""
        pts = self._ellipse_points(A, B, dx)
        dot, c = self.dot, self.cl0
        for x, y in pts:
            dot(cx + x, cy + y, opm, c)

    def rarc(self, cpx, cpy, dxc, dyc, dxe, dye, cw, opm):
        # current point is the start, (dxc, dyc) the centre, (dxe, dye) the end
        r = math.sqrt(dxc * dxc + dyc * dyc)
        self._trace_arc(cpx + dxc, cpy + dyc, r, r,
                        -dxc, -dyc, dxe - dxc, dye - dyc, bool(cw), opm)

    def rearc(self, cpx, cpy, a, b, dxc, dyc, dxe, dye, cw, opm):
        # ellipse x^2/a + y^2/b = R^2 through the start point
        if not a or not b:
            return
        R = math.sqrt(dxc * dxc / a + dyc * dyc / b)
        self._trace_arc(cpx + dxc, cpy + dyc, math.sqrt(abs(a)) * R, math.sqrt(abs(b)) * R,
                        -dxc, -dyc, dxe - dxc, dye - dyc, bool(cw), opm)

    def _trace_arc(self, cx, cy, rx, ry, sdx, sdy, edx, edy, cw, opm):
        """Arc of the ellipse with semi-axes rx, ry (not rounded) around
        (cx, cy), from the start point (sdx, sdy) to the end point (edx, edy),
        both relative to the centre. Like the chip it walks the curve from
        the start pixel, so the arc always joins the lines drawn before and
        after it and a following PAINT stays inside the outline. The pixel
        path is kept 8-connected and one pixel thin. Y grows upwards, so
        clockwise means decreasing angle; end == start is a full ellipse."""
        if rx <= 0 or ry <= 0:
            self.dot(cx + sdx, cy + sdy, opm, self.cl0)
            return
        t0 = math.atan2(sdy / ry, sdx / rx)
        full = (sdx, sdy) == (edx, edy)
        if full:
            sweep = 2 * math.pi
        else:
            t1 = math.atan2(edy / ry, edx / rx)
            sweep = ((t0 - t1) if cw else (t1 - t0)) % (2 * math.pi)
            # When the end point is off the curve, end at the curve point
            # nearest to it (searched within 90 degrees of its direction).
            # The card fonts are drawn that way: e.g. the 5's bowl ends at
            # its bottom instead of curling up towards the end point.
            sgn = -1 if cw else 1
            best = None
            for k in range(-90, 91):
                s = sweep + math.radians(k)
                if not 0 <= s <= 2 * math.pi:
                    continue
                t = t0 + sgn * s
                d = (rx * math.cos(t) - edx) ** 2 + (ry * math.sin(t) - edy) ** 2
                if best is None or d < best[0] - 1e-9:
                    best = (d, s)
            sweep = best[1]
        # pixels nearest the true curve: one per column where the curve is
        # flat, one per row where it is steep
        floor, sqrt = math.floor, math.sqrt
        ring = set()
        d = sqrt(rx * rx + ry * ry)
        xb, yb = rx * rx / d, ry * ry / d
        for x in range(-floor(xb), floor(xb) + 1):
            y = floor(ry * sqrt(max(0.0, 1 - x * x / (rx * rx))) + 0.5)
            ring.update(((x, y), (x, -y)))
        for y in range(-floor(yb), floor(yb) + 1):
            x = floor(rx * sqrt(max(0.0, 1 - y * y / (ry * ry))) + 0.5)
            ring.update(((x, y), (-x, y)))
        # keep the ones from the start to the end, in drawing order
        two_pi = 2 * math.pi
        sel = []
        for x, y in ring:
            t = math.atan2(y / ry, x / rx)
            off = ((t0 - t) if cw else (t - t0)) % two_pi
            if full or off <= sweep + 1e-9:
                sel.append((off, x, y))
        sel.sort()
        pts = [(sdx, sdy)]
        for _, x, y in sel:
            p = (x, y)
            if p == pts[-1] or p in pts[-2:]:
                continue
            if abs(x - pts[-1][0]) > 1 or abs(y - pts[-1][1]) > 1:
                # bridge a gap (only near the start point)
                px, py = pts[-1]
                while abs(x - px) > 1 or abs(y - py) > 1:
                    px += (x > px) - (x < px)
                    py += (y > py) - (y < py)
                    pts.append((px, py))
            pts.append(p)
        if full and len(pts) > 1 and pts[-1] == pts[0]:
            pts.pop()
        # The arc ends at the end angle. The end point itself need not lie
        # on the curve, and the chip does not draw out to it (manual, RARC:
        # "a dot is not drawn at Pe"; only CP moves there).
        # drop corner pixels: a pixel whose neighbours already touch
        thin = [pts[0]]
        for i in range(1, len(pts) - 1):
            px, py = thin[-1]
            nx, ny = pts[i + 1]
            if abs(nx - px) <= 1 and abs(ny - py) <= 1:
                continue
            thin.append(pts[i])
        if len(pts) > 1:
            thin.append(pts[-1])
        dot, c = self.dot, self.cl0
        for x, y in thin:
            dot(cx + x, cy + y, opm, c)

    def paint(self, sx, sy, col):
        """Flood fill bounded by the edge colour (iterative)."""
        bpp = self._bpp()
        rep = {4: 0x1111, 8: 0x0101}.get(bpp, 1)
        edg = self.edg
        stack = [(sx, sy)]
        seen = set()
        n = 0
        while stack and n < 200000:
            x, y = stack.pop()
            if (x, y) in seen:
                continue
            seen.add((x, y))
            n += 1
            self.dot(x, y, 0, col)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (nx, ny) in seen:
                    continue
                p = self.get_pixel(nx, ny) * rep
                if p != col and p != edg:
                    stack.append((nx, ny))
