"""
CTCSS (Continuous Tone-Coded Squelch System) encode and decode.

Encode  — generate a continuous sine wave at the PL frequency, suitable
          for mixing into the TX audio path at a configurable level.

Decode  — Goertzel algorithm operating on fixed-size windows of RX audio.
          Returns True when the target frequency is present above threshold
          for the configured integration window.

STE     — Squelch Tail Elimination: appended to the end of a transmission
          before the carrier is dropped.
            reverse_burst  — 120° Motorola phase shift for N ms then silence
            chicken_burst  — stop CTCSS N ms before dropping the carrier

All functions work with float32 numpy arrays at a configurable sample rate
(default 16 000 Hz, matching the audio engine).
"""

from __future__ import annotations
import math
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Standard EIA/TIA-603 CTCSS tones (Hz)  — 38 tones, 67.0–250.3 Hz
# ─────────────────────────────────────────────────────────────────────────────

TONES: list[float] = [
     67.0,  71.9,  74.4,  77.0,  79.7,  82.5,  85.4,  88.5,
     91.5,  94.8,  97.4, 100.0, 103.5, 107.2, 110.9, 114.8,
    118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4,
    156.7, 162.2, 167.9, 173.8, 179.9, 186.2, 192.8, 203.5,
    210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
]

SAMPLE_RATE = 16_000   # Hz — matches audio engine default


# ─────────────────────────────────────────────────────────────────────────────
# Encode
# ─────────────────────────────────────────────────────────────────────────────

class CTCSSEncoder:
    """
    Stateful CTCSS tone encoder.  Call generate() to get the next block of
    samples; the phase is preserved between calls so the tone is seamlessly
    continuous across block boundaries.

    level is the fraction of full scale (e.g. 0.15 = 15 % of peak).
    """

    def __init__(self, freq: float, level: float = 0.15,
                 sample_rate: int = SAMPLE_RATE):
        self.freq        = freq
        self.level       = level
        self.sample_rate = sample_rate
        self._phase      = 0.0            # radians, preserved across blocks
        self._phase_inc  = 2 * math.pi * freq / sample_rate

    def generate(self, n_samples: int) -> np.ndarray:
        """Return n_samples of the CTCSS tone as float32."""
        phases = self._phase + np.arange(n_samples, dtype=np.float64) * self._phase_inc
        self._phase = float(phases[-1] + self._phase_inc) % (2 * math.pi)
        return (np.sin(phases) * self.level).astype(np.float32)

    def reverse_burst(self, duration_ms: int) -> np.ndarray:
        """
        Generate a 120° Motorola reverse-burst segment.
        The phase is shifted by 120° (2π/3 rad) at the start of the burst
        then returned to normal (the carrier is dropped immediately after).
        """
        n = int(duration_ms / 1000 * self.sample_rate)
        shift = 2 * math.pi / 3              # 120° in radians
        phases = (self._phase + shift
                  + np.arange(n, dtype=np.float64) * self._phase_inc)
        self._phase = float(phases[-1] + self._phase_inc) % (2 * math.pi)
        return (np.sin(phases) * self.level).astype(np.float32)

    def reset(self) -> None:
        """Reset phase to zero (use when the encoder is switched off/on)."""
        self._phase = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Decode — Goertzel algorithm
# ─────────────────────────────────────────────────────────────────────────────

def _goertzel_power(samples: np.ndarray, freq: float,
                    sample_rate: int = SAMPLE_RATE) -> float:
    """
    Compute the normalised power for `freq` over the sample block using FFT.

    Returns a value in [0.0, 1.0] representing the fraction of total signal
    power concentrated at the target frequency.  Values above a threshold
    (typically 0.015–0.05 depending on signal quality) indicate tone presence.

    Uses numpy.fft.rfft and extracts the nearest bin — fast enough on a Pi 3
    and avoids the per-sample Python loop of the original Goertzel approach.
    """
    n = len(samples)
    if n == 0:
        return 0.0

    s64 = samples.astype(np.float64)
    energy = float(np.dot(s64, s64))
    if energy <= 0.0:
        return 0.0

    spectrum = np.fft.rfft(s64)
    # Find the FFT bin closest to the target frequency
    bin_index = int(round(freq * n / sample_rate))
    bin_index = min(bin_index, len(spectrum) - 1)

    # Power at the target bin: |X[k]|^2
    power = float(abs(spectrum[bin_index]) ** 2)

    # Normalise to match the original Goertzel scaling:
    # For N samples of a pure tone at the target freq, FFT gives |X[k]| = A*N/2,
    # so |X[k]|^2 = A^2 * N^2 / 4.  Signal energy = A^2 * N / 2.
    # Dividing |X[k]|^2 by (energy * N / 2) yields 1.0 for a pure tone.
    return power * 2.0 / (energy * n)


class CTCSSDecoder:
    """
    Streaming CTCSS tone decoder.

    Feed audio blocks with push(samples).  The decoder accumulates samples
    into a window of decode_time_ms ms; at the end of each window it runs
    Goertzel and updates the internal tone-present state.

    detected() returns True when the tone has been confirmed present.
    lost()     returns True when the tone was previously present but is gone.

    Both detected() and lost() are edge-triggered: they return True only
    once per transition (use poll() for continuous state).

    decode_hold_ms: hysteresis — once the tone is confirmed, it must be
    absent for this many ms before lost() fires.  This prevents false drops
    on fading signals (analogous to hardware IC behaviour).
    """

    def __init__(self, freq: float,
                 decode_time_ms: int  = 250,
                 decode_threshold: float = 0.015,
                 decode_hold_ms: int  = 500,
                 sample_rate: int     = SAMPLE_RATE):
        self.freq             = freq
        self.decode_time_ms   = decode_time_ms
        self.decode_threshold = decode_threshold
        self.decode_hold_ms   = decode_hold_ms
        self.sample_rate      = sample_rate

        self._window_size = int(decode_time_ms / 1000 * sample_rate)
        self._hold_size   = int(decode_hold_ms  / 1000 * sample_rate)

        self._buf: list[np.ndarray] = []   # list of numpy chunks
        self._buf_len: int = 0               # total samples across chunks
        self._present   = False          # current confirmed state
        self._hold_left = 0              # samples remaining in hold period
        self._detected  = False          # pending edge flag
        self._lost      = False          # pending edge flag

    # ── public API ───────────────────────────────────────────────────────────

    def push(self, samples: np.ndarray) -> None:
        """Feed a block of audio samples into the decoder."""
        self._buf.append(samples)
        self._buf_len += len(samples)
        while self._buf_len >= self._window_size:
            combined = np.concatenate(self._buf)
            window = combined[:self._window_size]
            leftover = combined[self._window_size:]
            if len(leftover) > 0:
                self._buf = [leftover]
            else:
                self._buf = []
            self._buf_len = len(leftover)
            self._process(window)

    def detected(self) -> bool:
        """Consume and return the tone-detected edge flag."""
        v, self._detected = self._detected, False
        return v

    def lost(self) -> bool:
        """Consume and return the tone-lost edge flag."""
        v, self._lost = self._lost, False
        return v

    def present(self) -> bool:
        """Return the current confirmed tone state (level-sensitive)."""
        return self._present

    def reset(self) -> None:
        self._buf.clear()
        self._buf_len  = 0
        was_present    = self._present
        self._present  = False
        self._hold_left = 0
        self._detected  = False
        self._lost      = was_present   # fire lost edge if we were present

    # ── internal ─────────────────────────────────────────────────────────────

    def _process(self, window: np.ndarray) -> None:
        power = _goertzel_power(window, self.freq, self.sample_rate)
        tone_in_window = power >= self.decode_threshold

        if tone_in_window:
            self._hold_left = self._hold_size
            if not self._present:
                self._present  = True
                self._detected = True
        else:
            if self._hold_left > 0:
                self._hold_left = max(0, self._hold_left - self._window_size)
            elif self._present:
                self._present = False
                self._lost    = True


# ─────────────────────────────────────────────────────────────────────────────
# STE helpers (standalone renderers — for pre-buffering before drop)
# ─────────────────────────────────────────────────────────────────────────────

def render_reverse_burst(freq: float, duration_ms: int, level: float = 0.15,
                         sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """
    Return a standalone 120° reverse-burst segment at full phase coherence.
    The burst starts at phase 0 with the 120° offset applied, simulating
    an in-transmission phase shift.  Use the CTCSSEncoder.reverse_burst()
    method instead when phase continuity with the preceding tone matters.
    """
    n = int(duration_ms / 1000 * sample_rate)
    shift = 2 * math.pi / 3
    t = np.arange(n, dtype=np.float64) * (2 * math.pi * freq / sample_rate) + shift
    return (np.sin(t) * level).astype(np.float32)


def render_chicken_burst(freq: float, lead_ms: int, level: float = 0.15,
                         sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """
    Return `lead_ms` ms of normal CTCSS tone (phase starts at 0).
    The caller is responsible for scheduling this so it ends lead_ms before
    the carrier is dropped — the actual silence comes from simply not
    generating further tone samples.
    """
    n = int(lead_ms / 1000 * sample_rate)
    t = np.arange(n, dtype=np.float64) * (2 * math.pi * freq / sample_rate)
    return (np.sin(t) * level).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def nearest_tone(freq: float) -> float:
    """Return the standard CTCSS tone frequency closest to freq."""
    return min(TONES, key=lambda t: abs(t - freq))


def tone_info(freq: float, decode_time_ms: int = 250,
              sample_rate: int = SAMPLE_RATE) -> str:
    """Return a human-readable description of a tone and its decode parameters."""
    window_size = int(decode_time_ms / 1000 * sample_rate)
    resolution  = sample_rate / window_size   # frequency resolution (Hz/bin)
    nearest     = nearest_tone(freq)
    return (f"{freq:.1f} Hz  |  window {decode_time_ms} ms ({window_size} samples)  "
            f"|  freq resolution {resolution:.2f} Hz/bin  "
            f"|  nearest standard tone: {nearest:.1f} Hz")


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sr   = SAMPLE_RATE
    freq = 100.0     # 100.0 Hz standard tone

    print(f"CTCSS self-test at {sr} Hz sample rate")
    print(f"Target: {tone_info(freq, decode_time_ms=250, sample_rate=sr)}")
    print()

    # Build a 1-second block with CTCSS present
    encoder = CTCSSEncoder(freq, level=0.15, sample_rate=sr)
    tone_block  = encoder.generate(sr)           # 1 second of tone
    noise_block = (np.random.randn(sr) * 0.05).astype(np.float32)  # background noise
    signal      = tone_block + noise_block

    decoder = CTCSSDecoder(freq, decode_time_ms=250, decode_threshold=0.015,
                           decode_hold_ms=500, sample_rate=sr)

    # Feed tone
    BLOCK = 800   # ~50 ms blocks
    detections = 0
    for i in range(0, len(signal), BLOCK):
        decoder.push(signal[i:i + BLOCK])
        if decoder.detected():
            detections += 1
            print(f"  → CTCSS detected  (sample {i})")

    print(f"  Present after tone: {decoder.present()}")

    # Feed silence
    for i in range(0, sr, BLOCK):
        decoder.push(np.zeros(BLOCK, dtype=np.float32))
        if decoder.lost():
            print(f"  → CTCSS lost  (sample {i})")

    print(f"  Present after silence: {decoder.present()}")

    # Reverse burst test
    burst = render_reverse_burst(freq, duration_ms=250, level=0.15, sample_rate=sr)
    print(f"\nReverse burst: {len(burst)} samples  "
          f"({len(burst)/sr*1000:.0f} ms)  "
          f"peak={np.max(np.abs(burst)):.3f}")

    # Encode round-trip: generate → power check
    blk = encoder.generate(int(0.25 * sr))
    pwr = _goertzel_power(blk, freq, sr)
    print(f"Goertzel power of clean tone block: {pwr:.4f}  "
          f"(threshold 0.015 → {'PASS' if pwr >= 0.015 else 'FAIL'})")

    print("\nSelf-test complete.")
