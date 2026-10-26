"""MC6821 Peripheral Interface Adapter.

Registers (addr & 3): 0 = DDRA / port A, 1 = CRA, 2 = DDRB / port B, 3 = CRB.
CRx bit 2 selects between the data direction register (0) and the port (1).
"""


class Side:
    def __init__(self):
        self.pins = 0xFF        # external input levels (pulled up); not reset
        self.reset()

    def reset(self):
        self.ddr = 0
        self.out = 0
        self.cr = 0
        self.irq1 = False       # CR bit 7: C1 active transition seen
        self.irq2 = False       # CR bit 6: C2 active transition seen (C2 as input)
        self.c1 = 1
        self.c2 = 1

    # control line C1 (always an input)
    def set_c1(self, level):
        level = 1 if level else 0
        if level != self.c1:
            rising = level == 1
            if rising == bool(self.cr & 0x02):
                self.irq1 = True
            self.c1 = level

    # control line C2 when configured as an input
    def set_c2(self, level):
        level = 1 if level else 0
        if level != self.c2:
            if not (self.cr & 0x20):
                rising = level == 1
                if rising == bool(self.cr & 0x10):
                    self.irq2 = True
            self.c2 = level

    def pulse_c1(self):
        """Generate one active edge on C1 (whatever polarity is configured)."""
        if self.cr & 0x02:
            self.set_c1(0); self.set_c1(1)
        else:
            self.set_c1(1); self.set_c1(0)

    def pulse_c2(self):
        if self.cr & 0x10:
            self.set_c2(0); self.set_c2(1)
        else:
            self.set_c2(1); self.set_c2(0)

    @property
    def irq(self):
        if self.irq1 and (self.cr & 0x01):
            return True
        if self.irq2 and (self.cr & 0x08) and not (self.cr & 0x20):
            return True
        return False

    def c2_output(self):
        """Level driven on C2 when it is an output in manual mode, else None."""
        if (self.cr & 0x30) == 0x30:
            return 1 if self.cr & 0x08 else 0
        return None


class PIA:
    def __init__(self, name="pia"):
        self.name = name
        self.a = Side()
        self.b = Side()
        self.on_write = None    # callback(pia, reg) after a port/CR write
        self.on_read = None     # callback(pia, reg) before a port read

    def reset(self):
        self.a.reset()
        self.b.reset()

    @property
    def irq_a(self):
        return self.a.irq

    @property
    def irq_b(self):
        return self.b.irq

    def port_value(self, side):
        # Outputs read back their output latch, inputs read the pins.
        return ((side.out & side.ddr) | (side.pins & ~side.ddr)) & 0xFF

    def output(self, side):
        """Value seen on the pins from the PIA side (inputs float high)."""
        return ((side.out & side.ddr) | (~side.ddr & 0xFF)) & 0xFF

    def read(self, reg):
        reg &= 3
        if self.on_read:
            self.on_read(self, reg)
        s = self.a if reg < 2 else self.b
        if reg & 1:
            v = s.cr & 0x3F
            if s.irq1:
                v |= 0x80
            if s.irq2:
                v |= 0x40
            return v
        if s.cr & 0x04:
            s.irq1 = s.irq2 = False
            return self.port_value(s)
        return s.ddr

    def write(self, reg, val):
        reg &= 3
        val &= 0xFF
        s = self.a if reg < 2 else self.b
        if reg & 1:
            s.cr = (s.cr & 0xC0) | (val & 0x3F)
        elif s.cr & 0x04:
            s.out = val
        else:
            s.ddr = val
        if self.on_write:
            self.on_write(self, reg)

    def set_input(self, port, bit, level):
        s = self.a if port == "a" else self.b
        if level:
            s.pins |= (1 << bit)
        else:
            s.pins &= ~(1 << bit)
