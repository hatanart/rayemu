"""MC6850 ACIA with a pluggable serial peer.

Control register write (addr even... here: reg 0):
  bits 0-1: 11 = master reset, otherwise clock divide
  bits 5-6: transmit control (01 = TX irq enabled)
  bit 7:    receive interrupt enable
Status register read:
  bit 0 RDRF, bit 1 TDRE, bit 2 DCD, bit 3 CTS, bit 7 IRQ
"""

from collections import deque


class ACIA:
    def __init__(self, name="acia", peer=None):
        self.name = name
        self.peer = peer          # object with .tx(byte) -> None and .tick()
        self.reset()

    def reset(self):
        self.ctrl = 0x03
        self.rx = deque()
        self.rdr = 0
        self.rdrf = False
        self.overrun = False
        self.log = []

    # --- peer side -------------------------------------------------------
    def receive(self, byte):
        """Peer sends a byte to the CPU."""
        self.rx.append(byte & 0xFF)

    def tick(self):
        """Move one queued byte into the receive data register if it's free."""
        if not self.rdrf and self.rx:
            self.rdr = self.rx.popleft()
            self.rdrf = True

    # --- CPU side --------------------------------------------------------
    @property
    def tx_irq_enabled(self):
        return (self.ctrl & 0x60) == 0x20

    @property
    def irq(self):
        if (self.ctrl & 3) == 3:
            return False
        if (self.ctrl & 0x80) and (self.rdrf or self.overrun):
            return True
        if self.tx_irq_enabled:   # transmitter is always ready here
            return True
        return False

    def status(self):
        if (self.ctrl & 3) == 3:
            return 0x02
        v = 0x02  # TDRE: we transmit instantly
        if self.rdrf:
            v |= 0x01
        if self.overrun:
            v |= 0x20
        if self.irq:
            v |= 0x80
        return v

    def read(self, reg):
        if reg & 1:
            v = self.rdr
            self.rdrf = False
            self.overrun = False
            return v
        return self.status()

    def write(self, reg, val):
        val &= 0xFF
        if reg & 1:
            if self.peer is not None:
                self.peer.tx(val)
        else:
            self.ctrl = val
            if (val & 3) == 3:
                self.rx.clear()
                self.rdrf = False
                self.overrun = False
