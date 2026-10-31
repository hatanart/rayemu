"""Light & sound board behind PIA1.

PIA1 port A = data bus D0-D7.
PIA1 port B: PB0 /WRITE, PB1 MODE (AY BC1), PB2 STROBE, PB7 SSEL (AY BC2).
Lamp data is latched on a rising MODE edge and moved into lamp latch
(data & 7) on a falling STROBE edge. Latch 2 bit 7 drives AY BDIR.

Latch 2 bit 3 (lamp bit 19) is VOL, not a lamp: it turns on Q13, which
shorts the AY mixing point to ground through 1k (R16). With the 10k mixing
resistors and the 28k to the amplifier that leaves 1/4 of the level (-12 dB).
The game switches to loud (VOL off) for the doubling music, the lost-double
"kosh" and sound 10, and back to quiet after them (ROM 0x1D12).
"""

MODE = 0x0200
STROBE = 0x0400
SSEL = 0x8000
VOL = 0x08            # in latch 2
VOL_QUIET = 0.25


class LightSound:
    def __init__(self, machine):
        self.m = machine
        self.reset()

    def reset(self):
        self.lights = [0, 0, 0]
        self.latch = 0
        self.old = 0
        self.ay_addr = 0
        self.ay_regs = [0] * 16
        self.psg_read = False
        self.last_data = 0

    @property
    def sound_gain(self):
        return VOL_QUIET if self.lights[2] & VOL else 1.0

    @property
    def lamps(self):
        return self.lights[0] | (self.lights[1] << 8) | (self.lights[2] << 16)

    def pia_written(self, pia, reg):
        if reg not in (0, 2):
            return
        a = pia.output(pia.a)
        b = pia.output(pia.b)
        data = a | (b << 8)
        changes = self.old ^ data
        self.last_data = a
        if (changes & MODE) and (data & MODE):
            self.latch = a
        if (changes & STROBE) and not (data & STROBE):
            idx = a & 7
            if idx < 3:
                self.lights[idx] = self.latch
        bdir = self.lights[2] & 0x80
        bc1 = data & MODE
        bc2 = data & SSEL
        self.psg_read = False
        if not bdir and bc1 and not bc2:
            self.ay_addr = a & 15
        elif not bdir and bc1 and bc2:
            self.psg_read = True
        elif bdir and not bc1 and not bc2:
            self.ay_addr = a & 15
        elif bdir and bc2 and not bc1:
            self.ay_regs[self.ay_addr] = a
            m = self.m
            m.ay.write(self.ay_addr, a, m.cycles - m.frame_start)
        elif bdir and bc2 and bc1:
            self.ay_addr = a & 15
        # feed data back so reads of port A see the bus
        if self.psg_read:
            pia.a.pins = self.ay_regs[self.ay_addr]
        else:
            pia.a.pins = a
        self.old = data
