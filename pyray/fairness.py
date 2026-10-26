"""Shows what the game's payout controller is doing (see GAME.txt).

Read-only: everything here comes from reading the game's RAM and ROM.

After the draw, the game rates the hand. For two pairs, three of a kind
and (from bet level 3) a straight it compares
    threshold = table[bet level][hand] + correction
with a random byte 0..255; if the threshold is higher it throws away one
of the cards that made the win (always a freshly drawn one) and deals
again until the hand no longer wins. The correction depends on the
machine's running payout percentage: +3..+13 above 90 %, -3..-13 below
80 %, 0 in between. See the draw code at ROM 0x1917A-0x194CE, the
correction at 0x18E28 and the payout formula at 0xD63A.

Counting take-backs: every dealt card is marked with the current round
number in a "used" table (A6-31060). A card that is taken back stays
marked, so after a draw

    cards marked this round = 5 + cards replaced by the player + taken back.
"""

A6 = 0x48B00
ROUND = A6 - 31177          # byte, +1 at the end of every draw
USED = A6 - 31060           # byte per card: round it was dealt in
HELD = A6 - 31084           # 5 longs: hold flags of the last draw
DRAW_STATE = A6 - 31180     # byte: 1 after the deal, 0 after the draw
CORRECTION = A6 - 31006     # long
BET = A6 - 29496            # long
PARAMS = A6 - 29534         # pointer to the game's parameter block
HAND_NAMES = ("2 pairs", "3 of a kind", "straight")


def s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


class Fairness:
    def __init__(self, machine):
        self.m = machine
        self.draws = 0
        self.taken_back = 0          # draws where a win was taken back
        self._round = None
        self._state = None

    # --- per frame -------------------------------------------------------
    def poll(self):
        mem = self.m.mem
        rnd, state = mem.r8(ROUND), mem.r8(DRAW_STATE)
        if (self._round is not None and rnd == (self._round + 1) & 0xFF
                and self._state == 1 and state == 0):
            # a draw just finished
            marked = sum(1 for i in range(56) if mem.r8(USED + i) == self._round)
            drawn = sum(1 for i in range(5) if mem.r32(HELD + 4 * i) == 0)
            extra = marked - 5 - drawn
            if 0 <= extra <= 20:
                self.draws += 1
                if extra:
                    self.taken_back += 1
        self._round, self._state = rnd, state

    # --- live values -----------------------------------------------------
    def payout(self):
        """Running payout percentage, as the game computes it (0xD63A)."""
        if not self.m.books_valid():
            return None
        b = lambda o: s32(self.m.book_read(o))
        bets = b(108)
        if bets <= 0:
            return 0.0
        d = bets - b(4) + b(8)
        if d < 0:
            return 0.0
        return 100.0 * (b(120) + d + b(116) + b(206)) / bets

    def _params(self):
        p = self.m.mem.r32(PARAMS)
        return p if 0 < p < 0x40000 - 3400 else None

    def _level(self, x):
        """Bet -> parameter table index, like ROM 0x18ABC. With the usual
        setting (5 bet steps) that is the bet itself."""
        steps = self.m.book_read(200, 2) // max(1, self.m.book_read(198, 2))
        if steps <= 5:
            return max(0, min(x, 5))
        return max(0, min(5, x * 5 // steps))

    def status(self):
        """Dict for the GUI, or None before the game is running."""
        mem = self.m.mem
        p, bet = self._params(), s32(mem.r32(BET))
        if p is None or not 1 <= bet <= 99 or not self.m.books_valid():
            return None
        lvl = self._level(bet - 1)            # table index used by the draw code
        if not 0 <= lvl < 5:
            return None
        corr = s32(mem.r32(CORRECTION))
        joker = self._level(bet) >= mem.r8(p + 3240) and mem.r8(p + 856) != 0
        chances = []
        for k in range(3):
            thr = s16(mem.r16(p + 3246 + 6 * lvl + 2 * k)) + corr
            chances.append(max(0, min(thr, 256)) / 256.0)
        return dict(payout=self.payout(), correction=corr, bet=bet, joker=joker,
                    chances=chances, draws=self.draws, taken_back=self.taken_back)
