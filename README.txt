RAY video poker emulator - test version
=======================================

An emulator of the Finnish RAY (Raha-automaattiyhdistys) "POKERI" video
poker machine from 1990 (program 6457/6458-7.7 with game parameters
8460-1.1 from October 1994). The 68000 CPU
runs on the Musashi core (Python package machine68k); the rest of the
board (graphics chip, sound chip, I/O chips, coin processor) is written
in Python.

ROMs
----
The ROM files are RAY's (now Veikkaus) and are not part of the source
repository. Put the four EPROM images into roms/ with these names:

    6457-7.7_ic30.bin   6458-7.7_ic38.bin   6458-7.7_ic34.bin   8460-1.1_ic43.bin

(64 KB each; see roms/pokeri-8460-1.1.ray). ./run.sh --list-roms checks
them against the checksums printed on the chip labels.

The 200 MK version (top win 200 instead of 100) is a second set: the same
three program chips plus its own IC43, 7907-2.0_ic43.bin. Start it with
./run.sh --rom pokeri-7907-2.0 (run.bat --rom pokeri-7907-2.0).

Starting
--------
Needs Python 3.9 or newer.

  Linux / macOS / Raspberry Pi:   ./run.sh
  Windows:                        run.bat

The first start installs the Python packages (pygame, numpy, machine68k)
into a local .venv folder, which needs internet. machine68k is compiled
from source, so you need a C compiler:
  Debian/Ubuntu/Raspberry Pi OS:  sudo apt install build-essential python3-dev python3-venv
  macOS:                          xcode-select --install
  Windows:                        "Microsoft C++ Build Tools"

The first game start also takes about 5 seconds longer: the emulator
boots the machine once, puts 1000 coins (1000 mk) in its payout reserve (like an
attendant filling the machine), turns on two settings from TESTI 14
(win music, and no dimming of the attract screen) and restarts it. The machine's battery
backed memory is then kept in roms/pokeri-8460-1.1.nvram.
Delete that file to start over.

Playing
-------
  I              insert a coin (1 mk)
  B              PANOS (bet)
  Enter          JAKO (deal / draw)
  1-5            VALITSE (hold cards 1-5)
  D              TUPLAUS (double up)
  Left / Right   PIENI / SUURI (small / big, when doubling)
  P              VOITONMAKSU (collect winnings)

Service:
  F10            door open/closed ("OVI AUKI" when open)
  S              HUOLTO (service)
  F1 / F2        SRVC1 / SRVC2
  F3-F6          K13-K16 (K16 opens the test menu with the door open)
  F8             look at the whole video memory (PgUp/PgDn, mouse wheel,
                 Home/End to move; F8 again to go back): the card faces,
                 pay tables and texts the game draws off screen
  F9             refill the payout reserve to 1000 coins and restart (use it
                 when the machine closes itself with P3 84 or P3 89)

  F11            fullscreen
  Esc            quit (the machine is told the mains went off, so it
                 saves its state like the real one does)

Leave it idle for about 80 seconds and the attract demo starts
("POKERI (c) RAY 1990" with bouncing cards).

Under the game picture are the board's 7-segment service display, the
lamp outputs and the coin unit's state, and two lines about the game's
payout controller (read from the game's memory):
  payout       the machine's running payout percentage, as the game
               computes it; the game aims for 80-90 %
  correction   how the game adjusts itself: below 80 % it gets more
               generous (down to -13), above 90 % stricter (up to +13)
  chance a winning draw is taken back
               after the draw, the game may secretly replace a freshly
               drawn card that made two pairs, three of a kind or (from
               bet 3) a straight, before it is shown. These are the odds
               for the current bet and correction.
  draws / wins taken back   counted in this session
From bet 3 a joker is added to the deck (53 cards).

Options
-------
  --fullscreen      start in fullscreen
  --no-sound        no sound
  --speed X         emulation speed factor (default 1.0)
  --float N         coins put in the payout reserve on the first start and
                    by F9 (default 1000, 0 = off)
  --ay-clock HZ     sound chip clock (default 1000000)
  --audio-latency MS  sound buffering (default 40); raise it, e.g. to 80,
                    if the sound crackles on a slow computer
  --acrtc-clock HZ  graphics chip drawing speed (default 3000000; higher =
                    faster animations, 0 = draw instantly)
  --rom SET|FILE    ROM set to run (default pokeri-8460-1.1, from its .ray
                    file in roms/), or one joined
                    256 KB ROM file
  --list-roms       list the ROM sets and check each chip file
  --nvram FILE      battery-backed RAM file (default: ROM name + .nvram)

For example: ./run.sh --fullscreen

Speed: about 60% of a 1.5 GHz Intel Pentium J4205 core. A Raspberry
Pi 4 is borderline, a Pi 5 is fine.

Known gaps
----------
- The graphics chip speed (3 MHz) is an estimate. If animations look too
  slow or too fast compared to a real machine, try --acrtc-clock.
- The MC68681 DUART is not emulated (the game runs without it).
- The hopper count test (TESTI 11) shows 99 after paying 100.
- K13-K15, SRVC1/2 and some PIA3 inputs are not fully understood.

Other ROM versions
------------------
The board has four 64 KB EPROMs: three hold the program, the fourth
(IC43) the game's parameters (pay table, limits, texts), so versions with
other prizes differ only in IC43. Each chip is one file in roms/, named after
its label and socket (e.g. 6458-7.7_ic38.bin). A ROM set is a text file
roms/<set name>.ray listing the chips (address, socket, file, label sum);
see roms/pokeri-8460-1.1.ray. To add a version, put its chip files in
roms/ and write a .ray file; then start it with --rom <set name>.
--list-roms checks each chip file against the sum printed on its label
(see HARDWARE.txt, "Program ROM"). A single joined 256 KB file also runs:
--rom FILE. Each set keeps its own NVRAM, roms/<set name>.nvram.

TESTI 2 "AUTOMAATIN TIEDOT" (door open, K16, then P twice) shows the game
and system numbers and versions.

Making your own IC43
--------------------
pyray/paramrom.py turns an IC43 into a settings file you can edit (pay
table, limits, the payout controller's tables, all screen texts, the
sounds as sound chip steps, and the font as pictures of . and X) and
back into a chip. After the first ./run.sh the Python is in .venv:

  .venv/bin/python -m pyray.paramrom dump roms/8460-1.1_ic43.bin my.txt
  (edit my.txt)
  .venv/bin/python -m pyray.paramrom build my.txt roms/my_ic43.bin

(on Windows .venv\Scripts\python instead of .venv/bin/python). Then copy
roms/pokeri-8460-1.1.ray to roms/my-poker.ray, put my_ic43.bin on its
IC43 line, change its title: line, and start it with
./run.sh --rom my-poker. build fixes the
module's checksum; without that the game doesn't start. A text may get
shorter, not longer. Name your sets so nobody takes them for RAY's,
and change the version and built lines at the top: TESTI 2 shows them
(e.g. version = 9999-0.1 and
built max 29 = "OMA PIIRI 30.10.2026\n").

params/ has both chips already dumped (pokeri-8460-1.1.txt and
pokeri-7907-2.0.txt), to read or to start from.

For example, a machine that never takes a win back (GAME.txt section 5):
set all takeback.bet lines and correction_high to 0.

HARDWARE.txt describes the board as far as it is known, and GAME.txt
the game: rules, dealing, the payout controller, settings and bookkeeping.
