"""python -m pyray [--rom SET|FILE] [--nvram FILE] [--fullscreen] [--list-roms]"""

import argparse
import os

from . import roms


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="RAY video poker emulator")
    ap.add_argument("--rom", default=None,
                    help="ROM set name (default %s) or a 256 KB ROM file" % roms.DEFAULT)
    ap.add_argument("--list-roms", action="store_true",
                    help="list the ROM sets and check their chip files")
    ap.add_argument("--nvram", default=None,
                    help="battery backed RAM image (default roms/<set name>.nvram)")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0, help="emulation speed factor")
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument("--ay-clock", type=int, default=1_000_000, help="AY-3-8910 clock in Hz")
    ap.add_argument("--float", type=int, default=1000, dest="float_coins",
                    help="coins put in the payout float on the first start and by F9 (0 = off)")
    ap.add_argument("--audio-latency", type=int, default=40, metavar="MS",
                    help="sound buffering in ms (default 40; raise it if the sound crackles)")
    ap.add_argument("--acrtc-clock", type=int, default=None,
                    help="graphics chip drawing clock in Hz (default 3000000, 0 = draw instantly)")
    args = ap.parse_args()

    if args.list_roms:
        roms.list_roms(here)
        return

    title, data, nvram, desc = roms.resolve(args.rom, here)
    nvram = args.nvram or nvram
    os.makedirs(os.path.dirname(os.path.abspath(nvram)), exist_ok=True)
    print("ROM:  ", title, "-", desc)
    print("NVRAM:", nvram)
    from .gui import App
    App(data, nvram, args.fullscreen, args.speed, sound=not args.no_sound,
        ay_clock=args.ay_clock, float_coins=args.float_coins,
        acrtc_clock=args.acrtc_clock, title=title, audio_latency=args.audio_latency).run()


if __name__ == "__main__":
    main()
