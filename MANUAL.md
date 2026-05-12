# Repeater Controller — Operator's Manual

---

## Design philosophy: shell-first configuration

The shell (`shell.py`) is the **primary configuration interface**. The goal —
fully realized by v1.0 — is that you never need to hand-edit `repeater.toml`
after the initial site setup. Everything reachable by `set` or `msg` in the
shell takes effect immediately on the running daemon. A `save` command will
write live configuration back to `repeater.toml`, completing the round-trip.

For now, use `repeater.toml` for:
- Hardware device paths (`hidraw_device`, `audio_device`)
- Initial ID message content (your callsign in `initial_ids`/`mandatory_ids`)
- Identity slot assignments (`ct_message`, startup/timeout messages)

Everything else — timers, audio levels, message content — is tunable live
through the shell without restarting the daemon.

---

## 1. Overview

This controller implements a complete repeater state machine targeting
Raspberry Pi 3+ with CM108/CM119-family USB audio/GPIO interfaces.

- **Five operating states**: IDLE, PENDING, ACTIVE, TAIL, TIMEOUT
- **Two access modes**: COR-only (`cor`) or COR+CTCSS (`cor_ctcss`)
- **All-digital audio path**: FM de-emphasis, 300 Hz HPF, audio passthrough
  with adjustable gain, tone generation, and clip mixing via sounddevice
- **Squelch tail elimination**: Configurable software audio delay (0–500 ms)
  gates the noise crash before it reaches the TX
- **FCC-compliant ID system**: Initial, pending, mandatory, and impolite IDs
  with rotation lists and polite/impolite scheduling
- **Abstract message system**: Messages are sequences of mixed elements —
  CW (Morse), VOICE (pre-rendered PCM clips), and TONE (synthesized tones)
  freely combined in any order
- **Repeat gain control**: RX passthrough level scalable independently of
  hardware input trim
- **TOML configuration**: Human-readable config file; the shell is the
  primary interface for live changes
- **Structured logging**: journald-friendly; configurable log level
- **712 pre-rendered voice clips**: Aviation/radio vocabulary in WAV format,
  plus a `user_pcm/` directory for user-supplied custom clips

### State Machine

```
                  +----------+
       COR up     |          |  COR too short
      +---------->| PENDING  +--------+
      | (cor_ctcss|          |(kerchunk)
      |  mode)    +----+-----+        |
      |                | both present |
      |                v              |
 +----+----+    +----------+    +-----v------+
 |         |    |          | TOT|            |
 |  IDLE   |    |  ACTIVE  +--->|  TIMEOUT   |
 |         |    |          |    |            |
 +----^----+    +----+-----+    +------------+
      |              | COR down
      |              v
      |         +----------+
      |  hang   |          |  CT plays after ct_delay
      <---------+   TAIL   |
        timer   |          |
                +----------+
```

**PENDING (cor_ctcss mode only)**: In `cor_ctcss` mode, either COR or CTCSS
may arrive first. The controller enters PENDING and waits for the other signal
within a 500 ms window. If the window expires without both signals present,
the controller returns to IDLE.

**TIMEOUT**: When the time-out timer (TOT) fires, the RX gate closes and the
controller plays the timeout message and drops PTT. The repeater ignores any
incoming signals until the offending COR drops, at which point it transmits a
timeout-cancel message (if configured) and returns to TAIL → IDLE.

---

## 2. Installation

### Setup script

Run the setup script as the user who will operate the repeater:

```bash
bash setup.sh
```

This installs system packages (`python3-dev`, `python3-venv`, `libportaudio2`),
the udev rule for `/dev/hidraw*` access, adds your user to the `audio` group,
and creates the Python venv. Everything requiring root uses sudo internally;
everything else stays inside the project directory.

After the script completes, unplug and replug the CM119, then open a new
terminal (or run `newgrp audio`) for the group change to take effect.

To also install and enable the systemd service:

```bash
bash setup.sh --service
```

### Verifying hardware access

```bash
# Device should appear (vendor 0d8c)
lsusb | grep 0d8c

# hidraw node should be group-writable by audio
ls -la /dev/hidraw*
```

---

## 3. Quick Start

### Start the daemon

```bash
python daemon.py repeater.toml
```

The daemon opens the CM119, starts the audio stream, and serves the Unix
socket. It runs until stopped (`Ctrl-C` or `shutdown` from the shell).

### Connect the shell

In a second terminal (or another SSH session):

```bash
python shell.py repeater.toml
```

The shell connects to the running daemon, subscribes to push events, and
displays the current state. Multiple shell instances can connect simultaneously.

### Run as a systemd service

```bash
sudo systemctl start rc
sudo systemctl status rc
journalctl -u rc -f

# After editing code, reload:
sudo systemctl restart rc
```

---

## 4. The Shell

The shell is the primary interface for monitoring and configuring the running
daemon. All commands take effect immediately — no restart needed.

```
rc> state               Show repeater state (IDLE/ACTIVE/TAIL/TIMEOUT)
rc> config              Show current configuration
rc> set <field> <val>   Change a config value live (see set examples below)
rc> play <message>      Trigger a named message (e.g. "play default_cw")
rc> ptt on|off          Force PTT on or off
rc> subscribe           Stream state-change events (Ctrl-C to stop)
rc> reload              Reload repeater.toml from disk
rc> msg ...             Manage messages (see Messages section)
rc> shutdown            Stop the daemon
rc> help                Command reference
rc> quit                Disconnect
```

### Set examples

The `set` command uses natural-language parsing — articles and filler words
are ignored. A bare number for timer fields uses the natural unit (ms for
hang/ct_delay/kerchunk, seconds for timeout/id_interval/id_pending).

```
set hang 2500             2500 ms hang time
set hang 2.5s             same as above
set ct delay 500          500 ms CT delay
set timeout 3m            3-minute TOT
set timeout 180           180 seconds TOT
set id interval 10m       10-minute mandatory ID interval
set morse wpm 20          Morse code speed
set morse pitch 700       Morse sidetone frequency
set morse level 0.9       CW amplitude (0.0–1.0)
set impolite level 0.3    CW level for mid-QSO impolite IDs
set voice level 0.9       Voice clip amplitude
set repeat gain 1.0       RX passthrough gain multiplier
set voice blocks repeat on   Mute RX during VOICE clips
set rx hpf on             Enable 300 Hz high-pass filter
set rx deemphasis on      Enable FM de-emphasis on RX
set tx preemphasis off    Disable FM pre-emphasis on TX
set pre message pad 100   100 ms dead air after PTT-on before CW/voice
set post message pad 50   50 ms dead air after audio drains before PTT-off
set ste 50                50 ms squelch tail elimination delay
set ctcss access cor      COR-only access mode
set ctcss access cor_ctcss  Both COR and CTCSS required
set log level DEBUG       Increase log verbosity
```

---

## 5. Configuration

Configuration is stored in `repeater.toml` (gitignored — copy from
`repeater.toml.sample` for initial setup). The shell is the primary interface
for live changes; use `reload` to pull TOML changes into a running daemon.

### `[daemon]`

| Field | Default | Description |
|-------|---------|-------------|
| `socket_path` | `"run/rc.sock"` | Unix socket path (relative = resolved from config directory) |
| `log_level` | `"INFO"` | Python logging level: DEBUG, INFO, WARNING, ERROR |

### `[hardware]`

| Field | Default | Description |
|-------|---------|-------------|
| `hidraw_device` | `""` | HID device path; empty = auto-detect by USB VID 0x0d8c |
| `audio_device` | `""` | sounddevice name; empty = system default |
| `cor_active_low` | `true` | true = bit clear means COR active (AllStar convention) |
| `ctcss_active_low` | `true` | true = bit clear means CTCSS active |

### `[audio]`

| Field | Default | Description |
|-------|---------|-------------|
| `sample_rate` | `48000` | Audio sample rate in Hz (CM119 supports 44100 and 48000) |
| `rx_hpf` | `true` | 300 Hz high-pass filter on RX (removes sub-audible CTCSS/hum) |
| `rx_deemphasis` | `true` | FM de-emphasis on received audio |
| `tx_preemphasis` | `false` | FM pre-emphasis on transmitted audio |
| `repeat_gain` | `1.0` | RX passthrough gain multiplier (1.0 = unity) |
| `morse_wpm` | `20` | Morse code speed in words per minute |
| `morse_pitch` | `700` | Morse sidetone frequency in Hz |
| `morse_level` | `0.9` | CW amplitude (0.0–1.0) |
| `impolite_morse_level` | `0.3` | CW level when IDing over an active QSO |
| `voice_level` | `0.9` | VOICE clip amplitude (0.0–1.0) |
| `voice_blocks_repeat` | `false` | Mute RX passthrough while VOICE clips play |
| `pre_message_ms` | `0` | Dead air after PTT-on before CW/voice starts |
| `post_message_ms` | `0` | Dead air after audio drains before PTT-off |
| `ste_delay_ms` | `0` | Squelch tail elimination delay in ms (0 = disabled, max ~500) |

### `[ctcss]`

| Field | Default | Description |
|-------|---------|-------------|
| `access_mode` | `"cor"` | `"cor"` = COR alone; `"cor_ctcss"` = both COR and CTCSS required |

### `[timers]`

| Field | Default | Unit | Description |
|-------|---------|------|-------------|
| `hang` | `2.5` | s | PTT holdoff after CT ("hangup time") |
| `ct_delay` | `0.5` | s | Delay from RX stream loss to courtesy tone |
| `kerchunk` | `0.5` | s | Minimum COR hold to respond (anti-kerchunk) |
| `timeout` | `180.0` | s | Time-out timer (TOT) transmit cutoff |
| `id_interval` | `600.0` | s | Mandatory ID interval (FCC max: 10 minutes) |
| `id_pending` | `60.0` | s | Arm pending ID this far before the mandatory deadline |

### `[identity]`

| Field | Default | Description |
|-------|---------|-------------|
| `startup_message` | `""` | Message played at daemon start; leave empty for none |
| `initial_ids` | `[]` | Rotation list; played at end of hang after first TX from quiet period |
| `mandatory_ids` | `[]` | Rotation list; played when ID interval expires |
| `pending_id` | `""` | Single message; sneaked in at COR drop before mandatory deadline |
| `impolite_id` | `""` | Single message; played over active QSO if deadline hits mid-transmission |
| `ct_message` | `""` | Courtesy tone message; played after `ct_delay` on COR drop |
| `timeout_message` | `""` | Played when TOT expires |
| `timeout_cancel_message` | `""` | Played when TX resumes after a timeout; leave empty for none |

---

## 6. Messages

Messages are the central abstraction for all controller audio output. A
message is a named sequence of **elements**, each of which is one of:

| Type | TOML example | Description |
|------|--------------|-------------|
| `cw` | `{type = "cw", text = "W1AW/R"}` | Morse code rendered in-process |
| `voice` | `{type = "voice", clip = "REPEATER"}` | Pre-rendered PCM clip |
| `tone` | `{type = "tone", freq1 = 1000, freq2 = 0, ms = 80, amp = 0.8}` | Synthesized tone |

A single message can freely mix all three types in any order.

### TOML format

```toml
[messages.my_id]
elements = [
  {type = "voice", clip = "THIS"},
  {type = "voice", clip = "IS"},
  {type = "cw",    text = "W1AW"},
  {type = "voice", clip = "REPEATER"},
]
```

### Shell `msg` commands

```
msg list                      List all messages with element types
msg show <name>               Show one message's elements in detail
msg new <name>                Create an empty message
msg add <name> cw <text>      Append a CW (Morse) element
msg add <name> voice <clip>   Append a VOICE clip element
msg add <name> tone <f1> [f2] <ms> <amp>   Append a TONE element
msg clear <name>              Remove all elements from a message
msg delete <name>             Delete a message
```

### Example: mixed CW + VOICE ID

```
rc> msg new my_id
rc> msg add my_id voice THIS
rc> msg add my_id voice IS
rc> msg add my_id cw W1AW
rc> msg add my_id voice REPEATER
```

### Example: courtesy tone

```
rc> msg new my_ct
rc> msg add my_ct tone 330 0 50 0.7
rc> msg add my_ct tone 495 0 50 0.7
rc> msg add my_ct tone 660 0 50 0.7
```

### Example: announce frequency

```
rc> msg new freq_announce
rc> msg add freq_announce voice FREQUENCY
rc> msg add freq_announce voice ONE
rc> msg add freq_announce voice FOUR
rc> msg add freq_announce voice SIX
rc> msg add freq_announce voice POINT
rc> msg add freq_announce voice NINE
rc> msg add freq_announce voice MEGAHERTZ
```

---

## 7. Station Identification

The controller implements FCC Part 97 identification requirements automatically.
Your station ID is simply the content of whatever CW and VOICE elements you
put in your messages — there is no separate callsign field.

### ID types

| Type | When it fires |
|------|---------------|
| **Initial** | End of hang after the first TX from a quiet period (polite: waits for QSO to end) |
| **Pending** | At COR drop before the CT when `id_pending` seconds remain before the mandatory deadline |
| **Mandatory** | When `id_interval` expires with TX activity since the last ID |
| **Impolite** | When the mandatory deadline hits while a QSO is in progress (ducked CW level) |

### Rotation

`initial_ids` and `mandatory_ids` are lists of message names played
round-robin. The pending and impolite slots are single messages (no rotation).

### Quiet period

If no transmission has occurred since the last ID, the `id_interval` timer
expires silently — no unnecessary transmission.

### Impolite ID interruption

If a voice ID is in progress when incoming COR asserts (new station keys up),
the voice ID is cancelled and an impolite CW-only ID (at `impolite_morse_level`)
is played over the active QSO. The ID cycle resets from the impolite ID.

---

## 8. Tone Elements

Tone elements produce synthesized audio within a message. Each element is
defined inline with four parameters:

| Parameter | Description |
|-----------|-------------|
| `freq1` | Primary frequency in Hz (0 = silence) |
| `freq2` | Secondary frequency in Hz (0 = single-frequency tone) |
| `ms` | Duration in milliseconds |
| `amp` | Amplitude from 0.0 (silent) to 1.0 (full scale) |

When both `freq1` and `freq2` are non-zero, both frequencies are summed
(useful for DTMF-style dual-tone chords).

### Classic patterns

**Ascending three-pip (yellow jacket)**:
```toml
{type = "tone", freq1 = 330, freq2 = 0, ms = 50, amp = 0.7}
{type = "tone", freq1 = 495, freq2 = 0, ms = 50, amp = 0.7}
{type = "tone", freq1 = 660, freq2 = 0, ms = 50, amp = 0.7}
```

**Descending three-tone timeout warning**:
```toml
{type = "tone", freq1 = 1200, freq2 = 0, ms = 60, amp = 0.9}
{type = "tone", freq1 = 0,    freq2 = 0, ms = 20, amp = 0.0}
{type = "tone", freq1 = 1000, freq2 = 0, ms = 60, amp = 0.9}
{type = "tone", freq1 = 0,    freq2 = 0, ms = 20, amp = 0.0}
{type = "tone", freq1 = 800,  freq2 = 0, ms = 60, amp = 0.9}
```

**Dual-frequency chord**:
```toml
{type = "tone", freq1 = 660, freq2 = 880, ms = 100, amp = 0.7}
```

---

## 9. Squelch Tail Elimination

When a user unkeys, the hardware COR/CTCSS decoder has 25–150 ms of
hysteresis before it drops — during which FM noise exits the receiver
discriminator. Without STE, that noise crash passes through to the TX.

`ste_delay_ms` introduces a software FIFO delay between the RX filter chain
and the passthrough gate. The gate still operates on real-time hardware signal
edges. When the gate closes, the delay buffer is abandoned — the noise burst
that entered the ADC after the user unkeyed is in the queue but never exits.

```
set ste 50      50 ms delay (moderate hysteresis)
set ste 0       disabled (default)
```

Typical values: 25–150 ms. Set to roughly match your hardware's COR/CTCSS
decoder hysteresis. Higher values increase startup latency at the beginning
of each transmission (hidden by CTCSS decode time on the receiving radio).

---

## 10. Pre/Post Message Padding

For standalone ID transmissions (where the repeater brings up PTT specifically
to identify), `pre_message_ms` and `post_message_ms` add dead air:

- **Pre**: After PTT-on, before the first CW or voice sample — gives the
  transmitter time to stabilize and receivers time to open squelch/decode CTCSS
- **Post**: After all audio drains, before PTT-off — prevents a "clipped" tail

These delays apply only to CW and voice elements (not tone-only messages) and
only when PTT was not already on. They are no-ops for courtesy tones,
mid-QSO impolite IDs, and pending IDs.

---

## 11. Logging

The daemon uses Python's standard `logging` module with journald-friendly
output. Log level is configurable: `DEBUG`, `INFO`, `WARNING`, `ERROR`.

```
set log level DEBUG      Verbose; includes every HID byte and audio block event
set log level INFO       Normal operation (default)
set log level WARNING    Service-impacting events only
```

**INFO-level events**: State transitions, PTT on/off, COR up/down with
duration, CTCSS detected/lost, ID timer fires, message playback, clip queue.

**WARNING-level events**: ADC clipping (rate-limited), PENDING timeout (in
`cor_ctcss` mode, when the second signal doesn't arrive in time).

**ERROR-level events**: Missing messages, unknown element types, voice clips
not found.

---

## 12. Hardware Reference

### CM108/CM119 signal mapping

| Signal | Direction | HID / Audio path |
|--------|-----------|-----------------|
| COR | Input | HID Vol-Up bit (byte 0, bit 1, 0x02) |
| CTCSS | Input | HID Vol-Down bit (byte 0, bit 0, 0x01) |
| PTT | Output | HID GPIO3 bit (output report, bit 2) |
| RX audio | Input | USB audio mic channel (discriminator output) |
| TX audio | Output | USB audio left speaker channel |

The right audio output channel is reserved for a future CTCSS encode tone.

### Signal polarity

The default `cor_active_low = true` / `ctcss_active_low = true` matches the
AllStar `chan_usbradio` convention (Masters Communications CM119 boards).
Some interfaces (e.g., DMK URIx) swap the bit assignments — set the
appropriate `_active_low` flag to match your hardware.

### Audio connections

- **Input**: Receiver discriminator tap or speaker output → CM119 mic input
- **Output**: CM119 left speaker output → transmitter audio input (mic or aux)

The CM119 only supports 44100 and 48000 Hz sample rates. The controller uses
48000 Hz.

---

## 13. File Reference

### Core modules

| File | Purpose |
|------|---------|
| `daemon.py` | Entry point — wires hardware, audio, port, and API server |
| `shell.py` | Operator CLI — connects to daemon via Unix socket |
| `port.py` | State machine — IDLE/PENDING/ACTIVE/TAIL/TIMEOUT, timers, ID scheduling |
| `api_server.py` | Unix socket server — JSON-lines protocol, push events |
| `audio_engine.py` | Duplex sounddevice stream — clip queue, RX/TX filter chain, STE |
| `audio_filters.py` | HPF300, DeEmphasis, PreEmphasis (scipy.signal IIR filters) |
| `hardware.py` | CM119 HID reader thread, PTT/COR/CTCSS callbacks |
| `rc_config.py` | TOML config model, `apply_set_command()` for shell set commands |
| `tones.py` | Tone synthesis (single and dual frequency, numpy-rendered) |
| `morse.py` | CW synthesis — `render()` for in-process use, CLI for standalone |
| `ctcss.py` | Goertzel CTCSS decode (retained for future use; not currently active) |

### Voice clip directories

| Path | Purpose |
|------|---------|
| `vocab_pcm/` | Built-in voice clips (712 WAV files, 8-bit mono, 8 kHz) |
| `user_pcm/` | User-supplied voice clips (takes precedence over vocab_pcm) |

### Configuration

| Path | Purpose |
|------|---------|
| `repeater.toml` | Site configuration (gitignored; copy from repeater.toml.sample) |
| `repeater.toml.sample` | Reference template with defaults and comments |

---

## Appendix A: Vocabulary

The `vocab_pcm/` directory contains 712 pre-rendered voice clips covering
aviation, radio, and general vocabulary. These clips are sourced from the
original **Texas Instruments speech synthesizer library** used in the famous
repeater controllers of the 1980s and 1990s (most notably the NHRC and
similar controllers of that era) — included here as a deliberate nod to
the controllers many of us grew up hearing on the air.

Clips are 8-bit mono WAV files at 8 kHz. They are committed to the repository
and require no generation step.

**You can replace any clip** by placing a same-named `.wav` file in `user_pcm/`.
Files in `user_pcm/` take precedence over `vocab_pcm/` when names match.
You can also build an entirely custom vocabulary by populating `user_pcm/`
with your own recordings — record your callsign, local landmarks, anything
you like. The only requirement is 16-bit or 8-bit mono WAV format; the audio
engine handles sample-rate conversion.

Clip names are case-insensitive and derived from the filename without the
`.wav` extension. Use `msg show <name>` in the shell or check the
`vocab_pcm/` directory for the exact set available.

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

---

## Appendix C: Troubleshooting

### `/dev/hidraw*` missing or permission denied

```bash
# Device should appear (vendor 0d8c)
lsusb | grep 0d8c

# hidraw node should be group-writable by audio
ls -la /dev/hidraw*   # expected: crw-rw---- 1 root audio ...
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| `/dev/hidraw*` missing | CM119 not plugged in | Check `dmesg \| tail` |
| `crw-------` (root only) | udev rule not applied | Replug device after installing rule |
| Open fails despite correct permissions | Group not active in session | Run `newgrp audio` or open new terminal |

### Audio device not found

Set `audio_device` in `[hardware]` to the exact sounddevice name. For CM119:

```toml
audio_device = "USB PnP Sound Device"
```

Leave empty for system default (may pick the wrong device on multi-card systems).

### No audio passthrough / repeater doesn't repeat

- Check COR polarity: if `cor_active_low = true` but your hardware drives the
  bit high on carrier detect, flip to `false`
- Run with `log_level = "DEBUG"` and watch for "COR up/down" messages in the log

### Courtesy tone not playing

- Verify `ct_message` is set to an existing message name
- Verify `ct_delay` is non-zero (the CT fires `ct_delay` seconds after COR drops)
- Use `play <message>` in the shell to test the message directly

### ID not firing

- Check that `initial_ids` / `mandatory_ids` contain at least one message name
- Verify the named messages exist: `msg list`
- Check `id_interval` (default 600 s — wait 10 minutes or reduce it for testing)

### Voice clip not found

- Clip names are case-insensitive; reference them in uppercase in your messages
- Check both `vocab_pcm/` and `user_pcm/` directories for the file
- Custom clips must be mono WAV format

### Audio clipping or distortion

- Lower `repeat_gain` if RX passthrough is too hot
- Lower `morse_level` or `voice_level`
- Check for ADC clipping warnings in the log (rate-limited to once per 5 s)

### Repeat audio too quiet / too loud

```
set repeat gain 1.5     Boost RX audio 50% before re-transmission
set repeat gain 0.8     Reduce RX audio 20%
set repeat gain 1.0     Unity (default)
```
