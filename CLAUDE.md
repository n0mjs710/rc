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
sudo apt install python3-dev libhidapi-hidraw0
pip install -r requirements.txt     # numpy, sounddevice, hidapi
pip install RPi.GPIO                # only if using rpi_gpio backend
```

`vocab_pcm/` is excluded from git (large binary WAVs). Regenerate with `render_vocab.py` after cloning. The controller will not start without it.

A udev rule is needed to access `/dev/hidraw*` without sudo (see `udev/` in repo, to be added).

## Module map

| File | Role |
|---|---|
| `controller.py` | Main asyncio event loop — COS/CTCSS state machine, timers, PTT control |
| `audio_engine.py` | Real-time audio I/O via sounddevice; renders and plays messages |
| `hardware.py` | Hardware abstraction — `MockHardware`, `CM119Hardware`, `RealHardware` |
| `ctcss.py` | CTCSS decode (Goertzel fallback) + encode; STE/reverse-burst tail modes |
| `tones.py` | Tone synthesis (single and dual freq, numpy-rendered) |
| `morse.py` | CW synthesis at configurable WPM and pitch |
| `rc_config.py` | TOML config loader; dataclass tree that the rest of the code imports |
| `rc_shell.py` | Interactive shell for live config editing and message preview |

## Configuration

Config is `repeater.toml` (committed — default values are safe). User-specific overrides go in `repeater.local.toml` (gitignored).

Key sections:
- `[hardware]` — backend type (`cm119`, `rpi_gpio`, `mock`), device selection
- `[ctcss]` — encode/decode freq, access mode (`cos_only` / `cos_ctcss`), tail modes
- `[timers]` — tail, hang, kerchunk, timeout, ID interval
- `[messages]` — named sequences of `cw`, `voice`, and `tone` elements
- `[audio]` — sample rate (16000), volumes, repeat gain

## Target hardware: CM119-based USB radio interfaces

The primary hardware backend is CM119-family USB audio/GPIO devices. Supported units include Masters Communications RA series, RepeaterBuilder interfaces, and DMK Engineering URIx. All use the same signal mapping.

### Signal map

**HID interface** (via `/dev/hidraw*`, `hidapi` library):
- Volume Up → CTCSS decode input (level, active = CTCSS present)
- Volume Down → COR input (level, active = carrier present)
- GPIO3 → PTT output

**USB audio** (class-compliant, sounddevice):
- Mic In → discriminator / de-emphasized RX audio (input to Goertzel if no hardware CTCSS decode)
- Left Out → main TX audio (voice, tones, repeated audio)
- Right Out → CTCSS encode tone (continuous subaudible tone while PTT active, or future direct-to-modulator use)

### HID behavior

The CM119 sends an input report on every state change. Each report contains the full current level state as a bitmask — delivery is edge-triggered but content is level. Implementation: background thread blocks on `hidraw.read()`, parses Vol-Up and Vol-Down bits, fires COR/CTCSS callbacks when state changes from previous read.

Reference: AllStar/chan_usbradio uses the same approach.

### RPi GPIO backend

Still supported for users who wire PTT/COS to Pi header pins directly. BCM pin numbers, configured in `[hardware]` section. `cos_invert = true` for active-LOW COS.

## Architecture notes

**Threading**: asyncio main loop + sounddevice native-thread callbacks. The GIL is load-bearing — simple attribute writes are atomic under CPython. `_ptt_lock` guards check-then-act patterns. Do not move to free-threaded Python without a full threading audit.

**Message system**: messages are named lists of elements. Each element is one of:
- `{type = "cw", text = "N0CALL"}` — Morse code
- `{type = "voice", clip = "REPEATER"}` — pre-rendered PCM from `vocab_pcm/`
- `{type = "tone", freq1 = 1000, freq2 = 0, ms = 50, amp = 0.8}` — synthesized tone (dual-freq capable)

**Audio playback**: consecutive same-type elements are concatenated into one stream to avoid per-element startup gaps. This matters especially for VOICE (each word is a separate WAV).

**CTCSS**: Goertzel algorithm (single-bin, cheaper than FFT) is the software fallback. When CM119 hardware is in use, CTCSS decode comes from the Vol-Up HID signal instead. CTCSS encode is a subaudible tone on the Right Out audio channel, not mixed into the main Left Out stream. All hot paths that touch audio sample buffers must use numpy — pure Python loops cannot keep up at 16 kHz on a Pi 3.

**Mock mode**: `hardware.type = "mock"` in TOML (or `--mock` flag) loads `MockHardware`, which has no external dependencies and exposes `hw.simulate_cos()` and `hw.simulate_ctcss()` for testing.

## Next implementation tasks

These were fully designed in the previous session and are ready to code:

### 1. Extend `HardwareBase` interface (`hardware.py`)
Add to the abstract base class:
```python
def get_ctcss_decode(self) -> bool: ...
def add_ctcss_decode_callback(self, cb: Callable[[bool], None]) -> None: ...
def remove_ctcss_decode_callback(self, cb: Callable[[bool], None]) -> None: ...
```
Add stub implementations to `MockHardware` (with `simulate_ctcss(active)` helper) and `RealHardware` (wired to a configurable BCM pin).

### 2. Add `CM119Hardware` class (`hardware.py`)
- Dependency: `hidapi` (`pip install hidapi`, needs `libhidapi-hidraw0` on Pi)
- Auto-detect CM119 device by USB VID 0x0d8c or accept explicit hidraw path in config
- Background thread: blocking `read()` on hidraw, parse Vol-Up (CTCSS) and Vol-Down (COR) bits, fire callbacks on state change
- PTT: write HID output report with GPIO3 bit set/cleared
- Implement full `HardwareBase` interface including new CTCSS decode methods

### 3. Update `[hardware]` config section (`rc_config.py` + `repeater.toml`)
New keys:
```toml
[hardware]
type = "cm119"          # "cm119", "rpi_gpio", or "mock"
hidraw_device = ""      # leave empty for auto-detect by VID
audio_device = ""       # sounddevice name or index, empty = system default
```

### 4. Stereo TX audio (`audio_engine.py`)
- TX output changes from mono to stereo
- Left channel: main audio mix (voice, tones, repeated RX audio)
- Right channel: CTCSS encode tone (continuous while PTT active, silence otherwise)
- CTCSS encode tone is generated by `ctcss.py` and passed to audio engine separately from message playback

### 5. Wire hardware CTCSS decode into controller (`ctcss.py` + `controller.py`)
- When `CM119Hardware` (or `RealHardware` with ctcss_decode_pin set), use hardware signal instead of Goertzel
- Goertzel path remains for `MockHardware` and when no hardware decode line is configured
- `controller.py` queries hardware backend capability at startup to select the active path

### 6. udev rule
Add `udev/99-cm119.rules` to repo so users can access `/dev/hidraw*` without sudo.
