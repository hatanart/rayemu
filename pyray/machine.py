"""RAY video poker board: 68000 + HD63484 + 3x MC6821 + MC6850 ACIAs.

Memory map (from the board notes in raymemmap.txt and the ROM code):
  000000-03FFFF  program ROM (256 KB)
  040000-04FFFF  RAM; 043800-043FFF is battery backed (MK48T02 style
                 timekeeper, clock registers at 043FF8-043FFF)
  0C0000         MC68681 DUART (level 2 autovector), optional
  0F6000-0F7FFF  HD63484 ACRTC (even = address/status, +2 = data)
  0FB000-0FB013  MC6850 ACIAs, ACIA n at FB000 + 4n + 2 (ctrl), +3 (data)
                 ACIA1 = coin processor link
  0FB014-0FB01F  MC6821 PIA1..3 (light/sound board, buttons, misc)

Interrupts: a priority encoder supplies vectors 64..71 ("int0..int7")
and there are two autovectored sources (level 2 DUART, level 7 NMI).
"""

import ctypes
import time

import machine68k

from .acia import ACIA
from .hd63484 import HD63484
from .pia import PIA
from .lightsound import LightSound
from .coinproc import CoinUnit
from .ay8910 import AY8910
from .fairness import Fairness

CPU_HZ = 8_000_000
ROM_SIZE = 0x40000
RAM_BASE = 0x40000

# vectored interrupt sources -> vector number
VEC_ACRTC = 64      # int0: HD63484 command FIFO handler
VEC_MAINS = 67      # int3: 100 Hz mains phase via PIA1 CB2
VEC_INT5 = 69       # int5
VEC_PIA1A = 70      # int6: PIA1 CA1
VEC_ACIA1 = 71      # int7: coin processor ACIA
VECTORED_LEVEL = 4
# Effective HD63484 drawing clock: the command times in the user's manual
# are 2CLK cycles. The screen timing (96 cycles per line, 304 lines, 50 Hz)
# and the interleaved access mode the game sets (OMR ACM=10, drawing shares
# memory cycles with the display) give roughly 3 MHz. A guess, adjustable.
ACRTC_HZ = 3_000_000
WD_WARN_CYCLES = CPU_HZ * 4 // 10      # ~0.4 s without a kick -> WCHNMI
WD_RESET_CYCLES = CPU_HZ // 10         # then ~100 ms later -> reset (boot test wants >25 ms)


def _load_lib():
    lib = ctypes.CDLL(machine68k.__file__)
    lib.m68k_set_irq.argtypes = [ctypes.c_uint]
    lib.m68k_cycles_run.restype = ctypes.c_int
    return lib


class Machine:
    def __init__(self, rom_path, nvram_path=None, verbose=False, ay_clock=1_000_000,
                 acrtc_clock=ACRTC_HZ):
        self.verbose = verbose
        self.m = machine68k.Machine(machine68k.CPUType.M68000, 0x50000 // 1024)
        self.cpu = self.m.cpu
        self.mem = self.m.mem
        self.lib = _load_lib()

        # a file name, or the ROM image itself (bytes)
        if isinstance(rom_path, (bytes, bytearray)):
            rom = bytes(rom_path)
        else:
            rom = open(rom_path, "rb").read()
        if len(rom) != ROM_SIZE:
            raise ValueError("expected a 256 KB ROM image, got %d bytes" % len(rom))
        self.mem.w_block(0, rom)
        self.rom = rom
        self.fair = Fairness(self)         # payout controller stats (reads RAM only)
        self._hopper_synced = False

        self.nvram_path = nvram_path
        if nvram_path:
            try:
                data = open(nvram_path, "rb").read()
                self.mem.w_block(RAM_BASE, data[:0x10000])
            except FileNotFoundError:
                pass

        # devices
        self.acrtc = HD63484()
        self.in_slice = False
        self.acrtc.clock = self.now
        self.acrtc.cpu_per_cycle = CPU_HZ / acrtc_clock if acrtc_clock else 0.0
        self.coin = CoinUnit(self)
        self.acias = [ACIA("acia%d" % (i + 1)) for i in range(5)]
        self.acias[0].peer = self.coin
        self.coin.acia = self.acias[0]
        self.pia1 = PIA("pia1")
        self.pia2 = PIA("pia2")
        self.pia3 = PIA("pia3")
        self.pias = [self.pia1, self.pia2, self.pia3]
        self.ls = LightSound(self)
        self.ay = AY8910(clock=ay_clock, cpu_hz=CPU_HZ)
        self.sound = False             # render audio into self.audio (list of arrays)
        self.audio = []
        self.frame_start = 0
        self.pia1.on_write = self.ls.pia_written

        self.cycles = 0
        self.frame = 0
        self.unknown_io = {}
        self.io_trace = None          # list to record (pc, rw, addr, val)
        self.int_counts = {}
        self.sevenseg = 0
        self.watchdog_kicks = 0
        self.mirror_hits = 0
        self.pc_ring = None           # deque of PCs sampled per slice (debug)
        self.on_hw_reset = None

        self.set_default_inputs()
        self._map_memory()
        self._reset_cb = self._reset_instruction
        self.cpu.set_reset_instr_callback(self._reset_cb)
        self.reset_instr_count = 0
        self.reset()
        self.coin.power_on()

    # ------------------------------------------------------------------
    def reset(self):
        # (the coin unit is a separate board and keeps running)
        for d in (self.acrtc, self.pia1, self.pia2, self.pia3, self.ls):
            d.reset()
        for a in self.acias:
            a.reset()
        self.cpu.pulse_reset()
        self.nmi_line = False
        self.wd_last_kick = self.cycles
        self.wd_warned = False

    def hardware_reset(self):
        """Reset line pulled by the watchdog: CPU and peripherals, RAM kept."""
        self.resets = getattr(self, "resets", 0) + 1
        if self.on_hw_reset:
            self.on_hw_reset()
        self.reset()

    def _reset_instruction(self):
        """68000 RESET instruction: pulses the peripheral reset line,
        which also restarts the watchdog timer."""
        self.reset_instr_count += 1
        for d in (self.acrtc, self.pia1, self.pia2, self.pia3, self.ls):
            d.reset()
        for a in self.acias:
            a.reset()
        self.kick_watchdog()

    def kick_watchdog(self):
        self.watchdog_kicks += 1
        self.wd_last_kick = self.cycles
        self.wd_warned = False

    def check_watchdog(self):
        idle = self.cycles - self.wd_last_kick
        if not self.wd_warned and idle > WD_WARN_CYCLES:
            self.wd_warned = True
            self.pia3.b.pulse_c2()          # WCHNMI
        elif self.wd_warned and idle > WD_WARN_CYCLES + WD_RESET_CYCLES:
            self.hardware_reset()

    def set_default_inputs(self):
        # Buttons are active low on PIA2 PA0-7, PB0-1, PB5-6, PIA3 PA2-5.
        self.pia2.a.pins = 0xFF
        self.pia2.b.pins = 0xFF
        self.pia2.set_input("b", 7, 0)      # +12V guard: 0 = power OK
        self.pia3.a.pins = 0xFF
        self.pia3.set_input("a", 0, 0)      # CBL1?
        self.pia3.set_input("a", 1, 0)      # CBL2?
        self.pia3.b.pins = 0xFF

    # ------------------------------------------------------------------
    def _map_memory(self):
        mem = self.mem
        # machine68k keeps callback references in a set, so equal-but-distinct
        # bound method objects would be dropped and freed. Create each once.
        self._cb = cb = dict(rom_w=self._rom_w, r8=self._io_r8, r16=self._io_r16,
                             r32=self._io_r32, w8=self._io_w8, w16=self._io_w16,
                             w32=self._io_w32, inv=self._invalid)
        # ROM pages: ignore writes
        for page in range(4):
            mem.set_special_range_write_funcs(page << 16, 1,
                                              w8=cb["rom_w"], w16=cb["rom_w"], w32=cb["rom_w"])
        # pages 5..15: I/O handlers (most of them unmapped)
        for page in range(5, 16):
            mem.set_special_range_read_funcs(page << 16, 1,
                                             r8=cb["r8"], r16=cb["r16"], r32=cb["r32"])
            mem.set_special_range_write_funcs(page << 16, 1,
                                              w8=cb["w8"], w16=cb["w16"], w32=cb["w32"])
        # The board only decodes A0-A19, so the 1 MB map repeats every 1 MB.
        # (The game relies on this: at 0xD05A it loads a message buffer's
        # contents instead of its address and ends up reading a ROM mirror.)
        cb["m8"], cb["m16"], cb["m32"] = self._mirror_r8, self._mirror_r16, self._mirror_r32
        cb["mw8"], cb["mw16"], cb["mw32"] = self._mirror_w8, self._mirror_w16, self._mirror_w32
        for page in range(16, 256):
            mem.set_special_range_read_funcs(page << 16, 1,
                                             r8=cb["m8"], r16=cb["m16"], r32=cb["m32"])
            mem.set_special_range_write_funcs(page << 16, 1,
                                              w8=cb["mw8"], w16=cb["mw16"], w32=cb["mw32"])
        mem.set_invalid_func(cb["inv"])

    def _mirror_r8(self, addr):
        self.mirror_hits += 1
        return self.mem.cpu_r8(addr & 0xFFFFF)

    def _mirror_r16(self, addr):
        self.mirror_hits += 1
        return self.mem.cpu_r16(addr & 0xFFFFF)

    def _mirror_r32(self, addr):
        self.mirror_hits += 1
        return self.mem.cpu_r32(addr & 0xFFFFF)

    def _mirror_w8(self, addr, val):
        self.mem.cpu_w8(addr & 0xFFFFF, val)

    def _mirror_w16(self, addr, val):
        self.mem.cpu_w16(addr & 0xFFFFF, val)

    def _mirror_w32(self, addr, val):
        self.mem.cpu_w32(addr & 0xFFFFF, val)

    def _rom_w(self, addr, val):
        pass

    def _invalid(self, mode, width, addr):
        key = ("inv", chr(mode) if isinstance(mode, int) else mode, addr)
        self.unknown_io[key] = self.unknown_io.get(key, 0) + 1

    # --- I/O dispatch ---------------------------------------------------
    def _io_r8(self, addr):
        v = self.io_read8(addr)
        if self.io_trace is not None:
            self.io_trace.append((self.cpu.r_pc(), "r8", addr, v))
        return v

    def _io_r16(self, addr):
        if 0xF6000 <= addr < 0xF8000:
            v = self.acrtc.read16(addr & 3)
        else:
            v = (self.io_read8(addr) << 8) | self.io_read8(addr + 1)
        if self.io_trace is not None:
            self.io_trace.append((self.cpu.r_pc(), "r16", addr, v))
        return v

    def _io_r32(self, addr):
        return (self._io_r16(addr) << 16) | self._io_r16(addr + 2)

    def _io_w8(self, addr, val):
        if self.io_trace is not None:
            self.io_trace.append((self.cpu.r_pc(), "w8", addr, val))
        self.io_write8(addr, val & 0xFF)

    def _io_w16(self, addr, val):
        if self.io_trace is not None:
            self.io_trace.append((self.cpu.r_pc(), "w16", addr, val))
        if 0xF6000 <= addr < 0xF8000:
            self.acrtc.write16(addr & 3, val)
        else:
            self.io_write8(addr, (val >> 8) & 0xFF)
            self.io_write8(addr + 1, val & 0xFF)

    def _io_w32(self, addr, val):
        self._io_w16(addr, val >> 16)
        self._io_w16(addr + 2, val & 0xFFFF)

    def io_read8(self, addr):
        if 0xF6000 <= addr < 0xF8000:
            return self.acrtc.read8(addr & 3)
        if 0xFB000 <= addr < 0xFB020:
            off = addr - 0xFB000
            if off >= 0x14:
                p = self.pias[(off - 0x14) >> 2]
                return p.read(off & 3)
            if off & 2:
                return self.acias[off >> 2].read(off & 1)
        self._unknown("r", addr)
        return 0xFF

    def io_write8(self, addr, val):
        if 0xF6000 <= addr < 0xF8000:
            self.acrtc.write8(addr & 3, val)
            return
        if 0xFB000 <= addr < 0xFB020:
            off = addr - 0xFB000
            if off >= 0x14:
                p = self.pias[(off - 0x14) >> 2]
                p.write(off & 3, val)
                if p is self.pia3 and (off & 3) == 2:
                    out = self.pia3.output(self.pia3.b)
                    if (out & 0x80) and not (self.sevenseg & 0x80):
                        self.kick_watchdog()
                    self.sevenseg = out
                return
            if off & 2:
                self.acias[off >> 2].write(off & 1, val)
                return
        self._unknown("w", addr)

    def _unknown(self, rw, addr):
        key = (rw, addr)
        n = self.unknown_io.get(key, 0)
        self.unknown_io[key] = n + 1
        if self.verbose and n == 0:
            print("unmapped %s %06X pc=%06X" % (rw, addr, self.cpu.r_pc()))

    # ------------------------------------------------------------------
    # interrupts
    def pending_vectored(self):
        """Highest priority pending vectored source, or None."""
        if self.acias[0].irq:
            return VEC_ACIA1
        if self.pia1.irq_a:
            return VEC_PIA1A
        if self.pia1.irq_b:
            return VEC_MAINS
        if self.acrtc.irq:
            return VEC_ACRTC
        return None

    def take_interrupt(self, level, vector):
        cpu, mem = self.cpu, self.mem
        sr = cpu.r_sr()
        pc = cpu.r_pc()
        new_sr = ((sr | 0x2000) & ~0x8000 & ~0x0700) | (level << 8)
        # Musashi's register-level SR write does not swap stack pointers,
        # so do what the hardware does: save USP, switch A7 to the ISP.
        usp, isp = cpu.r_usp(), cpu.r_isp()
        cpu.w_sr(new_sr)
        if not (sr & 0x2000):
            cpu.w_usp(usp)
            cpu.w_sp(isp)
        sp = (cpu.r_sp() - 6) & 0xFFFFFF
        mem.w16(sp, sr)
        mem.w32(sp + 2, pc)
        cpu.w_sp(sp)
        cpu.w_pc(mem.r32(vector * 4))
        self.int_counts[vector] = self.int_counts.get(vector, 0) + 1

    def check_interrupts(self):
        mask = (self.cpu.r_sr() >> 8) & 7
        # NMI: edge triggered level 7
        nmi = self.pia3.irq_b
        if nmi and not self.nmi_line:
            self.nmi_line = True
            self.take_interrupt(7, 31)
            return
        self.nmi_line = nmi
        if mask < VECTORED_LEVEL:
            vec = self.pending_vectored()
            if vec is not None:
                self.take_interrupt(VECTORED_LEVEL, vec)
                return

    # ------------------------------------------------------------------
    def run_cycles(self, n, slice_cycles=1000):
        """Run the CPU for about n cycles, servicing interrupts between slices."""
        done = 0
        execute = self.cpu.execute
        while done < n:
            self.check_interrupts()
            self.coin.poll(self.cycles)
            for a in self.acias:
                a.tick()
            self.in_slice = True
            r = execute(slice_cycles)
            self.in_slice = False
            if self.pc_ring is not None:
                self.pc_ring.append(self.cpu.r_pc())
            done += r.cycles
            self.cycles += r.cycles
            self.check_watchdog()
        return done

    def now(self):
        """Current CPU cycle, exact also inside a memory callback."""
        if self.in_slice:
            return self.cycles + self.lib.m68k_cycles_run()
        return self.cycles

    def run_frame(self):
        """One 100 Hz tick (the mains phase interrupt rate)."""
        self.frame_start = self.cycles
        ran = self.run_cycles(CPU_HZ // 100)
        if self.sound:
            self.audio.append(self.ay.render(ran))
        else:
            self.ay.events.clear()
        self.frame += 1
        self.fair.poll()
        if not self._hopper_synced and self.books_valid():
            # The coin unit's hopper count isn't saved; the game's books are.
            # Start the hopper with the coins the books say are in it.
            coins = self.book_read(self.BOOK_FLOAT)
            if coins < 0x80000000:
                self.coin.hopper_coins = coins
            self._hopper_synced = True
        # 100 Hz mains phase interrupt on PIA1 CB2
        self.pia1.b.pulse_c2()
        self.coin.tick_100hz()
        if self.frame % 100 == 0:
            self.update_rtc()

    def update_rtc(self):
        def bcd(v):
            return ((v // 10) << 4) | (v % 10)
        t = time.localtime()
        base = 0x43FF8
        ctrl = self.mem.r8(base)
        if ctrl & 0xC0:        # clock being read/written by the program
            return
        self.mem.w8(base + 1, bcd(t.tm_sec))
        self.mem.w8(base + 2, bcd(t.tm_min))
        self.mem.w8(base + 3, bcd(t.tm_hour))
        self.mem.w8(base + 4, bcd(t.tm_wday + 1))
        self.mem.w8(base + 5, bcd(t.tm_mday))
        self.mem.w8(base + 6, bcd(t.tm_mon))
        self.mem.w8(base + 7, bcd(t.tm_year % 100))

    def call(self, addr, max_cycles=2_000_000, **regs):
        """Run a ROM subroutine to completion with interrupts masked, then
        restore the CPU state. regs: d0..d7 / a0..a5 values."""
        R = machine68k.Register
        cpu, mem = self.cpu, self.mem
        names = ["D%d" % i for i in range(8)] + ["A%d" % i for i in range(8)]
        saved = {n: cpu.r_reg(getattr(R, n)) for n in names}
        pc, sr = cpu.r_pc(), cpu.r_sr()
        cpu.w_sr(sr | 0x0700)
        for k, v in regs.items():
            cpu.w_reg(getattr(R, k.upper()), v & 0xFFFFFFFF)
        cpu.w_reg(R.A6, 0x48B00)
        sentinel = 0x000400            # ILLEGAL in the vector area, never executed
        sp = (cpu.r_reg(R.A7) - 0x100 - 4) & 0xFFFFFF
        mem.w32(sp, sentinel)
        cpu.w_reg(R.A7, sp)
        cpu.w_pc(addr)
        done, ran = [False], 0
        lib = self.lib

        def hook(p):
            if p == sentinel:
                done[0] = True
                lib.m68k_end_timeslice()
        self._call_hook = hook
        cpu.set_instr_hook_callback(hook)
        try:
            while not done[0] and ran < max_cycles:
                ran += cpu.execute(1000).cycles
        finally:
            cpu.set_instr_hook_callback(None)
        result = cpu.r_reg(R.D0)
        for n, v in saved.items():
            cpu.w_reg(getattr(R, n), v)
        cpu.w_sr(sr)
        cpu.w_pc(pc)
        if not done[0]:
            raise RuntimeError("ROM call %06X did not return" % addr)
        return result

    # --- bookkeeping ("kassamuisti") ---------------------------------------
    # The books are kept in three copies (pointers at a6-31532/-31528/-31524),
    # in 18-byte records of 16 data bytes + a CRC. ROM 0xDC70 recomputes the
    # CRC of the record holding a field and copies it to the other copies.
    BOOK_PTR = 0x48B00 - 31532
    BOOK_COMMIT = 0xDC70
    HOPPER1, HOPPER2 = 116, 120        # coins in hopper 1 / 2 (longs)
    CLOSURE = 126                      # word: low byte 0xFF = open, else unit<<8|code

    def book_read(self, offset, size=4):
        base = self.mem.r32(self.BOOK_PTR)
        return self.mem.r32(base + offset) if size == 4 else self.mem.r16(base + offset)

    def book_write(self, offset, value, size=4):
        base = self.mem.r32(self.BOOK_PTR)
        if size == 4:
            self.mem.w32(base + offset, value & 0xFFFFFFFF)
        else:
            self.mem.w16(base + offset, value & 0xFFFF)
        for field in (base + offset, base + offset + size - 1):
            self.call(self.BOOK_COMMIT, d0=field, d1=field)

    # Book fields (longs) found by reading the reconciliation at ROM 0xB27C:
    #   [0] = [12] + [18] + [22]                 money held in the machine
    #   [4] + [130] - [144] - [0] - [8] - ... = 0   money in = out + held
    # [12] is the money the game counts on for payouts (it must cover the
    # largest possible win, else "P3 89"); 116 = PELIT, 120 = VOITOT.
    BOOK_HELD, BOOK_IN, BOOK_FLOAT = 0, 4, 12
    # 130 = hopper fills: the game books a fill as [12], [0] and [130]
    # (ROM 0xB538). Unlike money in [4], it doesn't count in the payout %.
    BOOK_FILL = 130
    # 162 = hopper 1 level (word, set in TESTI 1 "HOPPERIN 1 RAJA"). Coins
    # above level + 50 are moved to the cash box, 50 at a time (ROM 0xC3D4).
    HOPPER1_LEVEL = 162
    BOOK_PELIT, BOOK_VOITOT = 116, 120
    # 202 = game features (long), set in TESTI 14 "PELIOMINAISUUDET"; bit
    # list in the manual. Bit 13 "win music" plays the jackpot tune for a
    # win of at least parameter 3306 (50) (ROM 0x1A7DE). Bit 29 "saver
    # off" keeps the attract screen at full brightness (ROM 0x188D6, 0x1AEFA).
    FEATURES = 202
    WIN_MUSIC, SAVER_OFF = 1 << 13, 1 << 29

    def set_default_features(self, value=WIN_MUSIC | SAVER_OFF):
        """New books (features 0): turn on win music and saver off."""
        if self.books_valid() and self.book_read(self.FEATURES) == 0:
            self.book_write(self.FEATURES, value)

    def books_valid(self):
        p = self.mem.r32(self.BOOK_PTR)
        return 0x40000 <= p < 0x4FF00

    def ensure_float(self, coins=1000):
        """Make sure the payout float holds at least `coins`, as if an
        attendant had filled the machine, and lift a 'hoppers empty' (84) or
        'payout reserve too small' (89) closure. Returns coins added."""
        if not self.books_valid():
            return 0
        have = self.book_read(self.BOOK_FLOAT)
        if have >= 0x80000000:
            have -= 1 << 32
        add = max(0, coins - have)
        # the coin unit's hopper must hold at least the coins the books promise
        self.coin.hopper_coins = max(self.coin.hopper_coins, coins)
        if add:
            for f in (self.BOOK_FLOAT, self.BOOK_HELD, self.BOOK_FILL):
                self.book_write(f, self.book_read(f) + add)
        self.set_hopper_level(coins)
        closure = self.book_read(self.CLOSURE, 2)
        if (closure & 0xFF) in (0x84, 0x89):
            self.book_write(self.CLOSURE, 0x00FF, size=2)
        return add

    def power_cycle(self, off_frames=30):
        """Mains off (the game saves its state) and on again; battery RAM kept."""
        self.power_fail()
        for _ in range(off_frames):
            self.run_frame()
        self.pia2.set_input("b", 7, 0)
        self.reset()
        self.coin.power_on()

    def set_hopper_level(self, coins):
        """Set the hopper 1 level (as in TESTI 1) if it is below `coins`, so
        the game doesn't keep moving the float out to the cash box."""
        if self.books_valid() and self.book_read(self.HOPPER1_LEVEL, 2) < min(coins, 0xFFFF):
            self.book_write(self.HOPPER1_LEVEL, min(coins, 0xFFFF), size=2)

    FAULT_LIST = 0x48B00 - 30831     # 10 slots, 0 = empty

    def fault_codes(self):
        """Fault codes the game has queued for the 7-segment display (hex, as shown)."""
        out = []
        for i in range(10):
            v = self.mem.r8(self.FAULT_LIST + i)
            if v:
                out.append("%02X" % v)
        return out

    def power_fail(self):
        """Mains drops: +12V guard goes high and PIA3 CB1 fires the NMI, the
        game then saves its state and halts. Run a few frames afterwards."""
        self.pia2.set_input("b", 7, 1)
        self.pia3.b.pulse_c1()

    def save_nvram(self):
        if self.nvram_path:
            with open(self.nvram_path, "wb") as f:
                f.write(bytes(self.mem.r_block(RAM_BASE, 0x10000)))

    # ------------------------------------------------------------------
    def disasm(self, pc, n=10):
        out = []
        for _ in range(n):
            size, text = self.cpu.disassemble(pc)
            out.append("%06X  %s" % (pc, text))
            pc += size
        return out

    def regs(self):
        R = machine68k.Register
        c = self.cpu
        d = " ".join("D%d=%08X" % (i, c.r_reg(getattr(R, "D%d" % i))) for i in range(8))
        a = " ".join("A%d=%08X" % (i, c.r_reg(getattr(R, "A%d" % i))) for i in range(8))
        return "PC=%06X SR=%04X\n%s\n%s" % (c.r_pc(), c.r_sr(), d, a)
