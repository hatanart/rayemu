"""Turns HD63484 display registers + VRAM into a 608x292 picture.

The board stacks three horizontal screens (upper, base, lower) and puts a
window on top, all 4 bits per pixel, 152 words (608 pixels) wide.
Register usage follows what the game programs:
  R8C/R8A/R8E   heights of upper/base/lower screen (raster lines)
  RC2/RCA/RD2   memory width in words, RC4-6/RCC-E/RD4-6 start address
  R90-R96       window position and size, RDA-RDE window memory
  DCR (R06)     bits 15-8 enable the screens, bits 5-0 are the colour
                attenuation outputs used by the board.
"""

import numpy as np

WIDTH = 608
HEIGHT = 292

# Index -> RGB. The board's resistor DAC gives this order (from the 2010 emulator).
PALETTE = np.array([
    (0x00, 0x00, 0x00), (0x2D, 0x2D, 0x2D), (0xAA, 0x00, 0x00), (0xFF, 0x00, 0x00),
    (0x00, 0xAA, 0x00), (0x00, 0xFF, 0x00), (0xAA, 0xAA, 0x00), (0xFF, 0xFF, 0x00),
    (0x00, 0x00, 0xAA), (0x00, 0x00, 0xFF), (0xAA, 0x00, 0xAA), (0xFF, 0x00, 0xFF),
    (0x00, 0xAA, 0xAA), (0x00, 0xFF, 0xFF), (0xAA, 0xAA, 0xAA), (0xFF, 0xFF, 0xFF),
], dtype=np.uint8)


def palette_for(dcr):
    attr = (dcr & 0x3F) ^ 0xFF
    dim = np.array([1.0, 1.0, 1.0])
    if attr & 0x01: dim[0] += 2
    if attr & 0x02: dim[1] += 2
    if attr & 0x04: dim[2] += 2
    if attr & 0x08: dim[0] += 3
    if attr & 0x10: dim[1] += 3
    if attr & 0x20: dim[2] += 3
    return (PALETTE / dim).astype(np.uint8)


def _unpack(words):
    """uint16 words -> 4 pixels each, low nibble first."""
    out = np.empty(words.shape + (4,), dtype=np.uint8)
    out[..., 0] = words & 15
    out[..., 1] = (words >> 4) & 15
    out[..., 2] = (words >> 8) & 15
    out[..., 3] = (words >> 12) & 15
    return out.reshape(words.shape[:-1] + (words.shape[-1] * 4,))


def _sar(acrtc, base):
    return ((acrtc.reg(base) & 0x0F) << 16) | acrtc.reg(base + 2)


def render_indices(acrtc):
    vram = np.frombuffer(acrtc.vram, dtype=np.uint16)
    mask = len(vram) - 1
    img = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    dcr = acrtc.reg(0x06)
    y = 0
    # (height reg, mwr reg, sar reg, enable mask, enable value)
    for hreg, mwreg, sreg, en in ((0x8C, 0xC2, 0xC4, 0x3000),
                                   (0x8A, 0xCA, 0xCC, 0xC000),
                                   (0x8E, 0xD2, 0xD4, 0x0C00)):
        h = acrtc.reg(hreg) & 0xFFF
        mw = acrtc.reg(mwreg) & 0xFFF or 152
        h = min(h, HEIGHT - y)
        if h <= 0:
            continue
        if (dcr & en) == en:
            start = _sar(acrtc, sreg)
            ww = min(mw, WIDTH // 4)
            idx = (start + np.arange(h)[:, None] * mw + np.arange(ww)[None, :]) & mask
            img[y:y + h, :ww * 4] = _unpack(vram[idx])
        y += h

    # window
    if (dcr & 0x0300) == 0x0300:
        r92 = acrtc.reg(0x92)
        wx = (r92 >> 8) * 8 - ((acrtc.reg(0x84) >> 8) + 3) * 4
        wy = (acrtc.reg(0x94) & 0xFFF) - (acrtc.reg(0x88) >> 8)
        ww = (r92 & 0xFF) * 2 + 2
        wh = acrtc.reg(0x96) & 0xFF
        mw = acrtc.reg(0xDA) & 0xFFF or 152
        start = _sar(acrtc, 0xDC) + 2
        if wh > 0 and ww > 0:
            idx = (start + np.arange(wh)[:, None] * mw + np.arange(ww)[None, :]) & mask
            win = _unpack(vram[idx])
            x0, y0 = max(wx, 0), max(wy, 0)
            x1, y1 = min(wx + ww * 4, WIDTH), min(wy + wh, HEIGHT)
            if x1 > x0 and y1 > y0:
                img[y0:y1, x0:x1] = win[y0 - wy:y1 - wy, x0 - wx:x1 - wx]
    return img


def render_rgb(acrtc):
    return palette_for(acrtc.reg(0x06))[render_indices(acrtc)]


def vram_dump_rgb(acrtc, start=0, rows=600, width_words=152):
    vram = np.frombuffer(acrtc.vram, dtype=np.uint16)
    n = rows * width_words
    words = vram[start:start + n].reshape(rows, width_words)
    return PALETTE[_unpack(words)]
