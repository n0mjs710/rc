#!/usr/bin/env python3
"""
Courtesy tone and alert tone generator.

Each tone is a sequence of elements with the ACC format:
    [freq1_hz, freq2_hz, duration_ms, amplitude]

  freq1_hz   : primary sine frequency in Hz; 0 = silence
  freq2_hz   : secondary sine frequency in Hz; 0 = not used
  duration_ms: element duration in milliseconds
  amplitude  : peak level 0.0–1.0

When both freq1 and freq2 are non-zero, the two sines are summed and
normalised so the combined waveform stays within the amplitude limit.
When both are zero the element is a silence gap.

Tone elements can be stored inline in message definitions or referenced
by name from the built-in tone library below.  The built-in tones serve
as defaults for RepeaterConfig and can be previewed with this CLI tool.

Usage:
  python3 tones.py <name> [config.toml]   # play named tone
  python3 tones.py --list [config.toml]   # list available tones
  python3 tones.py --test [config.toml]   # play all available tones in sequence
"""

import argparse
import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
ATTACK_S    = 0.005   # 5 ms raised-cosine attack/decay (click-free keying)


# ─────────────────────────────────────────────────────────────────────────────
# Built-in named tones
# Each entry: list of elements → [freq1_hz, freq2_hz, duration_ms, amplitude]
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_TONES: dict[str, list] = {

    # Classic two-pip: ascending interval, commonly used on ACC-equipped repeaters
    "default": [
        [1000, 0, 50, 0.8],
        [   0, 0, 30, 0.0],
        [1200, 0, 50, 0.8],
    ],

    # Single pip — minimal, unobtrusive
    "single": [
        [1000, 0, 80, 0.8],
    ],

    # Three ascending pips — used as a mild state-change indicator
    "triple": [
        [ 800, 0, 40, 0.8],
        [   0, 0, 20, 0.0],
        [1000, 0, 40, 0.8],
        [   0, 0, 20, 0.0],
        [1200, 0, 40, 0.8],
    ],

    # Morse K (dah-dit-dah) — traditional courtesy tone at 20 WPM timing
    # dit = 60 ms, dah = 180 ms, inter-element gap = 60 ms
    "K": [
        [700, 0, 180, 0.8],
        [  0, 0,  60, 0.0],
        [700, 0,  60, 0.8],
        [  0, 0,  60, 0.0],
        [700, 0, 180, 0.8],
    ],

    # Two-tone chord — both frequencies simultaneously (e.g. DTMF-style)
    "chord": [
        [697, 1209, 150, 0.75],
    ],

    # Timeout warning — descending three-tone alert
    "timeout_warn": [
        [1200, 0, 60, 0.9],
        [   0, 0, 20, 0.0],
        [1000, 0, 60, 0.9],
        [   0, 0, 20, 0.0],
        [ 800, 0, 60, 0.9],
    ],

    # ID reminder — subtle single low pip
    "id_pending": [
        [880, 0, 60, 0.6],
        [  0, 0, 30, 0.0],
        [880, 0, 60, 0.6],
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Audio generation
# ─────────────────────────────────────────────────────────────────────────────

def _make_element(freq1: float, freq2: float,
                  duration_ms: int, amplitude: float) -> np.ndarray:
    """Render one tone element to a float32 array at SAMPLE_RATE."""
    n = max(1, int(duration_ms / 1000.0 * SAMPLE_RATE))

    if freq1 <= 0 and freq2 <= 0:
        return np.zeros(n, dtype=np.float32)

    t    = np.linspace(0.0, duration_ms / 1000.0, n, endpoint=False)
    wave = np.zeros(n, dtype=np.float32)
    active = 0

    if freq1 > 0:
        wave  += np.sin(2 * np.pi * freq1 * t)
        active += 1
    if freq2 > 0:
        wave  += np.sin(2 * np.pi * freq2 * t)
        active += 1

    # Normalise so two mixed tones don't exceed the requested amplitude
    if active > 1:
        wave /= active

    # Raised-cosine attack/decay to eliminate key clicks
    ramp_n = min(int(ATTACK_S * SAMPLE_RATE), n // 2)
    if ramp_n > 0:
        ramp = np.hanning(ramp_n * 2)
        wave[:ramp_n]  *= ramp[:ramp_n]
        wave[-ramp_n:] *= ramp[ramp_n:]

    return (wave * float(amplitude)).astype(np.float32)


def render_tone(elements: list) -> np.ndarray:
    """
    Render a named tone's element list to a float32 numpy array at SAMPLE_RATE.
    elements: list of [freq1_hz, freq2_hz, duration_ms, amplitude]
    """
    if not elements:
        return np.array([], dtype=np.float32)

    chunks = [
        _make_element(float(e[0]), float(e[1]), int(e[2]), float(e[3]))
        for e in elements
    ]
    return np.concatenate(chunks)


def play_tone(elements: list, block: bool = True) -> None:
    """Render and play a tone element list through the default audio output."""
    import sounddevice as sd
    audio = render_tone(elements)
    if audio.size > 0:
        sd.play(audio, samplerate=SAMPLE_RATE)
        if block:
            sd.wait()


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────

def load_tones_from_config(config_path: str) -> dict[str, list]:
    """
    Read courtesy tone definitions from a TOML repeater config file.
    Returns a dict of {name: elements_list}, merging over BUILTIN_TONES.
    """
    import tomllib
    data = tomllib.loads(Path(config_path).read_text())
    raw  = data.get("courtesy_tones", {})
    return {name: tone["elements"] for name, tone in raw.items()}


def get_tones(config_path: str | None = None) -> dict[str, list]:
    """Return merged dict of built-in tones plus any tones from config_path."""
    tones = dict(BUILTIN_TONES)
    if config_path and Path(config_path).exists():
        try:
            tones.update(load_tones_from_config(config_path))
        except Exception as exc:
            print(f"Warning: could not read tones from {config_path}: {exc}",
                  file=sys.stderr)
    return tones


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _element_summary(elements: list) -> str:
    parts = []
    for e in elements:
        f1, f2, ms, amp = float(e[0]), float(e[1]), int(e[2]), float(e[3])
        if f1 <= 0 and f2 <= 0:
            parts.append(f"gap {ms}ms")
        elif f2 > 0:
            parts.append(f"{f1:.0f}+{f2:.0f}Hz {ms}ms")
        else:
            parts.append(f"{f1:.0f}Hz {ms}ms")
    return "  ".join(parts)


def _tone_duration_ms(elements: list) -> int:
    return sum(int(e[2]) for e in elements)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name",   nargs="?", help="name of tone to play")
    parser.add_argument("config", nargs="?", help="TOML config file (optional)")
    parser.add_argument("--list", dest="list_tones", action="store_true",
                        help="list available tone names and their elements")
    parser.add_argument("--test", action="store_true",
                        help="play every available tone in sequence")
    args = parser.parse_args()

    tones = get_tones(args.config)

    if args.list_tones:
        print("Available courtesy tones:")
        for name, elements in sorted(tones.items()):
            dur = _tone_duration_ms(elements)
            print(f"  {name:<20}  {len(elements):2d} elem  {dur:4d} ms  "
                  f"  {_element_summary(elements)}")
        return

    if args.test:
        for name, elements in sorted(tones.items()):
            dur = _tone_duration_ms(elements)
            print(f"  ▶ {name}  ({dur} ms)")
            play_tone(elements)
        return

    if not args.name:
        parser.print_help()
        sys.exit(0)

    name = args.name
    if name not in tones:
        avail = ", ".join(sorted(tones))
        print(f"Unknown tone '{name}'.  Available: {avail}", file=sys.stderr)
        sys.exit(1)

    play_tone(tones[name])


if __name__ == "__main__":
    main()
