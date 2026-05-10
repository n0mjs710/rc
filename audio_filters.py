"""
FM audio filters for RX and TX signal paths.

Three filters, all designed at init and applied streaming via lfilter with
preserved state (zi) between calls so there are no discontinuities at block
boundaries.

  HPF300      — 2nd-order Butterworth high-pass at 300 Hz.
                Removes sub-audible energy (CTCSS tones, mains hum) from RX.

  DeEmphasis  — FM de-emphasis: H(s) = 1/(τs+1), τ=75 µs (NA/Japan standard).
                Compensates for the pre-emphasis applied at the transmitting
                radio. Apply to RX audio before passthrough/decode.

  PreEmphasis — FM pre-emphasis: H(s) = (τ₁s+1)/(τ₂s+1), τ₁=75 µs, τ₂=7.5 µs.
                Boosts highs before TX so the listener's de-emphasis restores
                a flat response end-to-end. Apply to TX mix before output.

All three are safe to call from the sounddevice audio callback.
"""

from __future__ import annotations
import numpy as np
from scipy.signal import butter, bilinear, lfilter


class HPF300:
    """300 Hz 2nd-order Butterworth high-pass filter."""

    def __init__(self, sample_rate: int = 16_000) -> None:
        nyq = sample_rate / 2.0
        self._b, self._a = butter(2, 300.0 / nyq, btype="high")
        self._zi = np.zeros(max(len(self._b), len(self._a)) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = lfilter(self._b, self._a, x, zi=self._zi)
        return y.astype(np.float32)


class DeEmphasis:
    """
    FM de-emphasis first-order IIR: H(s) = 1/(τs+1), τ=75 µs.
    Rolls off 6 dB/octave above fc = 1/(2πτ) ≈ 2.1 kHz.
    """

    def __init__(self, sample_rate: int = 16_000, tau: float = 75e-6) -> None:
        # Analogue: b=[1], a=[τ, 1]  — bilinear maps to digital coefficients
        self._b, self._a = bilinear([1.0], [tau, 1.0], fs=sample_rate)
        self._zi = np.zeros(max(len(self._b), len(self._a)) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = lfilter(self._b, self._a, x, zi=self._zi)
        return y.astype(np.float32)


class PreEmphasis:
    """
    FM pre-emphasis first-order IIR: H(s) = (τ₁s+1)/(τ₂s+1).
    τ₁=75 µs, τ₂=7.5 µs — 6 dB/octave boost above ≈2.1 kHz, flat above ≈21 kHz.
    """

    def __init__(self, sample_rate: int = 16_000,
                 tau1: float = 75e-6, tau2: float = 7.5e-6) -> None:
        self._b, self._a = bilinear([tau1, 1.0], [tau2, 1.0], fs=sample_rate)
        self._zi = np.zeros(max(len(self._b), len(self._a)) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = lfilter(self._b, self._a, x, zi=self._zi)
        return y.astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("audio_filters.py self-test")
    sr = 16_000
    t  = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)

    hpf  = HPF300(sr)
    deemph = DeEmphasis(sr)
    preemph = PreEmphasis(sr)

    # Test that filters pass high-freq signal and attenuate low-freq
    low  = np.sin(2 * np.pi * 50  * t).astype(np.float32)   # 50 Hz
    high = np.sin(2 * np.pi * 2000 * t).astype(np.float32)  # 2 kHz

    low_filtered  = hpf.process(low.copy())
    high_filtered = hpf.process(high.copy())

    low_rms  = float(np.sqrt(np.mean(low_filtered[sr//10:]**2)))
    high_rms = float(np.sqrt(np.mean(high_filtered[sr//10:]**2)))

    print(f"  HPF300: 50 Hz RMS {low_rms:.4f} (should be << {float(np.sqrt(np.mean(low[sr//10:]**2))):.4f})")
    print(f"  HPF300: 2 kHz RMS {high_rms:.4f} (should be ≈ {float(np.sqrt(np.mean(high[sr//10:]**2))):.4f})")
    assert low_rms < 0.01, "HPF should heavily attenuate 50 Hz"
    assert high_rms > 0.4, "HPF should pass 2 kHz"

    # De-emphasis should attenuate high frequencies relative to low
    deemph2 = DeEmphasis(sr)
    low_de  = deemph2.process(low.copy())
    high_de = DeEmphasis(sr).process(high.copy())
    print(f"  De-emph: 50 Hz RMS {float(np.sqrt(np.mean(low_de[sr//10:]**2))):.4f}")
    print(f"  De-emph: 2 kHz RMS {float(np.sqrt(np.mean(high_de[sr//10:]**2))):.4f}")

    # Pre-emphasis should boost high frequencies
    low_pre  = preemph.process(low.copy())
    high_pre = PreEmphasis(sr).process(high.copy())
    print(f"  Pre-emph: 50 Hz RMS {float(np.sqrt(np.mean(low_pre[sr//10:]**2))):.4f}")
    print(f"  Pre-emph: 2 kHz RMS {float(np.sqrt(np.mean(high_pre[sr//10:]**2))):.4f}")

    print("\nSelf-test passed.")
