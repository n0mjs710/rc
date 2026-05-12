# Architecture Notes

Internal design notes for the repeater controller codebase.
This covers the "how and why" — rationale behind structural decisions,
known trade-offs, and things to watch for as the project evolves.

---

## Threading Model

The controller uses Python's `asyncio` for the main event loop (COR/CTCSS
state machine, timers, message sequencing) and `sounddevice` for real-time
audio I/O.  Sounddevice callbacks run on a native OS thread and acquire
the GIL before entering Python code.

**The GIL is a deliberate part of the design.**  Simple attribute mutations
like `self._repeat_gain = gain` are atomic under CPython's GIL, which is
what makes the audio callback safe without explicit locking on every shared
variable.  The few places that need multiple operations to be atomic
together (check-then-act patterns) use `_ptt_lock` explicitly.  The STE
flush flag (`_ste_flush`) and PTT flag (`_ptt`) follow the same pattern:
set by the asyncio thread, read/cleared by the callback thread — both are
Python bools, so a GIL-protected read or write is atomic.

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

All three element types are stored inline in the message element list — tone
parameters live directly in the TOML alongside CW text and voice clip names.
There is no separate named tone definition layer.  All audio output from the
controller (IDs, courtesy tones, timeout announce, startup) goes through the
same `_play_message()` / `_play_element()` path.

## Station Identification Logic

The ID system tracks two orthogonal state dimensions:

**Timer state** — either *quiet period* (`_id_timer is None`) or *active period*
(`_id_timer` is running).  The timer starts after the first TX from quiet period
and restarts after every ID.  It expires silently if `_tx_activity` is False.

**Activity flag** (`_tx_activity`) — set True on any ACTIVE transition, any system
TX (startup, timeout recovery), or when `_on_id()` fires mid-QSO (state == ACTIVE).
Set False after every successful ID.  Allows the system to distinguish "no
transmission since last ID" (quiet period, no ID needed) from "TX occurred" (ID required).

**ID types:**
- *Initial* — at end of hang after first TX from quiet period; waits for QSO to end
- *Mandatory* — when timer expires with activity and state is not ACTIVE
- *Pending* — sneaked in at COR drop (before CT) when the `id_pending` window is armed
- *Impolite* — played over active QSO when deadline hits mid-transmission (ducked CW level)

**Epoch interruption** — each ID coroutine saves `_id_epoch` at start and checks it
after any `await`.  When a qualified COR asserts during a voice-element ID, `_transition(ACTIVE)`
increments the epoch, cancels audio, and spawns an impolite ID.  The interrupted coroutine
detects the epoch change after its drain unblocks and returns without resetting the ID cycle
(the impolite ID owns that).  CW/tone IDs are not interruptible — only voice IDs.

## Audio Repeat Path

```
CM119 ADC → [HPF 300 Hz] → [De-emphasis] → [STE delay buffer] → passthrough gate
                                                                        ↓
CM119 DAC ← [Pre-emphasis] ← left mix bus ← clip queue (CW, tones, voice)
```

**Detection taps** — CTCSS software decode (future) taps `rx` *before* the HPF so
the sub-audible tone is present.  DTMF decode (future) taps *after* the HPF but
*before* the STE buffer so it operates on real-time audio.

**Passthrough gate** — set by `set_passthrough()` in the asyncio thread; read in the
callback thread (GIL-safe bool).  Closes the instant the qualifying hardware signal
drops (COR edge, or CTCSS drop in `cor_ctcss` mode), not on state transitions.

**Clip mix** — queued clips (CW, voice, tones) are additively mixed into the passthrough
on the left channel.  The right channel is reserved for a future CTCSS encode tone.

## Squelch Tail Elimination (STE)

`ste_delay_ms` introduces a FIFO delay between the RX filter chain and the passthrough
gate.  While the gate is open, each callback block of filtered audio is appended to the
queue and the oldest block (from `delay_blocks` callbacks ago) is used as the TX source.
When the gate closes, the queue is abandoned — the noise burst that entered the ADC after
the user unkeyed is in the queue but never exits it.

The queue is flushed on every passthrough-open edge (via `_ste_flush` flag) so stale
audio from the previous transmission does not replay.  This creates a startup silence of
`ste_delay_ms` at the beginning of each transmission, which is typically hidden by the
receiving radio's CTCSS decode time.

The STE queue is a `collections.deque` of `numpy.ndarray` copies — one per callback
block, allocated on the audio thread.  The deque is only modified from the audio callback
thread; `_ste_flush` is the only cross-thread signal.

## Pre/Post Message Padding

`pre_message_ms` and `post_message_ms` apply `asyncio.sleep()` delays around messages
that contain CW or voice elements, when the message transmission also raises/drops PTT
(i.e., standalone ID transmissions where `was_ptt == False` on entry to `_transmit_id()`).
This gives transmitters time to stabilize and receivers time to open squelch/decode CTCSS
before audio starts, and a brief tail before the carrier drops.

These delays are no-ops for courtesy tones, mid-QSO impolite IDs, and pending IDs (all
cases where PTT is already on and stays on after the message).

## CTCSS Decoder

The Goertzel algorithm is used for single-frequency detection (more efficient
than FFT when you only need one bin).  The implementation must be numpy-
vectorized for real-time performance at 48 kHz on a Pi 3.  The software
Goertzel path (`ctcss.py`) is retained for future use but not currently
active — the hardware decode signal from the CM119 HID interface is used instead.

## Target Platform

Raspberry Pi 3 or better.  Plenty of RAM (1 GB+) so we favour keeping data
in memory (vocab cache, tone rendering) over disk I/O.  CPU is adequate for
real-time audio DSP as long as hot paths avoid pure Python loops on sample
data — use numpy for anything that touches audio buffers.
