# Repeater Controller — Operator's Manual

A software-defined amateur radio repeater controller written in Python.
Runs on Raspberry Pi with real GPIO hardware, or on any machine in
simulation mode for development and testing.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Quick Start](#3-quick-start)
4. [The Interactive Shell](#4-the-interactive-shell)
5. [Configuration](#5-configuration)
6. [Messages](#6-messages)
7. [ID System (Telemetry)](#7-id-system-telemetry)
8. [Simulation](#8-simulation)
9. [Audio Testing](#9-audio-testing)
10. [The Live Controller](#10-the-live-controller)
11. [CTCSS / PL Tone System](#11-ctcss--pl-tone-system)
12. [Tone Elements](#12-tone-elements)
13. [Logging](#13-logging)
14. [Hardware Setup](#14-hardware-setup)
15. [File Reference](#15-file-reference)
16. [Appendix A: Vocabulary](#appendix-a-vocabulary)
17. [Appendix B: Standard CTCSS Tones](#appendix-b-standard-ctcss-tones)
18. [Appendix C: Troubleshooting](#appendix-c-troubleshooting)

---

## 1. Overview

This controller implements a complete repeater state machine with:

- **Six operating states**: IDLE, PENDING, ACTIVE, TAIL, TIMEOUT, TRANSMIT
- **Four access modes**: COS-only, CTCSS-only, COS+CTCSS, CTCSS-init
- **Bidirectional PENDING**: In COS+CTCSS modes, either COS or CTCSS may
  arrive first; the controller waits for the other within a configurable
  decode window
- **All-digital audio path**: CTCSS encode/decode, audio passthrough with
  adjustable gain, tone generation, and clip mixing — all in software via
  the sounddevice library
- **FCC-compliant ID system**: Initial, pending, and mandatory IDs with
  configurable message pools and round-robin rotation
- **Abstract message system**: Messages are sequences of mixed elements —
  CW (Morse), VOICE (pre-rendered PCM clips), and TONE (synthesized tones)
  can be freely combined in any order within a single message
- **Repeat gain control**: RX passthrough level can be scaled up or down
  before re-transmission, independent of the hardware input trim
- **Voice-blocks-repeat**: Optional muting of RX passthrough during VOICE
  clip playback to prevent overlap
- **Inline tone elements**: Tone elements are defined directly in messages
  with frequency, duration, and amplitude parameters — supporting dual-tone,
  silence gaps, and per-element amplitude control
- **CTCSS squelch tail elimination**: Motorola 120-degree reverse burst and
  chicken burst modes
- **GPIO abstraction**: Real Raspberry Pi GPIO or full software simulation
- **TOML configuration**: Human-readable config files, editable from the
  interactive shell or any text editor
- **Structured logging**: INFO, WARNING, and ERROR levels for operational
  monitoring
- **712 pre-rendered voice clips**: Aviation/radio vocabulary in WAV format,
  plus a `user_pcm/` directory for user-supplied custom clips

### State Machine

```
                    +----------+
         COS up     |          |  COS too short
        +---------->| PENDING  +------------+
        |  (CTCSS   |          |  (kerchunk) |
        |   modes)  +----+-----+             |
        |                | both present      |
        |                v                   |
   +----+----+    +----------+         +-----v-----+
   |         |    |          |  TOT    |           |
   |  IDLE   |    |  ACTIVE  +-------->|  TIMEOUT  |
   |         |    |          |         |           |
   +----^----+    +----+-----+         +-----------+
        |              | COS down
        |              v
        |         +----------+
        |  tail   |          |  CT (tail message)
        <---------+   TAIL   |<-- hang timer
        |  timer  |          |
        |         +----------+
        |
   +----+----+
   |TRANSMIT |  (ID, announcement)
   +---------+
```

**PENDING is bidirectional.** In `cos_ctcss` and `ctcss_init` modes,
either COS or CTCSS may arrive first. The controller enters PENDING and
waits for the other signal within the configured decode window
(`decode_time_ms`). If the window expires without both signals present,
the controller returns to IDLE. This correctly handles both wide and
tight squelch configurations.

---

## 2. Installation

### Requirements

- Python 3.11 or later
- A sound card with input and output (for live operation)
- Raspberry Pi with RPi.GPIO (for real hardware; optional for development)

### Install dependencies

```bash
cd /path/to/repeater
pip install -r requirements.txt
```

This installs:
- **numpy** — numerical array processing for audio and DSP
- **sounddevice** — cross-platform audio I/O (wraps PortAudio)

On Raspberry Pi, also install RPi.GPIO if not already present:

```bash
pip install RPi.GPIO
```

### Voice clips

The 712 pre-rendered voice clips ship in `vocab_pcm/` as 16-bit mono
WAV files at 16 kHz. No generation step is required — they are ready
to use.

To add your own voice content (custom callsigns, phrases, or
announcements), drop `.wav` files into the `user_pcm/` directory.
Files in `user_pcm/` take precedence over `vocab_pcm/` when both
contain a clip with the same name. Clip names are derived from the
filename (without the `.wav` extension, case-insensitive).

---

## 3. Quick Start

### Interactive shell (development / configuration)

```bash
python3 rc_shell.py
```

Or load an existing configuration:

```bash
python3 rc_shell.py myrepeater.toml
```

### Live controller (on-air operation)

```bash
python3 controller.py repeater.toml
```

The controller runs until Ctrl-C. All configuration must be done
beforehand via the shell or by editing the TOML file directly.

---

## 4. The Interactive Shell

The shell is the primary tool for configuring, testing, and simulating
the repeater. It uses a hierarchical menu system navigable like a Unix
filesystem.

### Navigation

| Command | Action |
|---------|--------|
| `messages` | Build, edit, and delete messages |
| `telemetry` | Assign messages to functions (ID slots, CT, timeout) |
| `configure` | System settings (timers, audio, ctcss, hardware) |
| `simulate` | State machine simulator |
| `test` | Audio / hardware testing |
| `back` or `cd ..` | Go up one level |
| `cd /` | Return to root |
| `cd configure/timers` | Jump directly to any context |
| `/configure/audio` | Absolute path as a command |
| `..` | Shorthand for `cd ..` |
| `menu` | Show current menu |

### Context-sensitive help

Press **Enter** on an empty line to see what can be configured in the
current context, with examples:

```
repeater(configure/timers)>
  Settable in 'timers'  (use 'set <field> <value>'):

    tail            ms  -- PTT hold after COS drops    (e.g. set tail 2500)
    hang            ms  -- delay before CT plays        (e.g. set hang 500)
    timeout         s   -- TOT cutoff                  (e.g. set timeout 180)
    ...
```

### Tab completion

Tab completion works at every menu level for commands, subcommands,
and navigation targets.

### Global commands

These work from any context:

| Command | Action |
|---------|--------|
| `set <field> <value>` | Set a configuration value |
| `show` | Show full configuration |
| `show messages` | List all messages with elements |
| `show msg <name>` | Show one message's elements |
| `show vocabulary` | List available VOICE clips |
| `save [file]` | Save config to TOML |
| `load <file>` | Load config from TOML |
| `quit` | Exit |

---

## 5. Configuration

All configuration is stored in a TOML file and organized into sections.

### Setting values

The `set` command uses natural-language parsing. Articles and filler
words ("to", "the", "a") are ignored:

```
set tail 2500              (bare number = ms for tail/hang/kerchunk)
set timeout 180            (bare number = seconds for timeout/id timers)
set timeout 3m             (explicit unit)
set tail 2.5s              (combined token)
set morse wpm 20
set voice volume 80
set repeat gain 1.5
set voice blocks on
set ctcss encode freq 100.0
set ctcss access ctcss_init
set mock on
```

### Configuration sections

#### Timers (`configure/timers`)

| Field | Default | Unit | Description |
|-------|---------|------|-------------|
| `tail` | 2500 ms | ms | PTT hold time after COS drops |
| `hang` | 500 ms | ms | Delay before CT (tail message) plays |
| `kerchunk` | 500 ms | ms | Minimum COS duration to activate repeater |
| `timeout` | 180 s | s | Time-out timer (TOT) -- max single transmission |
| `id_interval` | 600 s | s | Mandatory ID interval (FCC max: 10 minutes) |
| `id_pending` | 30 s | s | How early to queue a pending ID before deadline |

For `tail`, `hang`, and `kerchunk`, a bare number is interpreted as
**milliseconds**. For `timeout`, `id_interval`, and `id_pending`, a bare
number is interpreted as **seconds**. You can always override with an
explicit unit: `set tail 2.5s`, `set timeout 3m`.

#### Audio (`configure/audio`)

| Field | Default | Description |
|-------|---------|-------------|
| `morse_wpm` | 20 | Morse code speed in words per minute |
| `morse_pitch` | 700 | Morse sidetone frequency in Hz |
| `morse_volume` | 90 | Morse volume (0-100) |
| `voice_volume` | 90 | VOICE clip playback level (0-100) |
| `repeat_gain` | 1.0 | Gain multiplier for RX passthrough before TX |
| `voice_blocks_repeat` | false | Mute RX passthrough while VOICE clips play |
| `sample_rate` | 16000 | Audio sample rate in Hz |

**Repeat gain** scales the received audio before re-transmission. If
the receiver's audio output is too quiet for the transmitter and the DAC
has headroom, increase `repeat_gain` above 1.0. If the input is too hot,
reduce it below 1.0. This is independent of the hardware input trim.

**Voice blocks repeat** prevents the received audio from mixing with
VOICE clip playback. When enabled, the RX passthrough is muted for the
duration of any VOICE clip. CW and TONE elements do not block passthrough
unless they are wrapped in a VOICE clip.

#### CTCSS (`configure/ctcss`)

| Field | Default | Description |
|-------|---------|-------------|
| `access_mode` | ctcss_init | Access activation mode (see [CTCSS](#11-ctcss--pl-tone-system)) |
| `encode_freq` | 0.0 | TX PL tone frequency (0 = disabled) |
| `encode_level` | 0.15 | Encode level as fraction of peak deviation |
| `decode_freq` | 0.0 | Required RX PL tone frequency (0 = disabled) |
| `decode_time_ms` | 250 | Goertzel integration window in ms |
| `decode_threshold` | 0.015 | Detection threshold (0-1, normalized) |
| `decode_hold_ms` | 500 | Hysteresis hold time after tone loss |
| `ste_mode` | reverse_burst | Squelch tail elimination mode |
| `chicken_burst_ms` | 200 | Chicken burst: ms CTCSS stops before carrier drops |
| `reverse_burst_ms` | 250 | Reverse burst: ms duration of 120-degree phase shift |

#### Hardware (`configure/hardware`)

| Field | Default | Description |
|-------|---------|-------------|
| `mock` | true | true = simulate GPIO, false = real Pi GPIO |
| `ptt_gpio` | 17 | BCM pin number for PTT output |
| `cos_gpio` | 27 | BCM pin number for COS input |
| `cos_invert` | false | true if COS signal is active-low |

### Saving and loading

```
save                    Save to last-used file (or repeater.toml)
save myrepeater.toml    Save to specific file
load myrepeater.toml    Load from file
```

---

## 6. Messages

Messages are the central abstraction for all transmitted audio content.
A message is a named sequence of **elements** that can freely mix three
types:

| Element Type | Syntax | Description |
|--------------|--------|-------------|
| **CW** | `cw <text>` | Morse code characters |
| **VOICE** | `voice <clip>` | Pre-rendered PCM clip from `vocab_pcm/` or `user_pcm/` |
| **TONE** | `tone <freq1> <freq2> <ms> <amplitude>` | A single tone element (see [Tone Elements](#12-tone-elements)) |

A single message can contain any combination of these. For example, a
message could play a tone element, then a voice clip, then Morse code.

### Message management commands

| Command | Action |
|---------|--------|
| `msg list` | List all messages with their elements |
| `msg show <name>` | Show one message's elements in detail |
| `msg new <name>` | Create an empty message |
| `msg add <name> cw <text>` | Append a CW (Morse) element |
| `msg add <name> voice <clip>` | Append a VOICE clip element |
| `msg add <name> tone <f1> <f2> <ms> <amp>` | Append a TONE element |
| `msg clear <name>` | Remove all elements from a message |
| `msg edit <name>` | Interactive element editor (add/delete elements) |
| `msg delete <name>` | Delete a message and remove from all slots |

### Examples

Create a mixed CW + VOICE ID message:

```
msg new my_id
msg add my_id voice THIS
msg add my_id voice IS
msg add my_id cw W1AW
msg add my_id voice REPEATER
```

Create a message that plays a tone pip followed by a voice clip:

```
msg new post_id
msg add post_id tone 1000 0 80 0.8
msg add post_id voice FREQUENCY
msg add post_id voice ONE
msg add post_id voice FOUR
msg add post_id voice SIX
msg add post_id voice POINT
msg add post_id voice NINE
msg add post_id voice ZERO
```

Shorthand for single-element messages (via the `id` command):

```
id add my_cw cw W1AW/R
id add my_voice voice REPEATER
id add my_ct tone 1000 0 80 0.8
```

### TOML format

Messages are stored in the configuration file under `[messages.<name>]`:

```toml
[messages.my_id]
elements = [
    {type = "cw", text = "W1AW"},
    {type = "voice", clip = "REPEATER"},
    {type = "tone", freq1 = 1000, freq2 = 0, ms = 80, amp = 0.8},
]
```

### Future element types

- **time** — System clock readback (day, date, time). The element type
  is recognized but not yet implemented. When completed, this will
  generate VOICE clips for the current time, date, day of week, day of
  month, and month of year from the system clock.

Other planned message types include timeout messages, OTA telemetry,
tail messages, and error-level log readback.

---

## 7. ID System (Telemetry)

The controller implements FCC-compliant station identification with
three ID types and two special message slots. Message assignments are
managed in the `telemetry` menu.

### ID types

| Type | When it fires |
|------|---------------|
| **Initial** | First activation after the repeater has been idle longer than `id_interval` |
| **Pending** | `id_pending` seconds before the mandatory deadline, giving the controller a chance to ID during a pause |
| **Mandatory** | Every `id_interval` seconds (FCC requires at least every 10 minutes during operation) |

### Special message slots

| Slot | When it fires |
|------|---------------|
| **CT** | Courtesy tone (tail message) — plays when the hang timer expires |
| **Timeout** | When the time-out timer (TOT) expires |

### Rotation

Each ID type (initial, pending, mandatory) has a rotation list.
Messages in the list are played round-robin each time that type fires.
This lets you alternate between different ID styles automatically.

### Managing rotations

The `assign` and `unassign` commands are top-level commands available
from any context:

```
assign my_cw mandatory            Add to mandatory rotation
assign my_voice pending           Add to pending rotation
assign my_cw initial              Add to initial rotation
assign my_ct ct                   Set as courtesy tone (tail message)
assign timeout_warn timeout       Set as timeout message
unassign my_cw mandatory          Remove from mandatory rotation
unassign my_cw                    Remove from all slots
```

### Example setup

```
msg new main_cw
msg add main_cw cw W1AW/R

msg new voice_id
msg add voice_id voice THIS
msg add voice_id voice IS
msg add voice_id cw W1AW
msg add voice_id voice REPEATER

msg new short_cw
msg add short_cw cw W1AW

assign main_cw mandatory
assign voice_id mandatory
assign short_cw pending
assign main_cw initial
```

With this setup:
- Mandatory IDs alternate between a full CW ID and a mixed voice+CW ID
- Pending IDs use the short CW ID
- Initial IDs use the full CW ID

The system does not use a "callsign" field. Your callsign is simply
the content of whatever CW and VOICE elements you put in your messages.

---

## 8. Simulation

The simulator models the full repeater state machine without any
hardware. Use it to verify your configuration and understand the
controller's behavior.

### Entering simulation mode

```
simulate
```

### Simulation commands

These commands work from the simulate menu or from any context:

| Command | Action |
|---------|--------|
| `cos up` | Simulate carrier detect (radio keyed) |
| `cos down` | Simulate carrier drop (radio unkeyed) |
| `ctcss on` | Simulate CTCSS tone detected |
| `ctcss off` | Simulate CTCSS tone lost |
| `dtmf <digit>` | Simulate DTMF digit received |
| `advance <seconds>` | Advance simulated clock (fires pending timers) |
| `id` | Fire mandatory ID now |
| `id initial` | Fire initial ID now |
| `id pending` | Fire pending ID now |
| `status` | Show current simulator state |
| `log` | Show full event timeline |
| `reset` | Reset simulator to IDLE |

### Example: COS arrives first (wide squelch)

```
repeater(simulate)> cos up
  [00:00]  COS first -- waiting for CTCSS (250 ms window)
  [00:00]  State: IDLE -> PENDING  [PENDING]

repeater(simulate)> ctcss on
  [00:00]  CTCSS confirmed (100.0 Hz)
  [00:00]  PTT ON  <- transmitter keyed
  [00:00]  State: PENDING -> ACTIVE  [ACTIVE]
```

### Example: CTCSS arrives first (tight squelch)

```
repeater(simulate)> ctcss on
  [00:00]  CTCSS confirmed (100.0 Hz)
  [00:00]  CTCSS first -- waiting for COS (250 ms window)
  [00:00]  State: IDLE -> PENDING  [PENDING]

repeater(simulate)> cos up
  [00:00]  PTT ON  <- transmitter keyed
  [00:00]  State: PENDING -> ACTIVE  [ACTIVE]
```

### Example: Full QSO cycle

```
repeater(simulate)> cos up
repeater(simulate)> ctcss on
  ... (ACTIVE)

repeater(simulate)> advance 5
  Clock advanced 5.0s -- no timer events fired

repeater(simulate)> cos down
  [00:05]  State: ACTIVE -> TAIL  [TAIL]
  [00:05]  Tail timer: 2500 ms

repeater(simulate)> advance 0.5
  [00:05]  CT: playing message 'hang_ct'
  [00:05]  > AUDIO: TONE: 'default'

repeater(simulate)> advance 3
  [00:08]  STE: 120 degree reverse burst (250 ms)
  [00:08]  PTT OFF <- transmitter unkeyed
  [00:08]  State: TAIL -> IDLE  [IDLE]
```

---

## 9. Audio Testing

The test menu plays real audio through your sound card. Use it to
verify Morse, VOICE clips, and courtesy tones sound correct.

| Command | Action |
|---------|--------|
| `play id` | Play next mandatory ID (advances rotation) |
| `play id <name>` | Play a named message directly |
| `play id initial` | Play next initial ID |
| `play morse <text>` | Play arbitrary Morse text |
| `play voice <clip>` | Play a VOICE clip by name |
| `play ct` | Play the configured courtesy tone (tail message) |
| `show vocabulary` | List all available VOICE clips |
| `gpio` | Show GPIO pin assignments |

### Adding custom voice clips

Drop `.wav` files into `user_pcm/` to add your own voice content.
Requirements:
- **Format**: 16-bit mono WAV
- **Sample rate**: 16 kHz recommended (other rates are resampled automatically)
- **Naming**: The filename (without `.wav`) becomes the clip name.
  For example, `user_pcm/MYCALL.wav` is referenced as `voice MYCALL`.

Clips in `user_pcm/` override same-named clips in `vocab_pcm/`.
Use `show vocabulary` to see all available clips and their source directory.

---

## 10. The Live Controller

The live controller (`controller.py`) runs the full state machine with
real audio and GPIO hardware.

### Starting

```bash
python3 controller.py repeater.toml
```

Or with default configuration (mock hardware):

```bash
python3 controller.py
```

### What it does

1. Opens a duplex audio stream (sounddevice)
2. Configures GPIO pins (PTT output, COS input)
3. Runs the state machine in an asyncio event loop
4. Monitors COS edges via GPIO interrupts
5. Decodes CTCSS from the RX audio stream in real time
6. Encodes CTCSS onto the TX audio stream
7. Manages all timers (tail, hang, timeout, ID)
8. Plays messages (mixed CW, VOICE, TONE elements)
9. Applies repeat gain to RX passthrough
10. Optionally mutes passthrough during VOICE playback
11. Detects and warns on ADC clipping

### Stopping

Press **Ctrl-C** to shut down gracefully.

---

## 11. CTCSS / PL Tone System

### Access modes

| Mode | Behavior |
|------|----------|
| `cos` | COS alone activates the repeater. CTCSS is ignored. |
| `ctcss` | CTCSS alone controls activation. COS is informational. |
| `cos_ctcss` | Both COS and CTCSS must be present. Either may arrive first. CTCSS loss forces tail even if COS remains. |
| `ctcss_init` | Both COS and CTCSS required to initially activate. Either may arrive first. Once active, COS alone maintains; CTCSS loss is logged but does not force tail. |

**Bidirectional PENDING**: In `cos_ctcss` and `ctcss_init` modes, the
controller does not assume COS will always arrive before CTCSS. With a
tight squelch, CTCSS may be decoded before the squelch opens. The
controller handles both orderings identically: whichever signal arrives
first puts the machine in PENDING and starts the decode window timer.
The other signal must arrive before the window expires or the controller
returns to IDLE.

### CTCSS decode

The decoder uses a **Goertzel algorithm** (single-frequency DFT) to
detect the sub-audible tone in the RX audio stream:

- **Integration window** (`decode_time_ms`): How many milliseconds of
  audio to analyze per detection cycle. Shorter = faster but less
  accurate. 250 ms is typical.
- **Threshold** (`decode_threshold`): Normalized power level (0-1) above
  which the tone is considered present. A pure tone yields 1.0.
  Default 0.015 works well for real-world signals.
- **Hold time** (`decode_hold_ms`): How long to maintain "tone present"
  after the last detection, preventing false drops during brief fades.

### Squelch tail elimination (STE)

When the repeater drops from TAIL to IDLE, it can suppress the squelch
burst heard on receivers:

| Mode | Description |
|------|-------------|
| `none` | No STE. Receivers hear a brief squelch burst. |
| `reverse_burst` | Motorola standard: shift CTCSS phase by 120 degrees for a brief burst before dropping carrier. Receivers detect the phase shift and mute before carrier drops. |
| `chicken_burst` | Stop the CTCSS tone a few hundred ms before dropping carrier. Receivers lose tone lock and mute before carrier drops. |

---

## 12. Tone Elements

Tone elements produce synthesized audio within a message. Each tone
element is defined inline with four parameters:

| Parameter | Description |
|-----------|-------------|
| `freq1` | Primary frequency in Hz (0 = silence) |
| `freq2` | Secondary frequency in Hz (0 = single tone) |
| `ms` | Duration in milliseconds |
| `amp` | Amplitude from 0.0 (silent) to 1.0 (full scale) |

### Adding tone elements to messages

Use the `msg add` command with all four parameters:

```
msg add my_message tone 1000 0 80 0.8
```

To create a silence gap between tones, set freq1 to 0:

```
msg add my_message tone 0 0 30 0.0
```

When both freq1 and freq2 are non-zero, a dual-frequency tone is
produced (both frequencies are summed). This is useful for DTMF-style
chords:

```
msg add my_message tone 697 1209 150 0.75
```

### TOML format

In the configuration file, tone elements use explicit field names:

```toml
{type = "tone", freq1 = 1000, freq2 = 0, ms = 80, amp = 0.8}
```

### Classic tone patterns

Courtesy tones and alert sequences are built by adding multiple tone
elements to a message. Here are some common patterns:

**Ascending two-pip** (classic courtesy tone):

```
msg new my_ct
msg add my_ct tone 1000 0 50 0.8
msg add my_ct tone 0 0 30 0.0
msg add my_ct tone 1200 0 50 0.8
```

**Descending three-tone alert** (timeout warning):

```
msg new my_warn
msg add my_warn tone 1200 0 60 0.9
msg add my_warn tone 0 0 20 0.0
msg add my_warn tone 1000 0 60 0.9
msg add my_warn tone 0 0 20 0.0
msg add my_warn tone 800 0 60 0.9
```

**Rising three-tone**:

```
msg new rising
msg add rising tone 800 0 40 0.8
msg add rising tone 0 0 20 0.0
msg add rising tone 1000 0 40 0.8
msg add rising tone 0 0 20 0.0
msg add rising tone 1200 0 40 0.8
```

**Single pip**:

```
msg new pip
msg add pip tone 1000 0 80 0.8
```

Tone elements can be freely mixed with CW and VOICE elements in the
same message. The CT slot is typically assigned a message containing
tone elements:

```
assign my_ct ct
```

---

## 13. Logging

The controller uses Python's standard `logging` module with three
severity levels:

### INFO — Routine operations

All normal state transitions and actions:
- State changes (IDLE -> PENDING -> ACTIVE -> TAIL -> IDLE)
- PTT on/off
- COS up/down with duration
- CTCSS detected/lost
- ID timer fires and message playback
- Clip playback (CW, VOICE, TONE)
- Audio stream start/stop

### WARNING — Service-impacting events

Things that affect quality but don't prevent operation:
- ADC clipping on RX input (rate-limited to once per 5 seconds)
- CTCSS decode window timeout (PENDING expired without both signals)
- Empty message elements
- Missing voice directories

### ERROR — Operational failures

Problems that prevent a message or function from working correctly:
- Message not found in pool when ID fires
- Unknown element type in a message
- Missing `text`, `clip`, or `tone` field in an element
- Courtesy tone name not found
- Voice clip not found in any directory
- Morse playback subprocess failure

### Log format

```
HH:MM:SS  LEVEL    module  message
14:30:01  INFO     controller  State: IDLE -> ACTIVE
14:30:01  INFO     controller  PTT ON
14:35:01  INFO     controller  ID (mandatory) -> 'main_cw'
14:35:01  INFO     controller  CW: 'W1AW/R'  20 WPM  700 Hz
14:42:15  WARNING  audio       ADC clipping on RX input -- peak 0.991 FS
```

---

## 14. Hardware Setup

### Raspberry Pi GPIO wiring

| Function | Default Pin | Direction | Description |
|----------|-------------|-----------|-------------|
| PTT | GPIO 17 | Output | Drives the transmitter PTT line |
| COS | GPIO 27 | Input | Receives carrier-operated squelch signal |

Pin numbers use **BCM numbering** (not physical pin numbers).

### COS polarity

Most radios drive COS high when a signal is present. If your radio
drives COS low on carrier detect, set:

```
set cos invert on
```

### Mock mode

For development without a Pi:

```
set mock on       Simulate GPIO (no real hardware needed)
set mock off      Use real RPi.GPIO (Pi only)
```

### Audio connections

The controller uses the system's default audio input and output devices
via sounddevice/PortAudio:

- **Input**: Connect receiver audio (discriminator tap or speaker output)
- **Output**: Connect to transmitter audio input (mic input or aux)

Audio runs at 16 kHz sample rate with 20 ms block size for low latency.

If the receiver's audio level doesn't match the transmitter's needs,
adjust `repeat_gain`:

```
set repeat gain 1.5     Boost RX audio 50% before re-transmission
set repeat gain 0.8     Reduce RX audio 20%
```

---

## 15. File Reference

### Core modules

| File | Purpose |
|------|---------|
| `controller.py` | Live asyncio state machine — the on-air controller |
| `rc_shell.py` | Interactive configuration and simulation shell |
| `rc_config.py` | Configuration dataclass, TOML load/save, set parser |
| `audio_engine.py` | Sounddevice duplex stream, clip mixer, CTCSS pipeline |
| `ctcss.py` | CTCSS encoder, Goertzel decoder, STE helpers |
| `hardware.py` | GPIO abstraction (MockHardware / RealHardware) |
| `tones.py` | Courtesy tone renderer and player |
| `morse.py` | Morse code audio generator |

### Voice clip directories

| Path | Purpose |
|------|---------|
| `vocab_pcm/` | Built-in voice clips (712 WAV files, 16-bit mono, 16 kHz) |
| `user_pcm/` | User-supplied voice clips (takes precedence over vocab_pcm) |

### Configuration

| Path | Purpose |
|------|---------|
| `repeater.toml` | Configuration file (created/updated by `save`) |

### Support files

| File | Purpose |
|------|---------|
| `requirements.txt` | Python package dependencies |
| `MANUAL.md` | This manual |

---

## Appendix A: Vocabulary

The `vocab_pcm/` directory contains 712 pre-rendered voice clips
(the vocabulary). These can be used in VOICE elements within messages.
Clip names are case-insensitive (referenced in uppercase internally).

To use a clip in a message, the corresponding `.wav` file must exist in
either `vocab_pcm/` or `user_pcm/`. Missing clips are logged as errors
and skipped. Use `show vocabulary` in the shell to list all available
clips.

```
A           ABORT       ABOUT       ABOVE       ACCELERATED
ACKNOWLEDGE ACTION      ADJUST      ADVANCED    ADVISE
AERIAL      AFFIRMATIVE AFTERNOON   AIR         AIRCRAFT
AIRPORT     AIRSPEED    ALERT       ALL         ALOFT
ALPHA       ALTERNATE   ALTIMETER   ALTITUDE    AMATEUR
AMPS        AND         ANSWER      APPROACH    APPROACHES
APRIL       AREA        ARRIVAL     AS          AT
AUGUST      AUTO        AUTOMATIC   AUTOPILOT   AUXILLIARY
B           BAND        BANK        BASE        BATTERY
BELOW       BETWEEN     BLOWING     BOARD       BOOST
BRAKE       BRAVO       BREAK       BREAKING    BROKEN
BUTTON      BY          C           CABIN       CALIBRATE
CALL        CALLING     CALM        CANCEL      CAUTION
CEILING     CELSIUS     CENTER      CHANGE      CHARLIE
CHECK       CIRCUIT     CLEAR       CLEARANCE   CLIMB
CLOCK       CLOSE       CLOSED      CLUB        CODE
COME        COMPLETE    COMPUTER    CONDITION   CONNECT
CONTACT     CONTROL     CONVERGING  COUNT       COURSE
COWL        CROSSWIND   CRYSTALS    CURRENT     CYCLE
CYLINDER    D           DANGER      DAYS        DECEMBER
DECREASE    DECREASING  DEGREE      DEGREES     DELTA
DEPARTURE   DEVICE      DIAL        DIRECTION   DISPLAY
DIVIDED     DOOR        DOORS       DOWN        DOWNWIND
DRIVE       DRIZZLE     DUST        E           EAST
ECHO        EIGHT       EIGHTEEN    EIGHTY      ELECTRICIAN
ELEVATION   ELEVEN      EMERGENCY   ENGINE      ENTER
EQUAL       EQUALS      ERROR       ESTIMATED   EVACUATE
EVACUATION  EVENING     EXIT        EXPECT      F
FAIL        FAILURE     FARAD       FAST        FEBRUARY
FEET        FIELD       FIFTEEN     FIFTY       FILED
FINAL       FIRE        FIRST       FIVE        FLAPS
FLIGHT      FLOW        FOG         FOR         FOUR
FOURTEEN    FOURTH      FOURTY      FOXTROT     FREEDOM
FREEZING    FREQUENCY   FRIDAY      FROM        FRONT
FUEL        FULL        G           GALLEY      GALLONS
GAP         GAS         GATE        GAUGE       GEAR
GET         GLIDE       GO          GOLF        GOOD
GRAIN       GREAT       GREEN       GREENWICH   GROUND
GUST        H           HAIL        HALF        HAM
HAMFEST     HAVE        HAZARDOUS   HAZE        HEADING
HEAT        HEAVY       HELP        HENRY       HERTZ
HIGH        HOLD        HOME        HOTEL       HOUR
HOURS       HUNDRED     I           ICE         ICING
IDENTIFY    IDLE        IFR         IGNITE      IGNITION
ILS         IMMEDIATELY IN          INBOUND     INCH
INCREASE    INCREASING  INDIA       INDICATED   INFLIGHT
INFORMATION INNER       INSPECTOR   INSTRUMENT  INSTRUMENTS
INTRUDER    IS          IT          J           JANUARY
JULIET      JULY        JUNE        K           KEY
KILO        KNOTS       L           LAND        LANDING
LATE        LAUNCH      LEAN        LEFT        LEG
LESS        LEVEL       LIGHT       LIGHTS      LIMA
LINE        LINK        LIST        LOCALIZER   LOCK
LONG        LOOK        LOW         LOWER       LUNCH
M           MACHINE     MAGNETOS    MAINTAIN    MANUAL
MARCH       MARKER      MAY         MAYDAY      MEAN
MEASURE     MEASURED    MEETING     MEGA        MESSAGES
METER       MICRO       MIDDLE      MIDPOINT    MIKE
MILE        MILES       MILL        MILLI       MILLION
MINUS       MINUTES     MIST        MIXTURE     MOBILE
MODERATE    MONDAY      MONTH       MORE        MORNING
MOTOR       MOVE        MOVING      MUCH        N
NEAR        NEGATIVE    NET         NEW         NEXT
NIGHT       NINE        NINER       NINETEEN    NINETY
NO          NOR         NORTH       NORTHEAST   NORTHWEST
NOT         NOVEMBER    NUMBER      O           OBSCURED
OCLOCK      OCTOBER     OF          OFF         OHIO
OHMS        OIL         ON          ONE         OPEN
OPERATION   OPERATOR    OSCAR       OTHER       OUT
OUTER       OVER        OVERCAST    OVERSPEED   P
PAPA        PARTIALLY   PASS        PASSED      PAST
PATCH       PATH        PAUSE       PELLETS     PER
PERCENT     PHASE       PHONE       PICO        PLAN
PLEASE      PLUS        POINT       POLICE      POSITION
POWER       PRACTICE    PRESS       PRESSURE    PRIVATE
PROBE       PROGRAMMING PULL        PUMPS       PUSH
Q           QUEBEC      R           RADAR       RADIAL
RADIO       RADIOS      RAIN        RAISE       RANGE
RATE        READY       REAR        RECEIVE     RED
REFUELLING  RELEASE     REMARK      REMOTE      REPAIR
REPEAT      REPEATER    RICH        RIG         RIGHT
ROAD        ROGER       ROMEO       ROUTE       S
SAFE        SAND        SATURDAY    SCATTERED   SECOND
SECONDS     SECURITY    SELECT      SEPTEMBER   SEQUENCE
SERVICE     SET         SEVEN       SEVENTEEN   SEVENTY
SEVER       SEVERE      SHORT       SHOWERS     SHUT
SIDE        SIERRA      SIGHT       SIX         SIXTEEN
SIXTY       SLEET       SLOPE       SLOW        SMOKE
SNOW        SOUTH       SOUTHEAST   SOUTHWEST   SPEED
SPOILERS    SPRAY       SQUAWK      STABILISER  STALL
START       STOP        STORM       SUNDAY      SWITCH
SYSTEM      T           TANGO       TANK        TARGET
TAXI        TELEPHONE   TEMPERATURE TEN         TERMINAL
TEST        THANK YOU   THAT        THE         THIN
THINLY      THIRD       THIRTEEN    THIRTY      THIS
THOUSAND    THREE       THUNDERSTORM THURSDAY   TIME
TIMER       TIMES       TO          TODAY       TOMORROW
TONIGHT     TOOL        TORNADO     TOUCHDOWN   TOWER
TRAFFIC     TRANSMIT    TRIM        TRUE        TUESDAY
TURBULENCE  TURN        TWELVE      TWENTY      TWO
U           UNDER       UNDERCARRIAGE UNICOM    UNIFORM
UNIT        UNLIMITED   UNTIL       UP          USE
V           VACUUM      VALLEY      VALVE       VARIABLE
VECTORS     VERIFY      VFR         VICTOR      VISIBILITY
VOLTS       VOR         W           WAIT        WAKE
WARNING     WATCH       WATTS       WAY         WEATHER
WEDNESDAY   WEIGHT      WELCOME     WEST        WHISKEY
WHITE       WILL        WIND        WINDOWS     WRONG
X           XRAY        Y           YANKEE      YELLOW
YESTERDAY   YOU         YOUR        Z           ZERO
ZONE        ZULU
```

Not every variant is listed above; see the `vocab_pcm/` directory for
the exact filenames available, or use `show vocabulary` in the shell.

To add clips not in this list (custom callsigns, local phrases, etc.),
record them as 16-bit mono WAV files and place them in `user_pcm/`.

---

## Appendix B: Standard CTCSS Tones

Standard EIA/TIA-603 CTCSS (PL) tone frequencies in Hz:

```
 67.0   71.9   74.4   77.0   79.7   82.5   85.4   88.5
 91.5   94.8   97.4  100.0  103.5  107.2  110.9  114.8
118.8  123.0  127.3  131.8  136.5  141.3  146.2  151.4
156.7  162.2  167.9  173.8  179.9  186.2  192.8  203.5
210.7  218.1  225.7  233.6  241.8  250.3
```

Set encode and decode to the same frequency for a standard PL repeater:

```
set ctcss encode freq 100.0
set ctcss decode freq 100.0
```

---

## Appendix C: Troubleshooting

### "No module named 'sounddevice'"

Install the audio library:

```bash
pip install sounddevice
```

On Linux you may also need PortAudio:

```bash
sudo apt install libportaudio2
```

### "RPi.GPIO is not available"

You're running on a non-Pi machine. Use mock mode:

```bash
# In the shell:
set mock on

# Or in the TOML file:
# [hardware]
# mock = true
```

### CTCSS not detecting

- Verify the frequency matches your radio's PL tone exactly
- Try lowering `decode_threshold` (e.g. from 0.015 to 0.010)
- Increase `decode_time_ms` for more reliable detection (at the cost of
  latency)
- Check that audio input levels are adequate

### CTCSS arriving before COS (tight squelch)

This is normal and fully supported. In `cos_ctcss` and `ctcss_init`
modes, the controller handles either signal arriving first. If you see
"CTCSS first -- waiting for COS" in the log, the system is working
correctly. If the COS doesn't arrive within `decode_time_ms`, the
controller returns to IDLE. Consider increasing `decode_time_ms` if
this happens frequently with a tight squelch.

### Courtesy tone (CT) not playing

- Verify the message exists: `msg show hang_ct`
- Check the CT assignment: `msg list` or `telemetry list`
- Verify the hang timer is non-zero: the CT plays after
  `hang` milliseconds following COS drop

### ID not firing in simulation

- Check that messages exist: `msg list`
- Verify messages are assigned to a rotation: `assign <name> mandatory`
- Use `advance` to move the clock forward: `advance 601`

### Voice clip not found

- Use `show vocabulary` to see available clips
- Verify the clip name matches a `.wav` filename (case-insensitive)
- Check both `vocab_pcm/` and `user_pcm/` directories
- Ensure `.wav` files are 16-bit mono format

### Audio clipping or distortion

The audio engine clips output to +/- 1.0 to prevent DAC overload. If
you hear distortion:

- Lower `repeat_gain` if RX passthrough is too hot
- Lower `encode_level` for CTCSS (default 0.15 = 15% of peak)
- Lower `morse_volume` or `voice_volume`
- Check that input audio isn't already clipped (look for ADC clipping
  warnings in the log)

### Repeat audio too quiet / too loud

Adjust the repeat gain:

```
set repeat gain 1.5     Boost 50%
set repeat gain 0.8     Reduce 20%
set repeat gain 1.0     Unity (default)
```

The ADC clipping detector warns if input peaks exceed 0.98 FS. If you
see these warnings, reduce the hardware input level or lower
`repeat_gain`.
