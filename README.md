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
| COR    | Input     | HID Vol-Up bit (byte 0, bit 1, 0x02) |
| CTCSS  | Input     | HID Vol-Down bit (byte 0, bit 0, 0x01) |
| PTT    | Output    | HID GPIO3 bit (output report, bit 2) |
| RX audio | Input  | USB audio mic channel (discriminator output) |
| TX audio | Output | USB audio left speaker channel |

The right audio output channel is reserved for a future CTCSS encode tone.

> **Board-specific:** The COR/CTCSS bit assignments above match the Masters Communications CM119 board. Some boards (e.g., DMK URIx) swap Vol-Up and Vol-Down. Adjust `cor_active_low` / `ctcss_active_low` in `[hardware]` to match your interface.

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

712 pre-rendered voice clips live in `vocab_pcm/` (committed to git — no generation step needed after cloning). They are sourced from the original **Texas Instruments speech synthesizer library** used in the famous repeater controllers of the 1980s and 1990s — included here as a deliberate nod to the controllers many of us grew up hearing on the air.

Drop custom `.wav` files in `user_pcm/` to override any built-in clip or add new ones. Files are loaded by name (case-insensitive, without extension). `user_pcm/` takes precedence over `vocab_pcm/` on name collisions — you can replace the entire built-in vocabulary with your own recordings if you prefer a different voice.

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

**The shell is the primary configuration interface.** Connect to the running daemon with `python shell.py repeater.toml` and use `set` and `msg` commands to change settings and build messages live — no file editing required. Changes made with `set` take effect immediately on the running daemon. A `save` command (planned for the v1.0 release) will write the live configuration back to `repeater.toml`, making the shell the complete round-trip interface for all settings.

For now, use the TOML file for initial site setup (hardware device names, callsign embedded in your first ID message, access mode) and for anything the shell doesn't yet cover (identity slot assignments such as `initial_ids`, `mandatory_ids`). Once the daemon is running, day-to-day tuning — timers, audio levels, courtesy tones, message content — is all done through the shell. Use `reload` to pull TOML changes into a running daemon without restarting.

### TOML quick-start

Copy the sample and edit for your site:

```bash
cp repeater.toml.sample repeater.toml
```

`repeater.toml` is gitignored — it holds your callsign (embedded in your ID messages), hardware device names, and access settings. `repeater.toml.sample` is the committed reference.

Key sections:

```toml
[hardware]
cor_active_low   = true   # true = bit clear means COR active (AllStar convention)
ctcss_active_low = true   # true = bit clear means CTCSS active

[ctcss]
access_mode = "cor"       # "cor" = COR alone opens repeater
                          # "cor_ctcss" = both COR and CTCSS required

[audio]
rx_hpf               = true   # 300 Hz HPF removes sub-audible energy from RX
rx_deemphasis        = true   # FM de-emphasis on received audio
tx_preemphasis       = false  # FM pre-emphasis on transmitted audio
repeat_gain          = 1.0    # RX passthrough level multiplier
morse_level          = 0.9    # CW amplitude
impolite_morse_level = 0.3    # CW level when IDing over an active QSO (ducked)
voice_level          = 0.9    # voice clip amplitude
pre_message_ms       = 0      # dead air after PTT-on before CW/voice starts
post_message_ms      = 0      # dead air after audio drains before PTT-off
ste_delay_ms         = 0      # squelch tail elimination delay (0 = disabled)

[timers]
hang        = 2.5    # s — hang time: PTT holdoff after CT ("hangup time")
ct_delay    = 0.5    # s — delay from RX stream loss to courtesy tone
kerchunk    = 0.5    # s — minimum COR hold to respond
timeout     = 180.0  # s — TOT cutoff
id_interval = 600.0  # s — FCC ID interval (≤ 10 min)
id_pending  = 60.0   # s — arm pending ID this far before mandatory deadline

[identity]
startup_message        = ""              # message played at daemon start
initial_ids            = ["default_voice"]   # rotation; played at end of first hang
mandatory_ids          = ["default_cw"]      # rotation; played when ID interval expires
pending_id             = ""              # sneaked in at COR drop before deadline
impolite_id            = ""              # played over active QSO if deadline hits
ct_message             = "yellow_jacket"
timeout_message        = "timeout_warn"
timeout_cancel_message = ""              # played when TX resumes after timeout
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
- `tone` — synthesized tone (dual-frequency supported for DTMF-style tones)

### Station identification

The controller implements FCC Part 97 ID requirements automatically:

- **Initial ID** — played at the end of the hang following the first transmission from a quiet period (polite: waits for the QSO to end)
- **Mandatory ID** — played when the 10-minute interval expires and the TX has been active; fires between turns if the `id_pending` window is armed, or over an active QSO (`impolite_id`) if the deadline hits mid-transmission
- **Quiet period** — if no TX has occurred since the last ID, the timer expires silently with no transmission

### Squelch tail elimination

`ste_delay_ms` introduces a software audio delay (0–500 ms) between the RX filter chain and the passthrough gate. The gate still operates on real-time hardware COR/CTCSS edges. When the gate closes, the delay buffer is discarded — the FM noise burst that occurs after the user unkeys exits the ADC into the delay buffer but never reaches the TX. Set to 25–150 ms depending on your hardware's COR decoder hysteresis.

## Running as a systemd service

Install the service with the setup script — it substitutes your username and project path automatically:

```bash
bash setup.sh --service
sudo systemctl start rc

# Check status
sudo systemctl status rc
journalctl -u rc -f

# After editing code, restart to pick up changes
sudo systemctl restart rc
```

`systemd/rc.service` is a reference template with `<USER>` / `<PROJECT_DIR>` placeholders. Do not copy it directly — `setup.sh --service` generates the correctly substituted unit file.

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
