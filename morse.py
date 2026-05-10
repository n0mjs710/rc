#!/usr/bin/env python3
"""
Morse Code Audio Generator

Usage:
  python3 morse.py <wpm> <pitch_hz> <volume_%> <text...>

Arguments:
  wpm       Words per minute, e.g. 20
  pitch_hz  Tone frequency in Hz, e.g. 700
  volume    Volume as 0–100, e.g. 80
  text      One or more words; use | between words (or just separate args)

Examples:
  python3 morse.py 20 700 80 HELLO WORLD
  python3 morse.py 15 600 100 CQ CQ DE W1AW
  python3 morse.py 20 700 80 "HELLO | WORLD"   # | forces word gap mid-arg

Timing (PARIS standard):
  dit       = 1200 / wpm  ms
  dah       = 3 dits
  ele gap   = 1 dit   (between dots/dashes within a letter)
  char gap  = 3 dits  (between letters)
  word gap  = 7 dits  (between words / separated args / | character)
"""

import sys
import numpy as np

SAMPLE_RATE = 44100   # default for CLI playback

# International Morse code
MORSE = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
    'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
    'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
    'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
    'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
    'Y': '-.--',  'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..',  '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', "'": '.----.',
    '!': '-.-.--', '/': '-..-.',  '(': '-.--.',  ')': '-.--.-',
    '&': '.-...',  ':': '---...', ';': '-.-.-.',  '=': '-...-',
    '+': '.-.-.',  '-': '-....-', '_': '..--.-',  '"': '.-..-.',
    '@': '.--.-.',
}


def make_tone(duration_s: float, pitch: float, volume: float,
              attack_s: float = 0.005,
              sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Generate a sine tone with a short raised-cosine attack and decay."""
    n = int(duration_s * sample_rate)
    if n == 0:
        return np.array([], dtype=np.float32)

    t    = np.linspace(0, duration_s, n, endpoint=False)
    tone = np.sin(2 * np.pi * pitch * t).astype(np.float32)

    ramp_n = min(int(attack_s * sample_rate), n // 2)
    if ramp_n > 0:
        ramp = np.hanning(ramp_n * 2)
        tone[:ramp_n]  *= ramp[:ramp_n]
        tone[-ramp_n:] *= ramp[ramp_n:]

    return tone * volume


def make_silence(duration_s: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(int(duration_s * sample_rate), dtype=np.float32)


def encode_text(words: list[str], wpm: int, pitch: float, volume: float,
                sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """
    Convert a list of words (already split on word boundaries) to audio.
    Within each word, characters are separated by char gaps; words are
    separated by word gaps.
    """
    dit  = 1.2 / wpm              # dit duration in seconds
    dah  = dit * 3
    ele  = dit                    # inter-element gap
    char = dit * 3                # inter-character gap
    word = dit * 7                # inter-word gap

    chunks: list[np.ndarray] = []

    for w_idx, word_str in enumerate(words):
        if w_idx > 0:
            chunks.append(make_silence(word, sample_rate))

        chars = [c for c in word_str.upper() if c != ' ']
        unknown = [c for c in chars if c not in MORSE]
        if unknown:
            print(f"  warning: no Morse code for {unknown}, skipping")

        first_char = True
        for ch in chars:
            if ch not in MORSE:
                continue
            if not first_char:
                chunks.append(make_silence(char, sample_rate))
            first_char = False

            elements = MORSE[ch]
            for e_idx, element in enumerate(elements):
                if e_idx > 0:
                    chunks.append(make_silence(ele, sample_rate))
                duration = dah if element == '-' else dit
                chunks.append(make_tone(duration, pitch, volume, sample_rate=sample_rate))

    if not chunks:
        return np.array([], dtype=np.float32)

    return np.concatenate(chunks)


def render(text: str, wpm: int, pitch_hz: float, level: float,
           sample_rate: int = 16_000) -> np.ndarray:
    """
    Render Morse code for text to a float32 numpy array.

    This is the in-process API used by the audio engine.  Use this instead
    of the CLI to avoid subprocess overhead.

    Args:
        text       : callsign or text string (case-insensitive)
        wpm        : speed in words per minute
        pitch_hz   : CW tone frequency in Hz
        level      : peak amplitude 0.0–1.0
        sample_rate: output sample rate (default: 16000 for the audio engine)

    Returns:
        float32 numpy array of audio samples
    """
    words = [w.strip() for w in text.upper().split() if w.strip()]
    if not words:
        return np.array([], dtype=np.float32)
    return encode_text(words, wpm, pitch_hz, level, sample_rate)


def parse_args():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(1)

    try:
        wpm    = int(sys.argv[1])
        pitch  = float(sys.argv[2])
        volume = float(sys.argv[3]) / 100.0
    except ValueError:
        print("Error: wpm, pitch, and volume must be numbers.")
        sys.exit(1)

    if not (1 <= wpm <= 60):
        print("Error: wpm must be between 1 and 60.")
        sys.exit(1)
    if not (100 <= pitch <= 4000):
        print("Error: pitch must be between 100 and 4000 Hz.")
        sys.exit(1)
    if not (0.0 <= volume <= 1.0):
        print("Error: volume must be between 0 and 100.")
        sys.exit(1)

    # Remaining args are the text; split each arg on | for word gaps
    raw = ' '.join(sys.argv[4:])
    words = [w.strip() for w in raw.replace('|', ' | ').split()
             if w.strip()]

    # A lone '|' token becomes an empty word → word gap
    # Re-group: split on '|' tokens
    groups, current = [], []
    for token in words:
        if token == '|':
            if current:
                groups.append(''.join(current))
                current = []
        else:
            current.append(token)
    if current:
        groups.append(''.join(current))

    return wpm, pitch, volume, groups


def main():
    import sounddevice as sd

    wpm, pitch, volume, words = parse_args()

    dit_ms = 1200 / wpm
    print(f"Sending: {' / '.join(words)}")
    print(f"Speed: {wpm} WPM  |  dit: {dit_ms:.1f} ms  |  "
          f"pitch: {pitch:.0f} Hz  |  volume: {volume*100:.0f}%")

    audio = encode_text(words, wpm, pitch, volume)
    if audio.size == 0:
        print("Nothing to send.")
        return

    sd.play(audio, samplerate=SAMPLE_RATE)
    sd.wait()


if __name__ == '__main__':
    main()
