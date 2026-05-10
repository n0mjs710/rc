# Repeater Controller — CLAUDE.md

Ham radio repeater controller targeting Raspberry Pi 3+. Written in Python with asyncio + sounddevice. Primary developer is Cort Buffington (N0MJS), author of HBLink3/HBLink4.

## Development workflow

Primary development is done over **VS Code Remote SSH** directly on the Pi. The repo lives on GitHub at `https://github.com/n0mjs710/rc`. Changes are committed and pushed from the Pi.

Mac is used only when Pi hardware isn't needed (config work, doc edits). Never assume Mac-only libraries are available; target is Raspberry Pi OS (Debian-based, ARM).

## Running the controller

```bash
# Start the daemon (requires CM119 hardware plugged in)
python daemon.py repeater.toml

# Connect the operator shell (separate terminal or SSH session)
python shell.py repeater.toml
```

The daemon serves a Unix socket at `/run/rc/rc.sock` (configurable). The shell connects to it for monitoring and live configuration.

## Pi setup

```bash
sudo apt install python3-dev libhidapi-hidraw0
pip install -r requirements.txt     # numpy, sounddevice, scipy, hidapi

# udev rule for /dev/hidraw* access without sudo
sudo cp udev/99-cm119.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG audio $USER
```

`vocab_pcm/` is committed to git (712 WAV files). No regeneration needed after cloning.

## Module map

| File | Role |
|---|---|
| `daemon.py` | Main entry point — wires hardware, audio, port, and API server |
| `shell.py` | Operator CLI — connects to daemon via Unix socket |
| `port.py` | State machine — IDLE/PENDING/ACTIVE/TAIL/TIMEOUT, timers, ID scheduling |
| `api_server.py` | Unix socket server — JSON-lines protocol, push events |
| `audio_engine.py` | Duplex sounddevice stream — clip queue, RX/TX filter chain |
| `audio_filters.py` | HPF300, DeEmphasis, PreEmphasis (scipy.signal IIR filters) |
| `hardware.py` | CM119Hardware — HID reader thread, PTT/COR/CTCSS callbacks |
| `tones.py` | Tone synthesis (single and dual freq, numpy-rendered) |
| `morse.py` | CW synthesis — `render()` for in-process use, CLI for standalone |
| `rc_config.py` | TOML config model, `apply_set_command()` for shell set commands |
| `ctcss.py` | CTCSS encode/decode (currently unused; retained for future use) |

## Configuration

Config is `repeater.toml` (committed — defaults are safe). User-specific overrides go in `repeater.local.toml` (gitignored).

Key sections:
- `[daemon]` — socket_path, log_level
- `[hardware]` — hidraw_device (empty = auto-detect), audio_device
- `[audio]` — sample_rate (16000), rx_hpf, rx_deemphasis, tx_preemphasis, repeat_gain, morse/voice levels
- `[ctcss]` — access_mode ("cor" or "cor_ctcss")
- `[timers]` — hang (PTT holdoff/"hangup time"), ct_delay (pre-CT pause), kerchunk, timeout, id_interval
- `[identity]` — callsign, message rotation lists, ct_message
- `[messages]` — named sequences of cw/voice/tone elements
- `[courtesy_tones]` — named tone sequences [freq1, freq2, ms, amp]

## Architecture

**Daemon + shell**: The daemon is a long-running process. The shell is a separate process (or remote SSH session) that connects via Unix socket. Multiple shells can connect simultaneously.

**Threading**: asyncio main loop + sounddevice native-thread callbacks + CM119 HID reader thread. The GIL is load-bearing — simple attribute writes are atomic under CPython. `_ptt_lock` guards check-then-act patterns on PTT state. Do not move to free-threaded Python without a full threading audit.

**Audio filter chain**:
- RX in → HPF300 (removes sub-audible) → DeEmphasis (FM) → TX passthrough
- TX out left = passthrough + clips (CW, tones, voice)
- TX out right = silence (reserved for CTCSS encode tone)
- PreEmphasis applied to left channel if `tx_preemphasis = true`

**CTCSS**: Hardware signals from the CM119 HID interface. The HID Vol-Down bit = COR, Vol-Up bit = CTCSS decode. Software Goertzel decode (`ctcss.py`) is retained for future use but not currently active.

**Message system**: messages are named lists of elements. Each element is one of:
- `{type = "cw", text = "N0CALL"}` — Morse code rendered in-process via morse.render()
- `{type = "voice", clip = "REPEATER"}` — pre-rendered PCM from `vocab_pcm/` or `user_pcm/`
- `{type = "tone", freq1 = 1000, freq2 = 0, ms = 50, amp = 0.8}` — synthesized tone

**Unix socket API**: JSON-lines (newline-delimited JSON). Commands: state, config, set, play, ptt, reload, shutdown, subscribe. Push events are broadcast to subscribed clients on every state change.

**State machine** (port.py):
- IDLE → ACTIVE: COR up (+ CTCSS if cor_ctcss mode)
- ACTIVE → TAIL: COR (or CTCSS in cor_ctcss mode) drops (after kerchunk check)
- TAIL: ct_delay timer fires → CT plays → TOT resets → hang timer starts
- TAIL → IDLE: hang timer expires (TX "hangs up" = PTT off)
- TAIL → ACTIVE: COR (+ CTCSS) comes back up during hang; TOT continues if CT hasn't fired yet, fresh TOT if CT already fired and reset it
- ACTIVE → TIMEOUT: TOT exceeded; RX gate closes, timeout message plays, PTT off, locked out
- TIMEOUT → TAIL: Offending COR drops; TX comes back up, plays timeout-cancel message (if configured), hang runs, PTT off → IDLE
- TIMEOUT: incoming qualified signals are ignored (locked out); ID timer fires normally (PTT on for ID, off after)

**Async audio playback** (port.py): IDs, timeout announce, and timeout-cancel message use `create_task` + `_drain_clips()` to wait for the clip queue to empty before dropping PTT. This ensures audio actually plays; it's necessary because `play_message()` queues samples to the engine deque — PTT must stay on until the audio callback consumes them.

**RX audio source gating** (port.py + audio_engine.py):
- Passthrough gates on hardware signal edges, not state transitions
- "cor" mode: passthrough open when COR is active
- "cor_ctcss" mode: passthrough open when both COR and CTCSS are active
- Closes the moment the qualifying signal drops (not when TAIL is entered)

## Next implementation tasks

1. **CTCSS encode** — right audio channel; configure tone frequency in `[ctcss]`
2. **Software CTCSS decode** — Goertzel fallback when hardware signal unavailable; wire into port.py
3. **Multi-port linking** — PortConfig array; priority-based audio routing between ports
4. **DTMF decode** — autopatch / remote-control command interface
