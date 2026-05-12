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

# Or with systemd (after setup.sh --service)
sudo systemctl start rc
sudo systemctl restart rc   # after code changes
journalctl -u rc -f         # live log
```

The daemon serves a Unix socket at `run/rc.sock` relative to the config file (configurable). The shell connects to it for monitoring and live configuration.

## Pi setup

```bash
bash setup.sh            # packages, udev, group, venv
bash setup.sh --service  # same, plus install and enable systemd service
```

Or manually:

```bash
sudo apt install python3-dev python3-venv libportaudio2
pip install -r requirements.txt     # numpy, sounddevice, scipy

# udev rule for /dev/hidraw* access without sudo
sudo cp udev/99-cm119.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG audio $USER
# Then unplug and replug the CM119 (udev re-evaluates on connect)
# And open a new terminal or run 'newgrp audio' for the group change to take effect
```

Note: `hardware.py` uses direct hidraw file I/O (`/dev/hidraw*`) — no `hidapi` Python package needed. The CM119's HID interface is accessed via the kernel's hidraw driver, which avoids the libusb conflict with `snd_usb_audio`.

Audio device must be explicitly set in config (`audio_device = "USB PnP Sound Device"`). The CM119 only supports 44100 and 48000 Hz; config uses 48000.

`vocab_pcm/` is committed to git (712 WAV files). No regeneration needed after cloning.

## Module map

| File | Role |
|---|---|
| `daemon.py` | Main entry point — wires hardware, audio, port, and API server |
| `shell.py` | Operator CLI — connects to daemon via Unix socket |
| `port.py` | State machine — IDLE/PENDING/ACTIVE/TAIL/TIMEOUT, timers, ID scheduling |
| `api_server.py` | Unix socket server — JSON-lines protocol, push events |
| `audio_engine.py` | Duplex sounddevice stream — clip queue, RX/TX filter chain, STE buffer |
| `audio_filters.py` | HPF300, DeEmphasis, PreEmphasis (scipy.signal IIR filters) |
| `hardware.py` | CM119Hardware — HID reader thread, PTT/COR/CTCSS callbacks |
| `tones.py` | Tone synthesis (single and dual freq, numpy-rendered) |
| `morse.py` | CW synthesis — `render()` for in-process use, CLI for standalone |
| `rc_config.py` | TOML config model, `apply_set_command()` for shell set commands |
| `ctcss.py` | CTCSS encode/decode (currently unused; retained for future use) |
| `version.py` | Version string |

## Configuration

Config is `repeater.toml.sample` (reference template, committed). Copy to `repeater.toml` (gitignored) and edit for your site. `setup.sh` creates `repeater.toml` from the sample if it doesn't exist.

Key sections:
- `[daemon]` — socket_path (relative paths resolve from config file's directory; default `run/rc.sock`), log_level
- `[hardware]` — hidraw_device (empty = auto-detect), audio_device, cor_active_low, ctcss_active_low
- `[audio]` — sample_rate (48000 — CM119 only supports 44100/48000), rx_hpf, rx_deemphasis, tx_preemphasis, repeat_gain, morse_wpm, morse_pitch, morse_level, impolite_morse_level, voice_level, voice_blocks_repeat, pre_message_ms, post_message_ms, ste_delay_ms
- `[ctcss]` — access_mode ("cor" or "cor_ctcss")
- `[timers]` — hang (PTT holdoff/"hangup time"), ct_delay (pre-CT pause), kerchunk, timeout, id_interval, id_pending
- `[identity]` — startup_message, initial_ids (rotation list), mandatory_ids (rotation list), pending_id, impolite_id, ct_message, timeout_message, timeout_cancel_message
- `[messages]` — named sequences of cw/voice/tone elements

## Architecture

**Daemon + shell**: The daemon is a long-running process. The shell is a separate process (or remote SSH session) that connects via Unix socket. Multiple shells can connect simultaneously.

**Threading**: asyncio main loop + sounddevice native-thread callbacks + CM119 HID reader thread. The GIL is load-bearing — simple attribute writes are atomic under CPython. `_ptt_lock` guards check-then-act patterns on PTT state. `_ste_flush` and `_ptt` are plain Python bools shared between threads — GIL-safe for simple reads/writes. Do not move to free-threaded Python without a full threading audit.

**Audio filter chain**:
- RX in → HPF300 (removes sub-audible) → DeEmphasis (FM) → STE delay buffer → TX passthrough gate
- TX out left = passthrough + clips (CW, tones, voice)
- TX out right = silence (reserved for CTCSS encode tone)
- PreEmphasis applied to left channel if `tx_preemphasis = true`

**CTCSS**: Hardware signals from the CM119 HID interface. COR = Vol-Up bit (byte 0, bit 1, 0x02); CTCSS decode = Vol-Down bit (byte 0, bit 0, 0x01). Polarity configurable via `cor_active_low` / `ctcss_active_low`. Software Goertzel decode (`ctcss.py`) is retained for future use but not currently active.

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

**ID system** (port.py):
- Boots in *quiet period* (`_id_timer is None`); timer starts after first TX
- `_tx_activity` flag — True when TX has been used since last ID; checked at timer expiry
- *Initial ID* — at end of hang following first TX from quiet period; waits politely for QSO to end
- *Mandatory ID* — timer expires with activity, state not ACTIVE
- *Pending ID* — sneaked in at COR drop (before CT) when `_pending_id_armed` is set by the `id_pending` sub-timer
- *Impolite ID* — plays over active QSO when mandatory deadline hits mid-transmission; uses `impolite_morse_level` for CW ducking; does not reset `_tx_activity` (QSO is still ongoing)
- *Epoch interruption* — `_id_epoch` counter; incremented when voice ID is interrupted by incoming COR; ID coroutines check epoch after each drain and bail if changed, so only one coroutine resets the ID cycle

**STE (squelch tail elimination)** (audio_engine.py):
- `ste_delay_ms` → `_ste_delay_blocks` blocks of FIFO delay between filter chain and passthrough gate
- Gate closes on real-time hardware edge; delay buffer is abandoned → noise crash discarded
- Buffer flushed on passthrough-open via `_ste_flush` flag (set from asyncio thread, cleared in callback)
- Startup silence of `ste_delay_ms` per new COR assertion (hidden by CTCSS decode hysteresis in practice)

**Async audio playback** (port.py): IDs, timeout announce, and timeout-cancel message use `create_task` + `_drain_clips()` to wait for the clip queue to empty before dropping PTT. This ensures audio actually plays; it's necessary because `play_message()` queues samples to the engine deque — PTT must stay on until the audio callback consumes them.

**Pre/post message padding** (port.py): `pre_message_ms` / `post_message_ms` add `asyncio.sleep()` dead air around CW/voice messages when PTT goes on/off specifically for that message (standalone ID transmissions). Implemented in `_transmit_id()` and `_play_startup()`.

**RX audio source gating** (port.py + audio_engine.py):
- Passthrough gates on hardware signal edges, not state transitions
- "cor" mode: passthrough open when COR is active
- "cor_ctcss" mode: passthrough open when both COR and CTCSS are active
- Closes the moment the qualifying signal drops (not when TAIL is entered)

## Next implementation tasks

1. **CTCSS encode** — right audio channel; configure tone frequency in `[ctcss]`
2. **Software CTCSS decode** — Goertzel fallback when hardware signal unavailable; wire into port.py; tap `rx` before HPF in audio callback
3. **DTMF decode** — autopatch / remote-control command interface; tap `rx` after HPF but before STE buffer in audio callback
4. **Multi-port linking** — PortConfig array; priority-based audio routing between ports
