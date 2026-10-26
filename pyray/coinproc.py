"""Coin processor (PCB 5002, "myntprocessor") on the ACIA1 serial link.

Link protocol (reverse engineered from the game ROM, 0x16E2-0x1952, 0xE226-):
  * A frame is 1..3 data bytes (bit 7 clear) followed by a check byte
    = (~sum(data)) | 0x80. The check byte also marks the end of the frame.
  * Bit 6 of the first byte of a data frame is a toggle that alternates
    between consecutive messages (so a repeat can be detected).
  * Control frames: 30 CF = ENQ, 00 FF / 40 BF = ACK (toggle 0/1),
    28 D7 / 68 97 = NAK, 50 AF = EOT.
  * Either side opens a session with ENQ; the other answers 40 BF. The
    opener then sends data frames, each ACKed with the ACK whose toggle
    matches the frame, and closes with EOT, which is echoed.
  * Command numbers (first byte & 0x3F) for messages to the game are
    listed in the table at ROM 0x9658; entries with bit 7 set carry two
    data bytes, the others none.
"""

from collections import deque

ENQ, ACK0, ACK1, NAK0, NAK1, EOT = 0x30, 0x00, 0x40, 0x28, 0x68, 0x50
CONTROL = {0x30: "ENQ", 0x50: "EOT", 0x00: "ACK0", 0x40: "ACK1", 0x28: "NAK0", 0x68: "NAK1"}
BYTE_CYCLES = 8_000_000 // 960      # 9600 baud, 10 bits per byte


def frame(*data):
    return list(data) + [((~sum(data)) & 0x7F) | 0x80]


def describe(fr):
    if not fr:
        return "?"
    if len(fr) == 2 and fr[0] in CONTROL:
        return CONTROL[fr[0]]
    return "MSG %02X%s %s" % (fr[0] & 0x3F, "'" if fr[0] & 0x40 else "",
                              " ".join("%02X" % b for b in fr[1:-1]))


class CoinProcessor:
    def __init__(self, machine):
        self.m = machine
        self.acia = None
        self.verbose = False
        self.reset()

    def reset(self):
        self.log = []            # (frame number, direction, description)
        self.rxbuf = []
        self.out_bytes = deque()
        self.next_byte_at = 0
        self.outbox = deque()    # messages (lists of data bytes) we want to send
        self.state = "idle"      # idle | enq | send | eot | slave
        self.toggle = 0
        self.current = None
        self.wait = 0
        self.received = []       # messages from the game (data bytes)
        self.on_message = None   # callback(data) for app-level behaviour

    # --- byte level -----------------------------------------------------
    def _send_frame(self, fr):
        self.log.append((self.m.frame, "coin", describe(fr)))
        if self.verbose:
            print("coin -> %-20s %s" % (describe(fr), " ".join("%02X" % b for b in fr)))
        self.out_bytes.extend(fr)

    def tx(self, byte):
        """Byte from the game CPU."""
        self.rxbuf.append(byte)
        if byte & 0x80:
            fr, self.rxbuf = self.rxbuf[-4:], []
            # resynchronise on stray bytes: use the longest valid tail
            for n in range(len(fr), 1, -1):
                if frame(*fr[-n:-1]) == fr[-n:]:
                    fr = fr[-n:]
                    break
            self.log.append((self.m.frame, "game", describe(fr)))
            if self.verbose:
                print("game -> %-20s %s" % (describe(fr), " ".join("%02X" % b for b in fr)))
            self._frame_in(fr)

    def poll(self, cycles):
        """Called often; feeds our bytes to the ACIA at the baud rate."""
        if self.out_bytes and cycles >= self.next_byte_at:
            self.acia.receive(self.out_bytes.popleft())
            self.next_byte_at = cycles + BYTE_CYCLES

    # --- link layer -----------------------------------------------------
    def _frame_in(self, fr):
        if frame(*fr[:-1]) != fr:
            self._send_frame(frame(NAK0))
            return
        ctrl = len(fr) == 2 and fr[0] in CONTROL
        c = fr[0] & 0xBF if fr[0] in (ACK1, NAK1) else fr[0]
        if ctrl and c == ENQ:
            if self.state in ("idle", "slave"):
                self.state = "slave"
                self._send_frame(frame(ACK1))
            return
        if ctrl and c == EOT:
            if self.state in ("slave", "idle"):
                # the game closes its session (or a stale one): answer EOT
                self._send_frame(frame(EOT))
            elif self.state == "send" and self.current:
                # session torn down mid-message: send it again later
                self.outbox.appendleft([self.current[0] & 0x3F] + self.current[1:-1])
            # in 'enq' an EOT means our request was refused; retry later
            self.state = "idle"
            self.current = None
            self.wait = 5
            return
        if ctrl and c == ACK0:
            if self.state == "enq":
                self.state = "send"
                self._send_next()
            elif self.state == "send":
                self.current = None
                self._send_next()
            return
        if ctrl and c == NAK0:
            if self.state == "send" and self.current:
                self._send_frame(self.current)
            elif self.state == "enq":
                self._send_frame(frame(ENQ))
            return
        # data frame
        if self.state != "slave":
            return
        self._send_frame(frame(ACK1 if fr[0] & 0x40 else ACK0))
        data = [fr[0] & 0xBF] + fr[1:-1]
        self.received.append(data)
        if self.on_message:
            self.on_message(data)

    def _send_next(self):
        if self.outbox:
            msg = list(self.outbox.popleft())
            msg[0] = (msg[0] & 0x3F) | (0x40 if self.toggle else 0)
            self.toggle ^= 1
            self.current = frame(*msg)
            self._send_frame(self.current)
        else:
            self.state = "eot"
            self._send_frame(frame(EOT))

    def send(self, *data):
        """Queue an application message to the game."""
        self.outbox.append(list(data))

    def tick_100hz(self):
        if self.wait:
            self.wait -= 1
            return
        if self.state == "idle" and self.outbox:
            # the receiver expects toggle 0 on the first message of a session
            self.toggle = 0
            self.state = "enq"
            self._send_frame(frame(ENQ))
            self.wait = 20
        elif self.state in ("enq", "send", "eot") and not self.out_bytes:
            # no answer: give up this session after a while
            self.wait_timeout = getattr(self, "wait_timeout", 0) + 1
            if self.wait_timeout > 100:
                self.wait_timeout = 0
                self.state = "idle"


class CoinUnit(CoinProcessor):
    """Application layer of the coin processor, from the RAY manual
    "Toimintaselostus, Rahakoneiston ohjain 5002", section 3.2.4.

    Messages from the game (game as master):
      09 n   TESTAUS   n=1 run tests, 2 internal tests, 3 sensor states
      19     ASTATKYS  sensor status query      -> 19 d1 d2 ASTATVAS
      21     KSTATKYS  cable status query       -> 21 d     KSTATVAS
      29     OSTATKYS  control status query     -> 29 d1 d2 OSTATVAS
      31     PARPYYNT  parameter request        -> 31 d1 d2 PARAMANT
      39 d   OSTATANT  set controls
      02/0A/12/1A, 22/2A/32/3A   coin channel inhibits on/off
      03/33/0B/3B  coin diverter to cash box / hopper
      04/0C/14     pay one coin from hopper 1/2/3   -> 05/0D/15 VOITTO
      24/2C/34 n   pay n coins (n < 64)             -> n x VOITTO
      05/0D, 25/2D n  pay to cash box               -> 06/0E KASSAAN
      06 d     LASKOHJ  pulse the mechanical counters
    Messages to the game (coin unit as master):
      01 a b   VIKAILM  fault report (codes in hex, 79 = self-test done)
      03/0B    TULIVMK1/2  coin from channel 1/2 into hopper 1/2
      14/1C    TULIKAN3/4  coin from channel 3/4
      24..3C   TULISETE..  bank note
      04/0C    TULIKAS1/2  coin from channel 1/2 into the cash box
      39       OSTATPYY    ask the game for the control status
    """

    # PARAMANT configuration: coin channel 1 and hopper 1 in use
    PARAM1 = 0x01 | 0x04          # PA.ONRK1, PA.ONVM1
    PARAM2 = 0x00

    def reset(self):
        super().reset()
        self.on_message = self._app
        self.controls = 0
        self.inhibit = {}
        self.diverter = {}
        self.hopper_coins = 100
        self.paid = 0
        self.to_cashbox = 0
        self.counters = [0, 0, 0]
        self.pending = []          # (frames from now, message)
        self.events = []           # human readable log
        self.configured = True
        self.announce_timer = 0

    def power_on(self):
        """The coin unit boots, tests itself and reports a cold start (07).
        The report is repeated until the game asks for the parameters, since
        the game may still be running its own power-up tests."""
        self.configured = False
        self.announce_timer = 150

    def _announce(self):
        if self.configured:
            return
        self.announce_timer -= 1
        if self.announce_timer <= 0:
            self.announce_timer = 300
            if self.state == "idle" and not self.outbox:
                self.send(0x01, 0x07, 0x00)

    def busy(self):
        """(coins still to pay out, coins still to move to the cash box)"""
        pay = sum(1 for p in self.pending if p[1][0] in (0x05, 0x0D, 0x15))
        box = sum(1 for p in self.pending if p[1][0] in (0x06, 0x0E))
        return pay, box

    def _later(self, delay, *msg):
        self.pending.append([delay, list(msg)])

    def _app(self, data):
        cmd = data[0] & 0x3F
        arg = data[1:]
        ev = None
        if cmd == 0x09:
            n = arg[0] if arg else 1
            if n == 1:
                ev = "test request"
                self._later(100, 0x01, 0x79, 0x00)      # self tests done, no fault
            elif n == 2:
                ev = "internal test request"
                self._later(20, 0x09, 0x00, 0x60)       # TEST2VAS: cables 1,2 in
            else:
                ev = "sensor state request"
                self._later(20, 0x11, 0x40)             # TEST3VAS: game cable in
        elif cmd == 0x19:
            self._later(5, 0x19, 0x00, 0x00)
        elif cmd == 0x21:
            self._later(5, 0x21, 0x07)                 # cables 1-3 connected
        elif cmd == 0x29:
            self._later(5, 0x29, self.controls & 0x7F, 0x00)
        elif cmd == 0x31:
            self.configured = True
            ev = "parameter request"
            self._later(5, 0x31, self.PARAM1, self.PARAM2)
        elif cmd == 0x39:
            self.controls = arg[0] if arg else 0
            ev = "controls %02X" % self.controls
        elif cmd in (0x02, 0x0A, 0x12, 0x1A):
            self.inhibit[cmd] = True
        elif cmd in (0x22, 0x2A, 0x32, 0x3A):
            self.inhibit[cmd - 0x20] = False
        elif cmd in (0x03, 0x33, 0x0B, 0x3B):
            self.diverter[cmd & 0x0F] = "hopper" if cmd & 0x30 else "cashbox"
        elif cmd in (0x04, 0x0C, 0x14):
            self._pay(cmd + 1, 1)
        elif cmd in (0x24, 0x2C, 0x34) and arg:
            self._pay({0x24: 0x05, 0x2C: 0x0D, 0x34: 0x15}[cmd], arg[0])
        elif cmd in (0x05, 0x0D):
            self.to_cashbox += 1
            self._later(30, cmd + 1)
        elif cmd in (0x25, 0x2D) and arg:
            # move n coins from the hopper to the cash box, one reply per coin
            ev = "hopper -> cash box %d" % arg[0]
            for i in range(arg[0]):
                self.to_cashbox += 1
                if cmd == 0x25:
                    self.hopper_coins -= 1
                self._later((i + 1) * self.PAY_FRAMES, (0x06, 0x0E)[cmd == 0x2D])
        elif cmd == 0x06:
            for i in range(3):
                if arg and arg[0] & (0x08 << i):
                    self.counters[i] += 1
        else:
            ev = "unhandled %02X %s" % (cmd, arg)
        if ev:
            self.events.append((self.m.frame, ev))

    PAY_FRAMES = 15                # ~7 coins per second

    def _pay(self, reply, n):
        self.events.append((self.m.frame, "payout %d" % n))
        start = max([p[0] for p in self.pending if p[1][0] == reply] + [0])
        for i in range(n):
            if self.hopper_coins <= 0:
                # hopper empty: "voitonmaksukoneisto 1 ei toimi tai rahat lopussa"
                self._later(start + (i + 2) * self.PAY_FRAMES, 0x01, 0x44, 0x00)
                break
            self.paid += 1
            self.hopper_coins -= 1
            self._later(start + (i + 1) * self.PAY_FRAMES, reply)

    def insert_coin(self, channel=1):
        """A coin passed the coin mech (channel 1 -> hopper 1 by default)."""
        self.events.append((self.m.frame, "coin in ch%d" % channel))
        if channel in (1, 2):
            self.hopper_coins += 1     # channels 1/2 feed the hoppers
        self.send({1: 0x03, 2: 0x0B, 3: 0x14, 4: 0x1C}[channel])

    def tick_100hz(self):
        self._announce()
        for p in self.pending:
            p[0] -= 1
        due = [p for p in self.pending if p[0] <= 0]
        self.pending = [p for p in self.pending if p[0] > 0]
        for _, msg in due:
            self.send(*msg)
        super().tick_100hz()
