"""General Instrument AY-3-8910 sound generator, rendered with numpy.

Register writes are timestamped in CPU cycles; render() produces the
samples for one emulated frame, splitting it at every register change so
envelope retriggers and note changes land at the right moment.
"""

import numpy as np

# Logarithmic DAC levels (normalised), as measured on real chips
LEVELS = np.array([0.0, 0.0137, 0.0205, 0.0291, 0.0423, 0.0618, 0.0847, 0.1369,
                   0.1691, 0.2647, 0.3527, 0.4499, 0.5704, 0.6873, 0.8482, 1.0])


def _noise_table():
    """17-bit LFSR (taps 0 and 3) output sequence, one entry per noise step."""
    n = (1 << 17) - 1
    out = np.empty(n, dtype=np.uint8)
    r = 1
    for i in range(n):
        out[i] = r & 1
        bit = (r ^ (r >> 3)) & 1
        r = (r >> 1) | (bit << 16)
    return out


class AY8910:
    def __init__(self, clock=1_000_000, rate=44100, cpu_hz=8_000_000):
        self.clock = clock
        self.rate = rate
        self.cpu_hz = cpu_hz
        self.noise = _noise_table()
        self.reset()

    def reset(self):
        self.regs = [0] * 16
        self.tone_phase = [0.0, 0.0, 0.0]
        self.noise_pos = 0.0
        self.env_pos = 0.0
        self.events = []          # (cycle offset in frame, reg, value)
        self.carry = 0.0          # fractional samples carried between frames
        self.dc = 0.0

    def write(self, reg, value, cycle_offset):
        self.events.append((cycle_offset, reg & 15, value & 0xFF))

    # ------------------------------------------------------------------
    def _segment(self, n):
        if n <= 0:
            return np.zeros(0)
        r = self.regs
        t = np.arange(1, n + 1, dtype=np.float64)
        mixer = r[7]
        # noise
        np_ = (r[6] & 0x1F) or 1
        nstep = self.clock / (16.0 * np_ * self.rate)
        npos = self.noise_pos + t * nstep
        noise = self.noise[(npos.astype(np.int64)) % len(self.noise)]
        self.noise_pos = float(npos[-1] % len(self.noise))
        # envelope: 16 steps per cycle, each lasting 16 * EP clock cycles
        ep = (r[11] | (r[12] << 8)) or 1
        estep = self.clock / (16.0 * ep * self.rate)
        epos = self.env_pos + t * estep
        self.env_pos = float(min(epos[-1], 1e9))
        env = self._envelope(epos, r[13])
        out = np.zeros(n)
        for ch in range(3):
            tp = (r[ch * 2] | ((r[ch * 2 + 1] & 0x0F) << 8)) or 1
            step = self.clock / (16.0 * tp * self.rate)
            ph = self.tone_phase[ch] + t * step
            self.tone_phase[ch] = float(ph[-1] % 1.0)
            if step > 0.5:        # above Nyquist: the square averages out
                tone = np.ones(n, dtype=bool)
            else:
                tone = (ph % 1.0) < 0.5
            tone_on = tone | bool(mixer & (1 << ch))
            noise_on = noise.astype(bool) | bool(mixer & (8 << ch))
            gate = tone_on & noise_on
            vol = r[8 + ch]
            if vol & 0x10:
                amp = LEVELS[env]
            else:
                amp = LEVELS[vol & 15]
            out += gate * amp
        return out

    @staticmethod
    def _envelope(epos, shape):
        e = epos.astype(np.int64)
        cyc = e // 16
        s = e % 16
        att = bool(shape & 4)
        cont = bool(shape & 8)
        alt = bool(shape & 2)
        hold = bool(shape & 1)
        first = s if att else 15 - s
        if not cont:
            later = np.zeros_like(s)
        elif hold:
            end = 15 if att else 0
            later = np.full_like(s, 15 - end if alt else end)
        elif alt:
            up = (cyc % 2 == 0) == att
            later = np.where(up, s, 15 - s)
        else:
            later = first
        return np.where(cyc == 0, first, later).astype(np.int64)

    def render(self, frame_cycles):
        """Samples (float, about -1..1) for one frame of frame_cycles CPU cycles."""
        exact = frame_cycles * self.rate / self.cpu_hz + self.carry
        total = int(exact)
        self.carry = exact - total
        chunks = []
        done = 0
        for cyc, reg, val in sorted(self.events, key=lambda e: e[0]):
            at = min(total, int(cyc * self.rate / self.cpu_hz))
            if at > done:
                chunks.append(self._segment(at - done))
                done = at
            self.regs[reg] = val
            if reg == 13:
                self.env_pos = 0.0
        self.events = []
        if total > done:
            chunks.append(self._segment(total - done))
        out = np.concatenate(chunks) / 3.0 if chunks else np.zeros(0)
        if out.size:
            # slow DC removal so silence sits at zero (no clicks on start/stop)
            self.dc = 0.97 * self.dc + 0.03 * float(out.mean())
            out = out - self.dc
        return out
