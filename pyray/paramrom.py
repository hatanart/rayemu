"""Game parameter ROM (IC43) to a settings file and back.

IC43 holds one OS-9 module ("gamepara", "g200para") with the game's
settings: the pay table, limits, the payout controller's tables and all
screen texts. The program (IC30, IC38, IC34) only reads it; nothing in it
is executed. See GAME.txt section 8.

    python -m pyray.paramrom dump  roms/8460-1.1_ic43.bin  my.txt
    python -m pyray.paramrom build my.txt  roms/my_ic43.bin

dump writes every byte of the chip: the known settings by name, the texts
as strings, everything else as hex ("raw" and "fill" lines). build reads
it back, recomputes the module's header parity and CRC, and writes a 64 KB
chip. dump followed by build gives the same chip byte for byte.

Offsets: "@534" is a parameter offset (as in GAME.txt, from the start of
the parameter block, chip offset 0x52); raw/fill lines use chip offsets
in hex. Texts use the machine's 7-bit Finnish characters: [ \\ ] { | } in
the ROM are written here as Ä Ö Å ä ö å.
"""

import re
import struct
import sys

CHIP = 0x10000
BLOCK = 0x52                    # parameter block = module entry point
TEXTS, NTEXTS = 986, 244        # text pointer table (relative to the module)

HANDS = "KAKSI PARIA, KOLMOSET, SUORA, VÄRI, TÄYSKÄSI, NELOSET, VÄRISUORA, VIITOSET"

# The lamp lines (bit = latch line, as in cray.conf), from watching the game.
LAMPS = {0: "TUPLAUS", 2: "always on", 3: "PIENI", 4: "lamp test", 6: "VOITONMAKSU",
         7: "VALITSE 5", 8: "PANOS", 10: "always on", 11: "VALITSE 4", 12: "always on",
         13: "lamp test", 14: "VALITSE 3", 15: "JAKO", 16: "SUURI", 19: "idle",
         20: "always on", 21: "VALITSE 2", 22: "VALITSE 1"}


# The board's 16 colours (resistor DAC, worked out from the schematics in
# 2010; pyray/display.py PALETTE).
COLOURS = ["black", "dark grey", "dark red", "red", "dark green", "green", "dark yellow",
           "yellow", "dark blue", "blue", "dark magenta", "magenta", "dark cyan", "cyan",
           "grey", "white"]


def colour_names(v):
    """A 16-bit word = 4 pixels of 4 bits, the first pixel in the low nibble."""
    px = [(v >> (4 * i)) & 15 for i in range(4)]
    if len(set(px)) == 1:
        return COLOURS[px[0]]
    return " / ".join(COLOURS[c] for c in px)


def lamp_names(v):
    return ", ".join(LAMPS.get(b, "bit %d ?" % b) for b in range(32) if v >> b & 1) or "none"


# name, parameter offset, size (1/2/4), count, comment
FIELDS = [
    ("reserve_margin", 84, 4, 1, "payout reserve margin ? (hand pay when held + this <= wins)"),
    ("forced_handpay_limit", 92, 4, 1, "wins above this are paid by hand (bit 27 partial pay pays the rest)"),
    ("unknown_96", 96, 4, 1, "?"),
    ("unknown_100", 100, 4, 1, "?"),
    ("unknown_104", 104, 4, 1, "? (80 in gamepara, 150 in g200para)"),
    ("unknown_108", 108, 4, 1, "?"),
    ("handpay_limit", 112, 4, 1, "jackpot / hand-pay limit"),
    ("handpay_on", 116, 2, 1, "hand pay ('HAE PELINHOITAJA') on if non-zero"),
    ("unknown_118", 118, 2, 1, "?"),
    ("default.coin_values", 134, 1, 4, "factory defaults for TESTI 13: coin 1-4 values (books 170-176)"),
    ("default.notes1_2", 2354, 1, 2, "note 1-8 values ? (books 180-194), in three pieces"),
    ("default.notes3_6", 138, 2, 4, ""),
    ("default.notes7_8", 2356, 2, 2, ""),
    ("default.min_price", 146, 2, 1, "PELIN MINIMIHINTA (books 198)"),
    ("default.max_bet", 148, 2, 1, "MAKSIMIPANOS (books 200)"),
    ("default_features", 122, 4, 1, "factory default of the features word (TESTI 14, books 202)"),
    ("unknown_126", 126, 4, 1, "?"),
    ("unknown_130", 130, 4, 1, "?"),
] + [
    ("lamp.%d" % k, 150 + 4 * k, 4, 1, "lamp mask (copied to A6-%d)" % (32758 - 4 * k) if k == 0 else "")
    for k in range(18)
] + [
    ("lamp_test.%d" % k, 266 + 4 * k, 4, 1, "power-on lamp test, one lamp at a time ?" if k == 0 else "")
    for k in range(20)
] + [
    ("colour.%d" % k, 2270 + 2 * k, 2, 1, "colour, 4 pixels (2270/2272 = text colours ?)" if k == 0 else "")
    for k in range(22)
] + [
    ("pay.bet%d" % (b + 1), 534 + 64 * b, 2, 8, HANDS if b == 0 else "")
    for b in range(5)
] + [
    ("joker", 856, 1, 1, "1 = joker in use"),
    ("alt_takeback", 2360, 2, 1, "other take-back mode (with alt_takeback_on)"),
    ("coin_values", 3216, 2, 12, "coin and note values ?"),
    ("joker_min_bet", 3240, 1, 1, "lowest bet with the joker"),
] + [
    ("takeback.bet%d" % (b + 1), 3246 + 6 * b, 2, 3, "take-back thresholds (GAME.txt section 5)" if b == 0 else "")
    for b in range(5)
] + [
    ("correction_high", 3276, 2, 5, "corrections above 90 % payout (section 7)"),
    ("correction_low", 3286, 2, 5, "corrections below 80 % payout"),
    ("alt_takeback_on", 3304, 2, 1, "see alt_takeback"),
    ("max_double", 3306, 4, 1, "largest win that may be doubled"),
]

FI = {"[": "Ä", "\\": "Ö", "]": "Å", "{": "ä", "|": "ö", "}": "å"}
FI_BACK = {v: k for k, v in FI.items()}


def os9_crc(data):
    acc = 0xFFFFFF
    for byte in data:
        acc ^= byte << 16
        for _ in range(8):
            acc <<= 1
            if acc & 0x1000000:
                acc ^= 0x800063
        acc &= 0xFFFFFF
    return acc


def fix_module(chip):
    """Recompute header parity and module CRC in place; returns module size."""
    size = struct.unpack_from(">I", chip, 4)[0]
    par = 0
    for i in range(0, 0x2E, 2):
        par ^= struct.unpack_from(">H", chip, i)[0]
    struct.pack_into(">H", chip, 0x2E, par ^ 0xFFFF)
    crc = os9_crc(chip[:size - 3]) ^ 0xFFFFFF
    chip[size - 3:size] = crc.to_bytes(3, "big")
    return size


def check_module(chip):
    size = struct.unpack_from(">I", chip, 4)[0]
    par = 0
    for i in range(0, 0x30, 2):
        par ^= struct.unpack_from(">H", chip, i)[0]
    return chip[:2] == b"\x4a\xfc" and par == 0xFFFF and os9_crc(chip[:size]) == 0x800FE3


def module_name(chip):
    off = struct.unpack_from(">I", chip, 12)[0]
    return chip[off:off + 32].split(b"\0")[0].decode("latin-1")


def text_encode(raw):
    out = []
    for b in raw:
        c = chr(b)
        if c in FI:
            out.append(FI[c])
        elif c == '"':
            out.append('\\"')
        elif 0x20 <= b < 0x7F:
            out.append(c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        else:
            out.append("\\x%02X" % b)
    return "".join(out)


def text_decode(s):
    out, i = bytearray(), 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            n = s[i + 1]
            if n == "x":
                out.append(int(s[i + 2:i + 4], 16))
                i += 4
                continue
            out.append({"n": 10, "r": 13, '"': 34}.get(n, ord(n)))
            i += 2
            continue
        if c in FI_BACK:
            out.append(ord(FI_BACK[c]))
        elif ord(c) < 0x80:
            out.append(ord(c))
        else:
            raise ValueError("character %r can't be shown by the machine" % c)
        i += 1
    return bytes(out)


def _texts(chip):
    """[(index, pointer, bytes)] for the text table."""
    out = []
    for i in range(NTEXTS):
        p = struct.unpack_from(">I", chip, BLOCK + TEXTS + 4 * i)[0]
        s = chip[p:chip.index(b"\0", p)] if 0 < p < CHIP else b""
        out.append((i, p, bytes(s)))
    return out


FONT_PTR = 2266                 # long: font, relative to the module
SOUND_LISTS = 2010              # three lists of sound pointers, each ending in 0
GLYPH_ROWS = 14                 # a character: 14 rows of 16 pixels, then the
                                # spacing word (+28) and a zero word (+30)


def _font(chip):
    """(start, number of characters); the font runs up to the module's data."""
    start = struct.unpack_from(">I", chip, BLOCK + FONT_PTR)[0]
    end = struct.unpack_from(">I", chip, 0x40)[0]          # M$IData
    return start, (end - start) // 32


def _sound_lists(chip):
    lists, i = [], 0
    for _ in range(3):
        ptrs = []
        while True:
            p = struct.unpack_from(">I", chip, BLOCK + SOUND_LISTS + 4 * i)[0]
            i += 1
            if not p:
                break
            ptrs.append(p)
        lists.append(ptrs)
    return lists, i


def _sound_steps(chip, p):
    """Walk a sound the way the player (ROM 0xE058) does: 14 sound chip
    registers and a time per step; after a step a 0 ends the sound, 1 starts
    it again, 2 n plays it from the start n more times."""
    items, a = [], p
    while True:
        regs, t = chip[a:a + 14], struct.unpack_from(">b", chip, a + 14)[0]
        items.append(("step", bytes(regs), t))
        a += 15
        c = chip[a]
        if c == 0:
            items.append(("end",))
            return items, a + 1
        if c == 1:
            items.append(("loop",))
            return items, a + 1
        if c == 2:
            items.append(("repeat", chip[a + 1]))
            a += 2
            if chip[a] == 0:
                items.append(("end",))
                return items, a + 1


def _step_line(regs, t):
    r = regs
    return ("  step tone %02x %02x  %02x %02x  %02x %02x  noise %02x  mix %02x  vol %02x %02x %02x"
            "  env %02x %02x  shape %02x  time %d" % (tuple(r) + (t,)))


# The numbered sounds as they sound in the game (listened to by the
# emulator's author). The bet sound is sound (bet mod 5) + 1 (ROM 0x17BF4).
SOUND_NAMES = {
    0: "card laid on the table",
    1: "ding: bet 5, also at start-up",
    2: "sad (minor) ding: bet back to 1",
    3: "bet 2", 4: "bet 3", 5: "bet 4",
    6: "shuffle",
    7: "heartbeat",
    8: "one for each coin won",
    9: "another kind of shuffle",
    10: "choosing (holding) a card",
    11: "'kosh': double lost",
    12: "doubling music",
    13: "attract mode ocean waves",
    14: "attract mode slower ocean waves",
    15: "jackpot music (feature bit 13, win music)",
}

SPECIAL = {0x7E: "heart", 0x7F: "diamond", 0x80: "spade", 0x81: "club",
           0x91: "middle finger (never used)", 0x92: "member (never used)",
           0x82: "J (joker ?)", 0x83: "card rank 1", 0x8C: "card rank 10",
           0x8D: "card rank J", 0x8E: "card rank Q", 0x8F: "card rank K", 0x90: "card rank A"}
SPECIAL.update({0x84 + i: "card rank %d" % (i + 2) for i in range(8)})


def version_str(b):
    """01 01 84 60 -> '8460-1.1' (as TESTI 2 prints it)."""
    return "%02X%02X-%d.%d" % (b[2], b[3], b[0], b[1])


def version_bytes(s):
    m = re.fullmatch(r"(\d{4})-(\d)\.(\d)", s)
    if not m:
        raise ValueError("version is NNNN-N.N, digits only (e.g. 8460-1.1)")
    return bytes([int(m.group(2)), int(m.group(3))]) + bytes.fromhex(m.group(1))


def char_name(code):
    c = chr(code)
    return SPECIAL.get(code) or (FI.get(c, c) if 0x21 <= code < 0x7F else "")


def _messages(chip):
    """NUL-terminated texts right after the last text of the table."""
    end = max(p + len(t) + 1 for i, p, t in _texts(chip) if 0 < p < CHIP)
    def text_at(a):
        z = chip.index(b"\0", a)
        t = chip[a:z]
        ok = len(t) >= 3 and all(0x20 <= c < 0x7F or c in (10, 13) for c in t)
        return bytes(t) if ok else None

    out = []
    t = text_at(end) if chip[end] else None
    while t is not None:
        nxt = end + len(t) + 1
        while chip[nxt] == 0 and nxt < CHIP - 1:    # padding left by a shortened message
            nxt += 1
        t2 = text_at(nxt)
        # the room is up to the next message; the last one keeps its own length
        out.append((end, t, nxt - end - 1 if t2 is not None else len(t)))
        end, t = nxt, t2
    return out


def dump(chip, f):
    chip = bytes(chip)
    if len(chip) != CHIP:
        raise SystemExit("expected a 64 KB chip, got %d bytes" % len(chip))
    if not check_module(chip):
        print("warning: module header parity or CRC is wrong", file=sys.stderr)
    size = struct.unpack_from(">I", chip, 4)[0]
    owned = bytearray(CHIP)                  # bytes written by named lines
    owned[0x2E:0x30] = b"\1\1"                # header parity, recomputed
    owned[size - 3:size] = b"\1\1\1"          # CRC, recomputed

    w = f.write
    w("# RAY game parameter ROM (IC43), from pyray.paramrom dump.\n")
    w("# module %r, size %X; build recomputes header parity and CRC.\n" % (module_name(chip), size))
    w("# @N = parameter offset (GAME.txt section 8), chip offset 0x52 + N.\n\n")

    # TESTI 2 "AUTOMAATIN TIEDOT" shows these two: give an edited chip
    # its own version and text, so nobody takes it for RAY's.
    owned[BLOCK:BLOCK + 4] = b"\1" * 4
    w("version = %s" % version_str(chip[BLOCK:BLOCK + 4]).ljust(30) +
      " # @0: part number - version (BCD), shown in TESTI 2\n")
    built = chip[size:chip.index(b"\0", size)]
    owned[size:size + len(built) + 1] = b"\1" * (len(built) + 1)
    w('built max %d = "%s"   # text after the module, shown in TESTI 2\n\n'
      % (len(built), text_encode(built)))

    fmt = {1: ">b", 2: ">h", 4: ">i"}
    for name, off, size_, count, comment in FIELDS:
        vals = [struct.unpack_from(fmt[size_], chip, BLOCK + off + size_ * k)[0] for k in range(count)]
        owned[BLOCK + off:BLOCK + off + size_ * count] = b"\1" * (size_ * count)
        if name.startswith("colour"):
            vals = [v & 0xFFFF for v in vals]
            line = "%-22s = %s" % (name, "0x%04X" % vals[0])
            comment = (comment + ": " if comment else "") + colour_names(vals[0])
        elif name == "default_features":
            vals = [v & 0xFFFFFFFF for v in vals]
            line = "%-22s = %s" % (name, "0x%08X" % vals[0])
        elif name.startswith("lamp"):
            vals = [v & 0xFFFFFFFF for v in vals]
            line = "%-22s = %s" % (name, " ".join("0x%08X" % v for v in vals))
            comment = (comment + ": " if comment else "") + lamp_names(vals[0])
        else:
            line = "%-22s = %s" % (name, " ".join(str(v) for v in vals))
        w("%-52s # @%d %s%s\n" % (line, off, {1: "byte", 2: "word", 4: "long"}[size_],
                                   ": " + comment if comment else ""))

    w("\n# Texts: text N @pointer max length = \"...\" (pointer relative to the\n"
      "# chip). A text may be shorter than max, not longer.\n")
    owned[BLOCK + TEXTS:BLOCK + TEXTS + 4 * NTEXTS] = b"\1" * (4 * NTEXTS)
    for i, p, s in _texts(chip):
        if 0 < p < CHIP:
            owned[p:p + len(s) + 1] = b"\1" * (len(s) + 1)
        w('text %3d @%04X max %2d = "%s"\n' % (i, p, len(s), text_encode(s)))

    msgs = _messages(chip)
    if msgs:
        w("\n# Messages after the last text, not in the text table: for the central\n"
          "# computer link (GAME.txt section 10); nothing is known to show them.\n")
        for p, s_, mx in msgs:
            owned[p:p + mx + 1] = b"\1" * (mx + 1)
            w('message @%04X max %2d = "%s"\n' % (p, mx, text_encode(s_)))

    lists, n = _sound_lists(chip)
    owned[BLOCK + SOUND_LISTS:BLOCK + SOUND_LISTS + 4 * n] = b"\1" * (4 * n)
    w("\n# Sounds (@%d): three lists of sound addresses (chip offsets). The game\n"
      "# plays sound N of the last list by number (named below). The first\n"
      "# two lists, groups A and B, are played one entry per call (sound\n"
      "# numbers 0x34 and 0x33, ROM 0xDFEE and 0xDF8E). Group A is the bass\n"
      "# line at the TUPLAATKO? prompt: 4 notes, 0.6 s each, looping (the 5th\n"
      "# entry is not played).\n" % SOUND_LISTS)
    for k, ptrs in enumerate(lists):
        w("sounds %d = %s\n" % (k, " ".join("%04X" % p for p in ptrs)))
    w("\n# A sound: steps of the 14 sound chip (AY-3-8910) registers: tone\n"
      "# periods A B C (low, high byte), noise period, mixer, volumes A B C\n"
      "# (16 or more = envelope), envelope period (low, high) and shape, and\n"
      "# how long the step lasts (time, 10 ms units). Then 'end', 'loop' (start\n"
      "# again) or 'repeat N' (play from the start N more times, then go on).\n")
    done = set()
    for ptrs in lists:
        for p in ptrs:
            if p in done:
                continue
            done.add(p)
            items, end = _sound_steps(chip, p)
            owned[p:end] = b"\1" * (end - p)
            num = ["%d: %s" % (i, SOUND_NAMES.get(i, "?")) for i, q in enumerate(lists[2]) if q == p]
            grp = ["group %s%s" % ("AB"[k], ": TUPLAATKO? bass line, one note per call" if k == 0 else "")
                   for k in (0, 1) if p in lists[k]]
            w("sound %04X%s\n" % (p, "   # " + "; ".join(grp + ["number " + x for x in num])
                                  if num or grp else ""))
            for it in items:
                w((_step_line(it[1], it[2]) if it[0] == "step" else
                   "  repeat %d" % it[1] if it[0] == "repeat" else "  " + it[0]) + "\n")

    start, count = _font(chip)
    owned[BLOCK + FONT_PTR:BLOCK + FONT_PTR + 4] = b"\1" * 4
    w("\nfont = %04X    # @%d: %d characters from code 20 (space)\n" % (start, FONT_PTR, count))
    w("# A character: 14 rows of 16 pixels (X = on, lowest bit on the left);\n"
      "# 'space' is used as 15 - space = the step to the next character.\n"
      "# Normal text can only use codes 20-7F; 7E-81 are the card suits.\n")
    for k in range(count):
        a = start + 32 * k
        rows = struct.unpack_from(">16H", chip, a)
        owned[a:a + 32] = b"\1" * 32
        extra = "  extra %d" % rows[15] if rows[15] else ""
        w("\nglyph %02X  space %d%s    # %s\n" % (0x20 + k, rows[14], extra, char_name(0x20 + k)))
        for r in rows[:GLYPH_ROWS]:
            w("  " + format(r, "016b")[::-1].replace("0", ".").replace("1", "X") + "\n")

    w("\n# Everything else, as hex (chip offset = bytes).\n")
    i = 0
    while i < CHIP:
        if owned[i]:
            i += 1
            continue
        j = i
        while j < CHIP and not owned[j] and chip[j] == chip[i]:
            j += 1
        if j - i >= 32:
            w("fill %04X-%04X = %02X\n" % (i, j - 1, chip[i]))
            i = j
            continue
        j = i
        while j < CHIP and not owned[j] and j - i < 32:
            k = j
            while k < CHIP and not owned[k] and chip[k] == chip[j]:
                k += 1
            if k - j >= 32 and j > i:
                break
            j += 1
        w("raw  %04X = %s\n" % (i, chip[i:j].hex()))
        i = j


def build(lines):
    chip = bytearray(CHIP)
    filled = bytearray(CHIP)
    fields = {name: (off, size, count) for name, off, size, count, _ in FIELDS}
    texts = []
    lists = {}
    owner = {}                         # sound/glyph bytes: who wrote them, to catch clashes
    sound_at = None                    # address of the next sound byte
    font_at = None
    built = None
    glyph = None                       # [address, space, extra, rows]

    def put(a, data, n):
        for i, b in enumerate(data):
            if a + i in owner and chip[a + i] != b:
                raise ValueError("byte %04X is also written by line %d, with another value"
                                 % (a + i, owner[a + i]))
            chip[a + i] = b
            filled[a + i] = 1
            owner.setdefault(a + i, n)

    for n, line in enumerate(lines, 1):
        code = (line.strip() if line.lstrip().startswith(("text", "built", "message"))
                else line.split("#", 1)[0].strip())
        if not code or code.startswith("#"):
            continue
        try:
            if glyph is not None:
                if not re.fullmatch(r"[.X]{16}", code):
                    raise ValueError("expected a row of 16 . or X (%d rows per character)" % GLYPH_ROWS)
                glyph[3].append(int(code[::-1].replace(".", "0").replace("X", "1"), 2))
                if len(glyph[3]) == GLYPH_ROWS:
                    put(glyph[0], struct.pack(">16H", *(glyph[3] + [glyph[1], glyph[2]])), n)
                    glyph = None
                continue
            word = code.split()[0]
            if word in ("step", "end", "loop", "repeat"):
                if sound_at is None:
                    raise ValueError("'%s' outside a sound" % word)
                if word == "step":
                    tok = [t for t in code.split()[1:] if t not in
                           ("tone", "noise", "mix", "vol", "env", "shape")]
                    if len(tok) != 16 or tok[14] != "time":
                        raise ValueError("a step has 14 register bytes and a time")
                    t = int(tok[15])
                    if not -128 <= t <= 127:
                        raise ValueError("time must be -128..127")
                    put(sound_at, bytes(int(x, 16) for x in tok[:14]) + bytes([t & 0xFF]), n)
                    sound_at += 15
                elif word == "repeat":
                    put(sound_at, bytes([2, int(code.split()[1])]), n)
                    sound_at += 2
                else:
                    put(sound_at, bytes([0 if word == "end" else 1]), n)
                    sound_at = None
                continue
            if word == "sounds":
                m = re.fullmatch(r"sounds\s+(\d)\s*=\s*([0-9A-Fa-f\s]*)", code)
                lists[int(m.group(1))] = [int(x, 16) for x in m.group(2).split()]
            elif word == "sound":
                sound_at = int(code.split()[1], 16)
            elif word == "version":
                put(BLOCK, version_bytes(code.split("=", 1)[1].strip()), n)
            elif word == "built":
                m = re.fullmatch(r'built\s+max\s+(\d+)\s*=\s*"(.*)"\s*(#.*)?', code)
                built = (int(m.group(1)), text_decode(m.group(2)), n)
            elif word == "font":
                font_at = int(code.split("=")[1], 16)
                put(BLOCK + FONT_PTR, struct.pack(">I", font_at), n)
            elif word == "glyph":
                m = re.fullmatch(r"glyph\s+([0-9A-Fa-f]{2})\s+space\s+(\d+)(\s+extra\s+(\d+))?", code)
                if font_at is None:
                    raise ValueError("glyph before the font line")
                glyph = [font_at + 32 * (int(m.group(1), 16) - 0x20), int(m.group(2)),
                         int(m.group(4) or 0), []]
            elif code.startswith("raw "):
                m = re.fullmatch(r"raw\s+([0-9A-Fa-f]+)\s*=\s*([0-9A-Fa-f]*)", code)
                a, data = int(m.group(1), 16), bytes.fromhex(m.group(2))
                chip[a:a + len(data)] = data
                filled[a:a + len(data)] = b"\1" * len(data)
            elif code.startswith("fill "):
                m = re.fullmatch(r"fill\s+([0-9A-Fa-f]+)-([0-9A-Fa-f]+)\s*=\s*([0-9A-Fa-f]{2})", code)
                a, b = int(m.group(1), 16), int(m.group(2), 16)
                chip[a:b + 1] = bytes([int(m.group(3), 16)]) * (b + 1 - a)
                filled[a:b + 1] = b"\1" * (b + 1 - a)
            elif code.startswith("message "):
                m = re.fullmatch(r'message\s+@([0-9A-Fa-f]+)\s+max\s+(\d+)\s*=\s*"(.*)"\s*(#.*)?', code)
                texts.append((None, int(m.group(1), 16), int(m.group(2)), text_decode(m.group(3)), n))
            elif code.startswith("text "):
                m = re.fullmatch(r'text\s+(\d+)\s+@([0-9A-Fa-f]+)\s+max\s+(\d+)\s*=\s*"(.*)"\s*(#.*)?', code)
                texts.append((int(m.group(1)), int(m.group(2), 16), int(m.group(3)),
                              text_decode(m.group(4)), n))
            else:
                name, vals = (x.strip() for x in code.split("=", 1))
                off, size, count = fields[name]
                vals = [int(v, 0) for v in vals.split()]
                if len(vals) != count:
                    raise ValueError("%s takes %d values" % (name, count))
                lo, hi = -(1 << (8 * size - 1)), (1 << (8 * size)) - 1
                for k, v in enumerate(vals):
                    if not lo <= v <= hi:
                        raise ValueError("%d doesn't fit in %d bytes" % (v, size))
                    a = BLOCK + off + size * k
                    chip[a:a + size] = (v & hi).to_bytes(size, "big")
                    filled[a:a + size] = b"\1" * size
        except (AttributeError, KeyError, ValueError, IndexError) as e:
            raise SystemExit("line %d: %s\n  %s" % (n, e or "can't read this line", line.rstrip()))
    if glyph is not None:
        raise SystemExit("the last character has fewer than %d rows" % GLYPH_ROWS)
    if built is not None:
        mx, text, n = built
        if len(text) > mx:
            raise SystemExit("line %d: built is %d characters, at most %d fit" % (n, len(text), mx))
        size = struct.unpack_from(">I", chip, 4)[0]
        put(size, text + b"\0" * (mx + 1 - len(text)), n)
    if sound_at is not None:
        raise SystemExit("the last sound has no end, loop or repeat")

    if lists:
        if sorted(lists) != [0, 1, 2]:
            raise SystemExit("need sounds 0, 1 and 2")
        a = BLOCK + SOUND_LISTS
        for k in range(3):
            for p in lists[k] + [0]:
                if filled[a]:
                    raise SystemExit("the sound lists are too long: they run into "
                                     "other settings at %04X" % a)
                struct.pack_into(">I", chip, a, p)
                filled[a:a + 4] = b"\1" * 4
                a += 4

    for i, p, mx, s, n in texts:
        if len(s) > mx:
            raise SystemExit("line %d: the text is %d characters, at most %d fit" % (n, len(s), mx))
        if i is None:                             # a message: no table entry
            continue
        struct.pack_into(">I", chip, BLOCK + TEXTS + 4 * i, p)
        filled[BLOCK + TEXTS + 4 * i:BLOCK + TEXTS + 4 * i + 4] = b"\1" * 4
    # write texts so that a shortened one is padded with zeros over its old room
    for i, p, mx, s, n in sorted(texts, key=lambda t: (t[1], -t[2])):
        if 0 < p < CHIP:
            chip[p:p + mx + 1] = s + b"\0" * (mx + 1 - len(s))
            filled[p:p + mx + 1] = b"\1" * (mx + 1)
    for i, p, mx, s, n in texts:                  # texts that share bytes must agree
        if 0 < p < CHIP and bytes(chip[p:chip.index(b"\0", p)]) != s:
            raise SystemExit("line %d: this text shares its bytes with another text "
                             "that was changed differently" % n)

    size = struct.unpack_from(">I", chip, 4)[0]
    filled[0x2E:0x30] = b"\1\1"
    filled[size - 3:size] = b"\1\1\1"
    missing = [i for i in range(size) if not filled[i]]
    if missing:
        raise SystemExit("no value for chip bytes %04X... (%d bytes); is a raw line missing?"
                         % (missing[0], len(missing)))
    fix_module(chip)
    return bytes(chip)


def main(argv):
    if len(argv) == 3 and argv[0] == "dump":
        with open(argv[1], "rb") as f:
            chip = f.read()
        with open(argv[2], "w", encoding="utf-8") as f:
            dump(chip, f)
    elif len(argv) == 3 and argv[0] == "build":
        with open(argv[1], encoding="utf-8") as f:
            chip = build(f.readlines())
        with open(argv[2], "wb") as f:
            f.write(chip)
        print("%s: module %r, CRC ok, chip sum %04X" % (argv[2], module_name(chip), sum(chip) & 0xFFFF))
    else:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
