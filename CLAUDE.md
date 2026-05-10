# Repeater Controller — CLAUDE.md

Ham radio repeater controller targeting Raspberry Pi 3+. Written in Python with asyncio + sounddevice. Primary developer is Cort Buffington (N0MJS), author of HBLink3/HBLink4.

## Development workflow

Primary development is done over **VS Code Remote SSH** directly on the Pi. The repo lives on GitHub at `https://github.com/n0mjs710/rc`. Changes are committed and pushed from the Pi.

Mac is used only when Pi hardware isn't needed (config work, doc edits). Never assume Mac-only libraries are available; target is Raspberry Pi OS (Debian-based, ARM).

## Running the controller

```bash
python controller.py repeater.toml          # real hardware
python controller.py repeater.toml --mock   # mock GPIO (for dev/test)
```

The interactive shell (`rc_shell.py`) can be started standalone to edit config and preview audio without running the full controller.

## Pi setup

```bash
pip install -r requirements.txt     # numpy, sounddevice
pip install RPi.GPIO                # Pi only — not in requirements.txt intentionally
```

`vocab_pcm/` is excluded from git (large binary WAVs). Regenerate with `render_vocab.py` after cloning. The controller will not start without it.

## Module map

| File | Role |
|---|---|
| `controller.py` | Main asyncio event loop — COS/CTCSS state machine, timers, PTT control |
| `audio_engine.py` | Real-time audio I/O via sounddevice; renders and plays messages |
| `hardware.py` | GPIO abstraction — `MockHardware` (any platform) / `RealHardware` (RPi.GPIO) |
| `ctcss.py` | Goertzel-based CTCSS decode + encode; STE/reverse-burst tail modes |
| `tones.py` | Tone synthesis (single and dual freq, numpy-rendered) |
| `morse.py` | CW synthesis at configurable WPM and pitch |
| `rc_config.py` | TOML config loader; dataclass tree that the rest of the code imports |
| `rc_shell.py` | Interactive shell for live config editing and message preview |

## Configuration

Config is `repeater.toml` (committed — default values are safe). User-specific overrides go in `repeater.local.toml` (gitignored).

Key sections:
- `[hardware]` — GPIO pin numbers (BCM), `mock = true/false`, `cos_invert`
- `[ctcss]` — encode/decode freq, access mode (`cos_only` / `cos_ctcss`), tail modes
- `[timers]` — tail, hang, kerchunk, timeout, ID interval
- `[messages]` — named sequences of `cw`, `voice`, and `tone` elements
- `[audio]` — sample rate (16000), volumes, repeat gain

## Hardware

- **PTT output**: GPIO 17 (BCM), active HIGH
- **COS input**: GPIO 27 (BCM), `cos_invert = true` means active LOW
- Audio interface: USB sound card or Pi built-in (configured via sounddevice device index)

## Architecture notes

**Threading**: asyncio main loop + sounddevice native-thread callbacks. The GIL is load-bearing — simple attribute writes are atomic under CPython. `_ptt_lock` guards check-then-act patterns. Do not move to free-threaded Python without a full threading audit.

**Message system**: messages are named lists of elements. Each element is one of:
- `{type = "cw", text = "N0CALL"}` — Morse code
- `{type = "voice", clip = "REPEATER"}` — pre-rendered PCM from `vocab_pcm/`
- `{type = "tone", freq1 = 1000, freq2 = 0, ms = 50, amp = 0.8}` — synthesized tone (dual-freq capable)

**Audio playback**: consecutive same-type elements are concatenated into one stream to avoid per-element startup gaps. This matters especially for VOICE (each word is a separate WAV).

**CTCSS**: Goertzel algorithm (single-bin, cheaper than FFT). All hot paths that touch audio sample buffers must use numpy — pure Python loops cannot keep up at 16 kHz on a Pi 3.

**Mock mode**: `hardware.mock = true` in TOML (or `--mock` flag) loads `MockHardware`, which has no RPi.GPIO dependency and exposes `hw.simulate_cos(True/False)` for testing.
