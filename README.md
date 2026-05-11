# Repeater Controller

A software repeater controller for FM amateur radio, targeting Raspberry Pi 3+ with CM119-family USB audio/GPIO interfaces. Written in Python with asyncio and sounddevice.

**N0MJS / Cort Buffington**

---

## What it does

This controller manages a full-duplex FM repeater (simultaneous receive and transmit on separate frequencies via a hardware duplexer):

- Monitors the **COR** (Carrier Operated Relay) signal from the radio interface to detect incoming transmissions
- Keys the repeater's **PTT** (Push-To-Talk) when access conditions are met and gates received audio back out on the transmit path
- Plays **courtesy tones** at the end of each transmission and **CW or voice IDs** on a configurable schedule to satisfy FCC identification requirements
- Implements **anti-kerchunk** (minimum carrier hold time), a **time-out timer (TOT)** to prevent stuck transmitters, and CT-delay/hang timers for a clean operating experience
- Applies **FM de-emphasis** to received audio and optional **pre-emphasis** and **300 Hz HPF** on the transmit path — all selectable per-port in config

All audio processing runs in the sounddevice callback thread using NumPy. Courtesy tones and CW IDs are rendered in-process from numpy arrays at startup; there are no subprocess calls on any hot path.

## Hardware

Any **CM108/CM119/CM119B** USB audio device works:

- Masters Communications RA series
- RepeaterBuilder interfaces
- DMK Engineering URIx
- Many generic "USB radio interface" dongles

Signal mapping (AllStar `chan_usbradio` convention):

| Signal | Direction | HID / Audio path |
|--------|-----------|-----------------|
| COR    | Input     | HID Vol-Down bit (byte 0, bit 0) |
| CTCSS  | Input     | HID Vol-Up bit (byte 0, bit 1) |
| PTT    | Output    | HID GPIO3 bit (output report, bit 2) |
| RX audio | Input  | USB audio mic channel (discriminator output) |
| TX audio | Output | USB audio left speaker channel |

The right audio output channel is reserved for a future CTCSS encode tone.

## Requirements

```
Raspberry Pi OS (Debian-based, ARM)
Python 3.11+
```

### First-time setup

Run the setup script as the user who will operate the repeater:

```bash
bash setup.sh
```

This installs system packages, the udev rule, adds your user to the `audio` group, and creates the Python venv — using sudo only for the steps that require it. Everything else stays inside the project directory.

After the script completes, unplug and replug the CM119, then open a new terminal (or run `newgrp audio`) for the group change to take effect.

To also install and enable the systemd service:

```bash
bash setup.sh --service
```

Run `bash setup.sh --help` for details on what each step does.

### Verifying hardware access

After setup, confirm the CM119 is accessible:

```bash
# Device should appear (vendor 0d8c)
lsusb | grep 0d8c

# hidraw node should be group-writable by audio
ls -la /dev/hidraw*
# Expected: crw-rw---- 1 root audio ...

# Quick open test
python3 -c "
import os, select
fd = os.open('/dev/hidraw0', os.O_RDWR)
r, _, _ = select.select([fd], [], [], 0.2)
print(os.read(fd, 4) if r else 'no data (device idle — permissions OK)')
os.close(fd)
"
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| `/dev/hidraw*` missing | CM119 not plugged in | Check `dmesg \| tail` |
| `crw-------` (root only) | udev rule not applied | Replug the device after installing rule |
| Open fails despite correct permissions | Group not active in session | Run `newgrp audio` or open new terminal |

## Voice vocabulary

Pre-rendered voice WAV files live in `vocab_pcm/` (committed to git — no generation step needed after cloning).

Drop custom `.wav` files in `user_pcm/` to override or extend the vocabulary. Files are loaded by name (case-insensitive, without extension).

## Running

```bash
# Start the daemon
python daemon.py repeater.toml

# In another terminal, connect the operator shell
python shell.py repeater.toml
```

The daemon exposes a Unix domain socket (`/run/rc/rc.sock` by default). The shell connects to it for monitoring and configuration.

## Shell commands

```
state               Show repeater state (IDLE/ACTIVE/TAIL/TIMEOUT)
config              Show current configuration
set <field> <val>   Change a config value live (e.g. "set hang 2500")
play <message>      Trigger a named message (e.g. "play default_cw")
ptt on|off          Force PTT on or off
subscribe           Stream state-change events (Ctrl-C to stop)
reload              Reload repeater.toml from disk
shutdown            Stop the daemon
help                Command reference
quit                Disconnect
```

Set examples:
```
set hang 2500         2500 ms hang time (PTT holdoff)
set ct delay 500ms    500 ms CT delay
set timeout 3m
set morse wpm 20
set morse pitch 700
set ctcss access cor_ctcss
```

## Configuration

Copy the sample and edit for your site:

```bash
cp repeater.toml.sample repeater.toml
```

`repeater.toml` is gitignored — it holds your callsign, hardware device names, and access settings. `repeater.toml.sample` is the committed reference.

Key sections:

```toml
[ctcss]
access_mode = "cor"       # "cor" = COR alone opens repeater
                          # "cor_ctcss" = both COR and CTCSS required

[audio]
rx_hpf          = true   # 300 Hz HPF removes sub-audible energy from RX
rx_deemphasis   = true   # FM de-emphasis on received audio
tx_preemphasis  = false  # FM pre-emphasis on transmitted audio
repeat_gain     = 1.0    # RX passthrough level multiplier

[timers]
hang        = 2.5    # s — hang time: PTT holdoff after CT ("hangup time")
ct_delay    = 0.5    # s — delay from RX stream loss to courtesy tone
kerchunk    = 0.5    # s — minimum COR hold to respond
timeout     = 180.0  # s — TOT cutoff
id_interval = 600.0  # s — FCC ID interval (≤ 10 min)
```

Your site config lives in `repeater.toml` (gitignored). The committed `repeater.toml.sample` is the reference template.

### Messages

Messages are named sequences of audio elements:

```toml
[messages.my_id]
elements = [
  {type = "voice", clip = "THIS"},
  {type = "voice", clip = "IS"},
  {type = "cw",    text = "W1AW"},
  {type = "tone",  freq1 = 1000, freq2 = 0, ms = 80, amp = 0.8},
]
```

Element types:
- `cw` — Morse code rendered in-process
- `voice` — pre-rendered WAV clip from `vocab_pcm/` or `user_pcm/`
- `tone` — synthesized tone (supports dual-frequency for DTMF-style tones)

### Courtesy tones

```toml
[courtesy_tones.my_tone]
elements = [[1000, 0, 50, 0.8], [0, 0, 30, 0.0], [1200, 0, 50, 0.8]]
# format: [freq1_hz, freq2_hz, duration_ms, amplitude]
```

## Running as a systemd service

The service runs as your user (edit `User=` and paths in `systemd/rc.service` to match your username). The socket and all runtime files stay inside the project directory — nothing is scattered across the OS.

Install and start:

```bash
sudo cp systemd/rc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rc
sudo systemctl start rc

# Check status
sudo systemctl status rc
journalctl -u rc -f
```

For manual use (outside systemd), the socket is at `run/rc.sock` inside the project directory — no root needed, nothing outside the project tree.

## Architecture

```
daemon.py          — asyncio event loop; owns hardware, audio, and port
  ├── hardware.py  — CM119Hardware: blocking HID read thread, PTT/COR/CTCSS
  ├── audio_engine.py — sounddevice duplex stream; clip queue; RX/TX filters
  │     └── audio_filters.py — HPF300, DeEmphasis, PreEmphasis (scipy.signal)
  ├── port.py      — state machine: IDLE/PENDING/ACTIVE/TAIL/TIMEOUT
  └── api_server.py — Unix socket; JSON-lines protocol; push events

shell.py           — operator CLI; connects to daemon socket
rc_config.py       — TOML config model; "set" command parser
tones.py           — courtesy tone synthesis (numpy)
morse.py           — CW synthesis (numpy)
```

The daemon and shell are separate processes. The shell connects over a Unix socket and receives pushed state-change events in real time. Multiple shell instances can connect simultaneously.

## Future work

- CTCSS encode on the right audio output channel (hardware tone injection)
- Software Goertzel CTCSS decode fallback for interfaces without hardware decode
- Multi-port linking with priority-based audio routing
- DTMF decode for autopatch / remote control
