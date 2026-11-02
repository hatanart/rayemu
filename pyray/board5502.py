"""The extra chips of the video board 5502 ("Videomonitorin ohjain II").

The ordinary board is the 5501 ("Monitorns styrenhet PCB 5501-0" in the
POKER/RKA cabling diagram). The 5502 (schematic PIK-5502-0, 1990-91) has
twice the video memory (1M words) and adds:

  0C0000  MC68681 DUART (IC17): two serial ports, for the central
          computer link. Only what the start-up test needs: the
          interrupt vector register reads 0F after reset, the transmitters
          are always ready and nothing is ever received.
  0D0000  32 KB static RAM (IC28)
  0E0000  Inmos IMS G176 colour palette DAC (IC45): 256 colours of 6 bits
          per gun, which the game loads at start (4 x the same 64).

The game finds the board by writing two words of video memory that are
the same word on a 512K-word board (ROM 0x5B9C). With it found, it loads
the G176, sets up the DUART, tests both at start-up (fault 17 = IC17, 28 =
IC28) and does the screen saver with the G176's pixel mask instead of the
ACRTC's DCR colour outputs (ROM 0x5B3A): brightness 63 -> mask CF, 59 ->
DF, 35 -> EF, 29 -> FF.

G176 colour index: P0-P3 are the pixel (LEV, RED, GRE, BLU), P4 and P5
are pulled high, P6 and P7 latch the memory address bits MAD16/MAD17. The
game fills all four copies alike, so P6/P7 are taken as 0 here. With P4/P5
high, the mask picks one of the game's four sets of 16 colours.
"""

import numpy as np

VRAM_WORDS = 0x100000


class G176:
    def __init__(self):
        self.pal = np.zeros((256, 3), dtype=np.uint8)   # 6 bits per gun
        self.mask = 0xFF
        self.waddr = 0
        self.raddr = 0
        self.wstep = 0
        self.rstep = 0
        self.version = 0            # bumped on every change, for caching

    def write(self, reg, v):
        if reg == 0:                # palette address, for writing
            self.waddr, self.wstep = v, 0
        elif reg == 1:              # palette data: red, green, blue
            self.pal[self.waddr, self.wstep] = v & 0x3F
            self.wstep += 1
            if self.wstep == 3:
                self.wstep = 0
                self.waddr = (self.waddr + 1) & 0xFF
            self.version += 1
        elif reg == 2:              # pixel mask
            self.mask = v
            self.version += 1
        else:                       # palette address, for reading
            self.raddr, self.rstep = v, 0

    def read(self, reg):
        if reg == 0:
            return self.waddr
        if reg == 1:
            v = int(self.pal[self.raddr, self.rstep])
            self.rstep += 1
            if self.rstep == 3:
                self.rstep = 0
                self.raddr = (self.raddr + 1) & 0xFF
            return v
        if reg == 2:
            return self.mask
        return self.raddr

    def palette16(self, mask=None):
        """RGB (8 bits per gun) of the 16 pixel values as shown (or with
        another pixel mask)."""
        idx = (0x30 | np.arange(16)) & (self.mask if mask is None else mask)
        p = self.pal[idx].astype(np.uint16)
        return ((p << 2) | (p >> 4)).astype(np.uint8)


class DUART:
    """MC68681 without its serial ports: enough for the start-up test."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.ivr = 0x0F

    def write(self, reg, v):
        if reg == 12:
            self.ivr = v

    def read(self, reg):
        if reg in (1, 9):           # status A / B: transmitter ready and empty
            return 0x0C
        if reg == 12:               # interrupt vector
            return self.ivr
        if reg == 13:               # input port: the pins have pull-ups
            return 0xFF
        return 0                    # no interrupts, nothing received


class Board5502:
    def __init__(self):
        self.g176 = G176()
        self.duart = DUART()
        self.sram = bytearray(0x8000)

    def reset(self):
        self.duart.reset()

    def read8(self, addr):
        """Byte read, or None when the address isn't on this board."""
        if 0xC0000 <= addr < 0xC0010:
            return self.duart.read(addr & 15)
        if 0xD0000 <= addr < 0xE0000:
            return self.sram[addr & 0x7FFF]
        if 0xE0000 <= addr < 0xE0008:
            return self.g176.read((addr >> 1) & 3)
        return None

    def write8(self, addr, v):
        """Byte write; False when the address isn't on this board."""
        if 0xC0000 <= addr < 0xC0010:
            self.duart.write(addr & 15, v)
        elif 0xD0000 <= addr < 0xE0000:
            self.sram[addr & 0x7FFF] = v
        elif 0xE0000 <= addr < 0xE0008:
            self.g176.write((addr >> 1) & 3, v)
        else:
            return False
        return True
