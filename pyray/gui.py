"""pygame front end for the RAY video poker emulator."""

import os
import sys
import time

import numpy as np

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

from .display import HEIGHT, WIDTH, _unpack, palette_for, render_indices  # noqa: E402
from .hd63484 import VRAM_WORDS  # noqa: E402
from .machine import Machine  # noqa: E402
from .fairness import HAND_NAMES  # noqa: E402

SCALE = 2
PANEL_H = 140
BG = (24, 24, 28)
FG = (220, 220, 220)
DIM = (110, 110, 120)
AMBER = (235, 190, 110)
VRAM_ROWS = VRAM_WORDS // 152   # video memory as 608-pixel rows
AUDIO_CHUNK = 1024       # samples per chunk handed to the sound card (23 ms)

# Cabinet buttons: (label, key, pia, port, bit). All inputs are active low.
BUTTONS = [
    ("VALITSE 1", pygame.K_1, "pia2", "b", 5),
    ("VALITSE 2", pygame.K_2, "pia2", "b", 1),
    ("VALITSE 3", pygame.K_3, "pia2", "b", 0),
    ("VALITSE 4", pygame.K_4, "pia2", "a", 7),
    ("VALITSE 5", pygame.K_5, "pia2", "a", 6),
    ("TUPLAUS", pygame.K_d, "pia2", "a", 5),
    ("PIENI", pygame.K_LEFT, "pia2", "a", 4),
    ("SUURI", pygame.K_RIGHT, "pia2", "a", 3),
    ("PANOS", pygame.K_b, "pia2", "a", 2),
    ("VOITONMAKSU", pygame.K_p, "pia2", "a", 1),
    ("JAKO", pygame.K_RETURN, "pia2", "a", 0),
    ("HUOLTO", pygame.K_s, "pia2", "b", 2),
    ("SRVC1", pygame.K_F1, "pia2", "b", 3),
    ("SRVC2", pygame.K_F2, "pia2", "b", 4),
    ("K13", pygame.K_F3, "pia3", "a", 2),
    ("K14", pygame.K_F4, "pia3", "a", 3),
    ("K15", pygame.K_F5, "pia3", "a", 4),
    ("K16", pygame.K_F6, "pia3", "a", 5),
]
KEYMAP = {b[1]: b for b in BUTTONS}

HELP = [
    "1-5 hold   Enter deal   B bet   D double   Left/Right small/big   P collect   I coin",
    "S service   F1/F2 srvc   F3-F6 K13-16   F8 video memory   F9 refill   F10 door   F11 full   Esc quit",
]

# 7-segment: bit0 a (top), 1 b, 2 c, 3 d (bottom), 4 e, 5 f, 6 g (middle)
SEGMENTS = {
    0: ((4, 0), (16, 0)), 1: ((18, 2), (18, 18)), 2: ((18, 22), (18, 38)),
    3: ((4, 40), (16, 40)), 4: ((2, 22), (2, 38)), 5: ((2, 2), (2, 18)),
    6: ((4, 20), (16, 20)),
}


class App:
    def __init__(self, rom, nvram, fullscreen=False, speed=1.0, sound=True,
                 ay_clock=1_000_000, float_coins=1000, acrtc_clock=None, title=None,
                 audio_latency=40):
        try:
            pygame.mixer.pre_init(44100, -16, 1, 1024)
        except Exception:
            pass
        pygame.init()
        if title is None:
            title = os.path.splitext(os.path.basename(rom))[0] if isinstance(rom, str) else "RAY"
        pygame.display.set_caption("RAY - " + title)
        self.size = (WIDTH * SCALE, HEIGHT * SCALE + PANEL_H)
        flags = pygame.SCALED | (pygame.FULLSCREEN if fullscreen else 0)
        self.screen = pygame.display.set_mode(self.size, flags)
        self.font = pygame.font.SysFont("monospace", 14)
        # pick the biggest help font whose lines fit the window width
        for size in range(15, 8, -1):
            self.small = pygame.font.SysFont("monospace", size)
            if max(self.small.size(t)[0] for t in HELP) <= self.size[0] - 16:
                break
        self.rom, self.nvram, self.ay_clock = rom, nvram, ay_clock
        self.acrtc_clock = acrtc_clock
        self.float_coins = float_coins
        self.m = self.new_machine()
        self.audio_ok = False
        if sound:
            try:
                if pygame.mixer.get_init() is None:
                    pygame.mixer.init(44100, -16, 1, 1024)
                self.channel = pygame.mixer.Channel(0)
                self.audio_ok = True
                self.m.sound = True
            except Exception as e:
                print("sound disabled:", e)
        self.abuf = np.zeros(0)
        # sound waiting in the emulator, on top of ~2 chunks in pygame
        self.audio_target = max(AUDIO_CHUNK, int(44100 * audio_latency / 1000))
        self.volume = 0.5
        if nvram and not os.path.exists(nvram):
            self.first_run()
        self.door_open = False
        self.speed = speed
        self.status = ""
        self.status_until = 0
        self.held = set()
        self.surf8 = None
        self.vram_view = False
        self.level_ok = False
        self.vram_row = 0
        self.pal_dcr = None
        self.scaled = pygame.Surface((WIDTH * SCALE, HEIGHT * SCALE), depth=8)

    def message(self, text):
        self.screen.fill(BG)
        self.screen.blit(self.font.render(text, True, FG), (40, self.size[1] // 2))
        pygame.display.flip()
        pygame.event.pump()

    def first_run(self):
        """No battery RAM yet: let the game initialise its books, put the
        payout float in (like an attendant filling the machine) and restart."""
        m = self.m
        snd, m.sound = m.sound, False
        for i in range(1700):
            m.run_frame()
            if i % 50 == 0:
                self.message("First start: initialising battery RAM ... %d%%" % (i * 100 // 1700))
        m.ensure_float(self.float_coins)
        m.power_cycle()
        m.save_nvram()
        m.sound = snd
        self.flash("float filled with %d coins" % self.float_coins)

    def check_hopper_level(self):
        """Once the game is up: machines made before the hopper level was set
        (TESTI 1, 0 = none) get it now, or the game keeps unloading the float
        to the cash box."""
        m = self.m
        if m.frame > 1500 and m.books_valid() and m.book_read(m.CLOSURE, 2) & 0xFF == 0xFF:
            if self.float_coins:
                m.set_hopper_level(self.float_coins)
            self.level_ok = True

    def refill(self):
        added = self.m.ensure_float(self.float_coins)
        self.m.power_cycle()
        self.flash("refilled (+%d) and restarted" % added)

    def new_machine(self):
        kw = {} if self.acrtc_clock is None else {"acrtc_clock": self.acrtc_clock}
        return Machine(self.rom, nvram_path=self.nvram, ay_clock=self.ay_clock, **kw)

    def pump_audio(self):
        """Feed the sound card in fixed chunks. The emulator makes sound at
        exactly 44100 samples per emulated second, the card plays at its own
        clock, so the backlog drifts. Instead of dropping sound when it gets
        too long (an audible skip), each chunk is stretched or squeezed by
        up to 0.5 % to keep the backlog near audio_target."""
        m = self.m
        if not self.audio_ok:
            m.audio.clear()
            return
        if m.audio:
            self.abuf = np.concatenate([self.abuf] + m.audio)
            m.audio.clear()
        if self.abuf.size > 44100:                  # stalled for a second: start over
            self.abuf = self.abuf[-self.audio_target:]
        while self.channel.get_queue() is None:
            err = (self.abuf.size - self.audio_target) / self.audio_target
            ratio = 1.0 + max(-0.005, min(0.005, err * 0.005))
            need = int(AUDIO_CHUNK * ratio) + 1
            if self.abuf.size < need:
                break
            src = self.abuf[:need]
            pos = np.arange(AUDIO_CHUNK) * ratio
            out = np.interp(pos, np.arange(need), src)
            self.abuf = self.abuf[int(AUDIO_CHUNK * ratio):]
            chunk = (np.clip(out * self.volume, -1, 1) * 32767).astype(np.int16)
            snd = pygame.sndarray.make_sound(chunk)
            if self.channel.get_busy():
                self.channel.queue(snd)
            else:
                self.channel.play(snd)

    # ------------------------------------------------------------------
    def set_button(self, b, pressed):
        _, _, pia, port, bit = b
        getattr(self.m, pia).set_input(port, bit, 0 if pressed else 1)
        if pressed:
            self.held.add(b[0])
        else:
            self.held.discard(b[0])

    def toggle_door(self):
        self.door_open = not self.door_open
        self.m.pia2.set_input("b", 6, 0 if self.door_open else 1)
        self.flash("door " + ("OPEN" if self.door_open else "closed"))

    def flash(self, text):
        self.status = text
        self.status_until = time.time() + 2

    # ------------------------------------------------------------------
    def draw_7seg(self, x, y, bits):
        on, off = (255, 40, 30), (55, 20, 20)
        for seg, (p0, p1) in SEGMENTS.items():
            c = on if bits & (1 << seg) else off
            pygame.draw.line(self.screen, c, (x + p0[0], y + p0[1]), (x + p1[0], y + p1[1]), 4)

    def draw_panel(self):
        y0 = HEIGHT * SCALE
        pygame.draw.rect(self.screen, BG, (0, y0, self.size[0], PANEL_H))
        self.draw_7seg(12, y0 + 12, self.m.sevenseg)
        lamps = self.m.ls.lamps
        for i in range(24):
            c = (255, 210, 60) if lamps & (1 << i) else (60, 55, 40)
            pygame.draw.circle(self.screen, c, (60 + i * 14, y0 + 20), 5)
        txt = "door %s   coin unit: hopper %d  paid %d" % (
            "OPEN" if self.door_open else "closed", self.m.coin.hopper_coins, self.m.coin.paid)
        pay, box = self.m.coin.busy()
        if pay:
            txt += "   PAYING OUT: %d to go" % pay
        if box:
            txt += "   hopper -> cash box: %d to go" % box
        faults = self.m.fault_codes()
        if faults:
            txt += "   board faults: " + " ".join(faults)
        self.screen.blit(self.font.render(txt, True, (255, 120, 90) if faults else FG), (60, y0 + 36))
        if self.held:
            self.screen.blit(self.font.render(" ".join(sorted(self.held)), True, (120, 200, 255)),
                             (960, y0 + 36))
        if self.vram_view:
            self.screen.blit(self.font.render(
                "VIDEO MEMORY rows %d-%d of %d  (PgUp/PgDn, wheel, Home/End; F8 back)" % (
                    self.vram_row, self.vram_row + HEIGHT - 1, VRAM_ROWS), True, (120, 255, 120)),
                (60, y0 + 56))
        if time.time() < self.status_until:
            self.screen.blit(self.font.render(self.status, True, (120, 255, 120)), (720, y0 + 36))
        st = None if self.vram_view else self.m.fair.status()
        if st:
            pay = "-" if st["payout"] is None else "%.1f %%" % st["payout"]
            a = "payout %s (game aims for 80-90 %%)   correction %+d   bet %d, %s" % (
                pay, st["correction"], st["bet"], "53 cards with joker" if st["joker"] else "52 cards")
            b = "chance a winning draw is taken back:  " + "  ".join(
                "%s %d %%" % (n, round(c * 100)) for n, c in zip(HAND_NAMES, st["chances"]))
            b += "      draws %d, wins taken back %d" % (st["draws"], st["taken_back"])
            self.screen.blit(self.font.render(a, True, AMBER), (60, y0 + 56))
            self.screen.blit(self.font.render(b, True, AMBER), (60, y0 + 74))
        lh = self.small.get_linesize()
        for i, t in enumerate(HELP):
            self.screen.blit(self.small.render(t, True, DIM), (8, y0 + PANEL_H - 6 - lh * (len(HELP) - i)))

    def vram_indices(self):
        """A screen-sized window into the whole video memory (F8)."""
        vram = np.frombuffer(self.m.acrtc.vram, dtype=np.uint16)
        rows = (self.vram_row + np.arange(HEIGHT)) % VRAM_ROWS
        words = vram[(rows[:, None] * 152 + np.arange(152)[None, :]) % len(vram)]
        return _unpack(words)

    def scroll_vram(self, rows):
        self.vram_row = max(0, min(VRAM_ROWS - HEIGHT, self.vram_row + rows))

    def draw(self):
        if self.vram_view:
            idx = self.vram_indices()
            dcr = 0x3F                     # show the memory undimmed
        else:
            idx = render_indices(self.m.acrtc)
            dcr = self.m.acrtc.reg(0x06) & 0x3F
        if self.surf8 is None:
            self.surf8 = pygame.Surface((WIDTH, HEIGHT), depth=8)
        if dcr != self.pal_dcr:
            pal = [tuple(int(v) for v in c) for c in palette_for(dcr)] + [(0, 0, 0)] * 240
            self.surf8.set_palette(pal)
            self.scaled.set_palette(pal)
            self.pal_dcr = dcr
        pygame.surfarray.blit_array(self.surf8, idx.T)
        pygame.transform.scale(self.surf8, (WIDTH * SCALE, HEIGHT * SCALE), self.scaled)
        self.screen.blit(self.scaled, (0, 0))
        self.draw_panel()
        pygame.display.flip()

    # ------------------------------------------------------------------
    def shutdown(self):
        self.flash("power down...")
        self.m.power_fail()
        for _ in range(30):
            self.m.run_frame()
        self.m.save_nvram()

    def run(self):
        clock_start = time.perf_counter()
        frames_done = 0
        running = True
        self.quit_requested = False
        try:
            import signal
            signal.signal(signal.SIGINT, lambda *a: setattr(self, "quit_requested", True))
            signal.signal(signal.SIGTERM, lambda *a: setattr(self, "quit_requested", True))
        except (ValueError, OSError):
            pass
        while running:
            if self.quit_requested:
                running = False
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.MOUSEWHEEL and self.vram_view:
                    self.scroll_vram(-24 * ev.y)
                elif ev.type in (pygame.KEYDOWN, pygame.KEYUP):
                    down = ev.type == pygame.KEYDOWN
                    if ev.key in KEYMAP:
                        self.set_button(KEYMAP[ev.key], down)
                    elif down and ev.key == pygame.K_ESCAPE:
                        running = False
                    elif down and ev.key == pygame.K_i:
                        self.m.coin.insert_coin(1)
                        self.flash("coin inserted")
                    elif down and ev.key == pygame.K_F10:
                        self.toggle_door()
                    elif down and ev.key == pygame.K_F9:
                        self.refill()
                    elif down and ev.key == pygame.K_F8:
                        self.vram_view = not self.vram_view
                    elif down and self.vram_view and ev.key == pygame.K_PAGEDOWN:
                        self.scroll_vram(HEIGHT // 2)
                    elif down and self.vram_view and ev.key == pygame.K_PAGEUP:
                        self.scroll_vram(-HEIGHT // 2)
                    elif down and self.vram_view and ev.key == pygame.K_HOME:
                        self.vram_row = 0
                    elif down and self.vram_view and ev.key == pygame.K_END:
                        self.scroll_vram(VRAM_ROWS)
                    elif down and ev.key == pygame.K_F11:
                        pygame.display.toggle_fullscreen()
            # keep emulated time in step with wall time (100 machine frames/s)
            target = int((time.perf_counter() - clock_start) * 100 * self.speed)
            if target - frames_done > 20:          # too slow: don't try to catch up
                frames_done = target - 2
            ran = 0
            while frames_done < target and ran < 4:
                self.m.run_frame()
                frames_done += 1
                if not self.level_ok and self.m.frame % 100 == 0:
                    self.check_hopper_level()
                ran += 1
            self.pump_audio()
            self.draw()
            if frames_done >= target:
                time.sleep(0.005)
        self.shutdown()
        # The battery RAM is saved. Leave without pygame.quit(): closing a
        # stuck sound device can hang, and then the window won't close.
        sys.stdout.flush()
        os._exit(0)
