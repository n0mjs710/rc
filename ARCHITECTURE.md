# Architecture Notes

Internal design notes for the repeater controller codebase.
This covers the "how and why" — rationale behind structural decisions,
known trade-offs, and things to watch for as the project evolves.

---

## Threading Model

The controller uses Python's `asyncio` for the main event loop (COS/CTCSS
state machine, timers, message sequencing) and `sounddevice` for real-time
audio I/O.  Sounddevice callbacks run on a native OS thread and acquire
the GIL before entering Python code.

**The GIL is a deliberate part of the design.**  Simple attribute mutations
like `self._repeat_gain = gain` are atomic under CPython's GIL, which is
what makes the audio callback safe without explicit locking on every shared
variable.  The few places that need multiple operations to be atomic
together (check-then-act patterns) use `_ptt_lock` explicitly.

If free-threaded Python (PEP 703, experimental in 3.13–3.14) ever becomes
the default runtime, a threading audit will be needed: every shared attribute
read/write between the audio callback thread and the main thread would
need explicit lock protection.  As of 2026 this is years away at best —
standard GIL-enabled CPython is the only target, and Raspberry Pi OS ships
it exclusively.

## Message System

Messages are the central abstraction for all controller audio output.
A message is a named sequence of elements, where each element is one of:

- **CW** — Morse code characters (`{"type": "cw", "text": "N0CALL"}`)
- **VOICE** — pre-rendered PCM clip (`{"type": "voice", "clip": "REPEATER"}`)
- **TONE** — inline tone parameters (`{"type": "tone", "freq1": 1000, "freq2": 0, "ms": 50, "amp": 0.8}`)

Tones were originally a separate construct (`courtesy_tones`) with named
definitions referenced indirectly by messages.  This added a layer of
indirection that made the shell harder to use — you had to create a tone
definition, then reference it by name in a message.  The current design
stores tone parameters inline in the message element, just like CW stores
its text and VOICE stores its clip name.  All three element types are
entered the same way at the shell level:

    add my_msg cw N0CALL
    add my_msg voice THIS IS REPEATER
    add my_msg tone 1000 0 50 0.8

The legacy `[courtesy_tones]` section in TOML is still loaded for backward
compatibility with old config files, but the shell no longer creates or
manages named tone definitions.

## Audio Playback — Concatenation Strategy

Consecutive elements of the same type are grouped and played as a single
audio stream to avoid per-element subprocess/player startup gaps.  This is
especially important for VOICE messages (each word is a separate WAV file)
and multi-element TONE sequences.  The grouping happens at playback time
in both the shell preview (`_play_id_message`) and the runtime controller
(`_play_message`).

## CTCSS Decoder

The Goertzel algorithm is used for single-frequency detection (more efficient
than FFT when you only need one bin).  The implementation should be numpy-
vectorized for real-time performance — a pure Python sample loop will not
keep up at 16 kHz sample rate on a Pi 3.

## Target Platform

Raspberry Pi 3 or better.  Plenty of RAM (1 GB+) so we favour keeping data
in memory (vocab cache, tone rendering) over disk I/O.  CPU is adequate for
real-time audio DSP as long as hot paths avoid pure Python loops on sample
data — use numpy for anything that touches audio buffers.
