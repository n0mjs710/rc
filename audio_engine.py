"""
Audio engine for the repeater controller.

Implements a callback-driven duplex audio path using sounddevice:

  RX in  →  [HPF 300 Hz]  →  [De-emphasis]  →  repeat passthrough
                                                  ↓
  TX out ← [Pre-emphasis]  ←  left mix bus   ←  clips (CW, tones, voice)
  TX out ← right channel   ←  silence (CTCSS encode placeholder)

Filters are optional and independently enabled in AudioConfig.

Thread safety
─────────────
play_clip() and set_ptt() may be called from any thread.  The audio
callback fires from the sounddevice native thread.  The GIL protects
simple attribute reads; _clip_lock guards the clip deque.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

from audio_filters import HPF300, DeEmphasis, PreEmphasis

log = logging.getLogger("audio")


# ─────────────────────────────────────────────────────────────────────────────
# WAV loader
# ─────────────────────────────────────────────────────────────────────────────

def load_wav(path: str | Path, target_rate: int) -> np.ndarray:
    """
    Load a 16-bit mono WAV file as float32 at target_rate.
    Applies linear interpolation resampling if the file rate differs.
    """
    with wave.open(str(path), "r") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: only mono WAV files are supported")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit WAV files are supported")
        file_rate = wf.getframerate()
        raw = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    samples = raw.astype(np.float32) / 32768.0

    if file_rate != target_rate:
        orig_len = len(samples)
        new_len  = int(orig_len * target_rate / file_rate)
        idx      = np.linspace(0, orig_len - 1, new_len)
        samples  = np.interp(idx, np.arange(orig_len), samples).astype(np.float32)

    return samples


# ─────────────────────────────────────────────────────────────────────────────
# Clip — one unit of queued TX audio
# ─────────────────────────────────────────────────────────────────────────────

class Clip:
    """A pre-rendered audio segment queued for playback on the TX mix bus."""

    def __init__(self, samples: np.ndarray, label: str = "",
                 blocks_passthrough: bool = False) -> None:
        self.samples            = samples
        self.label              = label
        self.blocks_passthrough = blocks_passthrough
        self.pos                = 0

    def remaining(self) -> int:
        return len(self.samples) - self.pos

    def read(self, n: int) -> np.ndarray:
        chunk    = self.samples[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk


# ─────────────────────────────────────────────────────────────────────────────
# Voice clip cache
# ─────────────────────────────────────────────────────────────────────────────

class VocabCache:
    """
    Lazily loads WAV files from one or more directories.
    Earlier directories shadow later ones (user_pcm overrides vocab_pcm).
    Thread-safe.
    """

    def __init__(self, vocab_dirs: list[str | Path], sample_rate: int) -> None:
        self._dirs  = [Path(d) for d in vocab_dirs]
        self._rate  = sample_rate
        self._cache: dict[str, np.ndarray] = {}
        self._lock  = threading.Lock()

    def get(self, clip_name: str) -> np.ndarray | None:
        key = clip_name.upper()
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        for d in self._dirs:
            path = d / f"{key}.wav"
            if not path.exists():
                continue
            try:
                samples = load_wav(path, self._rate)
                with self._lock:
                    self._cache[key] = samples
                return samples
            except Exception as exc:
                log.error("Could not load voice clip %s: %s", path, exc)

        return None

    def available_clips(self) -> list[tuple[str, str]]:
        seen:  set[str] = set()
        clips: list[tuple[str, str]] = []
        for d in self._dirs:
            if not d.exists():
                continue
            for wav in sorted(d.glob("*.wav")):
                name = wav.stem.upper()
                if name not in seen:
                    seen.add(name)
                    clips.append((name, d.name))
        return clips


# ─────────────────────────────────────────────────────────────────────────────
# Audio engine
# ─────────────────────────────────────────────────────────────────────────────

class AudioEngine:
    """
    Duplex sounddevice engine with optional FM filters.

    Typical lifecycle:
        engine = AudioEngine(cfg.audio, input_device=..., output_device=...)
        engine.start()
        engine.set_ptt(True)
        engine.play_clip(Clip(samples, "ct:default"))
        ...
        engine.stop()
    """

    def __init__(self,
                 sample_rate:         int        = 16_000,
                 blocksize:           int        = 320,
                 input_device:        str | int | None = None,
                 output_device:       str | int | None = None,
                 rx_hpf:              bool       = True,
                 rx_deemphasis:       bool       = True,
                 tx_preemphasis:      bool       = False,
                 repeat_gain:         float      = 1.0,
                 voice_blocks_repeat: bool       = False,
                 ste_delay_ms:        int        = 0,
                 ) -> None:

        self.sample_rate        = sample_rate
        self.blocksize          = blocksize
        self._input_device      = input_device
        self._output_device     = output_device
        self._repeat_gain       = repeat_gain
        self._voice_blocks_repeat = voice_blocks_repeat

        # RX filter chain (applied in order)
        self._rx_hpf    = HPF300(sample_rate)    if rx_hpf       else None
        self._rx_deemph = DeEmphasis(sample_rate) if rx_deemphasis else None

        # TX filter (applied to final left-channel mix)
        self._tx_preph  = PreEmphasis(sample_rate) if tx_preemphasis else None

        # PTT state
        self._ptt      = False
        self._ptt_lock = threading.Lock()

        # RX source gate — True when the hardware presents a valid signal
        # (COR active, or COR+CTCSS active depending on access mode).
        # The port drives this directly from the hardware signal edges, not
        # from state transitions, so the gate tracks the actual signal presence.
        # When the signal drops the RX source closes immediately, preventing
        # FM squelch noise from an ungated discriminator reaching the TX during
        # tail/hang.  Queued clips play regardless of this gate.
        self._passthrough = False

        # Clip queue (left channel / main TX mix)
        self._clips:    deque[Clip] = deque()
        self._clip_lock             = threading.Lock()

        # STE (squelch tail elimination) delay buffer.
        # A FIFO of callback-sized blocks accumulates filtered RX audio while
        # the passthrough gate is open.  Reading lags writing by _ste_delay_blocks
        # blocks, so gate closure discards the noisy tail before it exits the queue.
        # _ste_flush is set by the asyncio thread (GIL-safe bool); the callback
        # clears the queue on the next fire — no locking needed for the deque.
        self._ste_delay_blocks: int = (
            math.ceil(ste_delay_ms * sample_rate / 1000 / blocksize)
            if ste_delay_ms > 0 else 0
        )
        self._ste_queue: deque[np.ndarray] = deque()
        self._ste_flush: bool = False

        # ADC clipping rate limiter
        self._last_clip_warn: float = 0.0

        self._stream: sd.Stream | None = None

    # ── public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open and start the duplex stream."""
        self._stream = sd.Stream(
            samplerate        = self.sample_rate,
            blocksize         = self.blocksize,
            dtype             = "float32",
            channels          = (1, 2),   # mono RX, stereo TX
            device            = (self._input_device, self._output_device),
            callback          = self._callback,
        )
        self._stream.start()
        log.info("Audio stream started — %d Hz  blocksize=%d  repeat_gain=%.2f",
                 self.sample_rate, self.blocksize, self._repeat_gain)

    def stop(self) -> None:
        """Stop and close the stream."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        log.info("Audio stream stopped.")

    def set_ptt(self, active: bool) -> None:
        """Enable or disable TX output.  Resets RX filter state on rising edge."""
        with self._ptt_lock:
            prev      = self._ptt
            self._ptt = active
        if active and not prev:
            self._reset_rx_filters()

    def set_passthrough(self, active: bool) -> None:
        """
        Gate the RX→TX audio passthrough.

        Set True while the qualifying signal is present (COR, or COR+CTCSS)
        so received audio is re-transmitted.  Set False the moment the signal
        drops so FM squelch noise from an ungated discriminator does not reach
        the TX during CT delay, CT playback, or hang.  Queued clips continue.

        When STE is enabled, opening the gate signals the callback to flush the
        delay queue so stale audio from the previous transmission does not replay.
        """
        prev = self._passthrough
        self._passthrough = active
        if active and not prev and self._ste_delay_blocks > 0:
            self._ste_flush = True

    def play_clip(self, clip: Clip, priority: bool = False) -> None:
        """Queue a clip for TX playback.  priority=True plays it next."""
        with self._clip_lock:
            if priority:
                self._clips.appendleft(clip)
            else:
                self._clips.append(clip)

    def play_samples(self, samples: np.ndarray, label: str = "",
                     priority: bool = False,
                     blocks_passthrough: bool = False) -> None:
        """Wrap samples in a Clip and queue it."""
        self.play_clip(Clip(samples, label, blocks_passthrough), priority=priority)

    def clear_clips(self) -> None:
        """Discard all queued clips."""
        with self._clip_lock:
            self._clips.clear()

    def is_playing(self) -> bool:
        with self._clip_lock:
            return bool(self._clips)

    def set_repeat_gain(self, gain: float) -> None:
        self._repeat_gain = gain

    def _reset_rx_filters(self) -> None:
        """Clear RX filter state so each new QSO starts from a known zero state."""
        if self._rx_hpf is not None:
            self._rx_hpf._zi[:] = 0
        if self._rx_deemph is not None:
            self._rx_deemph._zi[:] = 0

    # ── sounddevice callback ─────────────────────────────────────────────────

    def _callback(self,
                  indata:  np.ndarray,   # (blocksize, 1)
                  outdata: np.ndarray,   # (blocksize, 2)
                  frames:  int,
                  time_info,
                  status) -> None:

        with self._ptt_lock:
            ptt = self._ptt

        # No access condition / PTT not engaged — nothing to do
        if not ptt:
            outdata[:] = 0
            return

        # Overflows/underflows during an active transmission are real problems.
        if status:
            log.warning("Audio status: %s", status)

        # ── RX path (only while PTT is active) ───────────────────────────────
        rx = indata[:, 0].copy()

        # ADC clipping check
        peak = float(np.max(np.abs(rx)))
        if peak > 0.98:
            now = time.monotonic()
            if now - self._last_clip_warn > 5.0:
                self._last_clip_warn = now
                log.warning("ADC clipping — peak %.3f FS; reduce input level", peak)

        # ── Future CTCSS decode hook ──────────────────────────────────────────
        # Software CTCSS decode (Goertzel) should tap `rx` HERE — before the
        # HPF strips the subaudible tone — then pass results to the port via a
        # callback or queue.  The port will enable this path when
        # ctcss.access_mode == "cor_ctcss" and no hardware decode is available.

        if self._rx_hpf is not None:
            rx = self._rx_hpf.process(rx)
        if self._rx_deemph is not None:
            rx = self._rx_deemph.process(rx)

        # STE delay: runs entirely in the callback thread.  _ste_flush is set
        # by the asyncio thread (GIL-safe bool) and consumed here.  While the
        # passthrough is open, each block is appended to the queue; once the
        # queue holds more than _ste_delay_blocks blocks the oldest block is
        # dequeued as the passthrough source.  When the gate closes the queue
        # is abandoned — the noise crash that entered the ADC after the unkey
        # is in the queue but never exits it.
        if self._ste_delay_blocks > 0:
            if self._ste_flush:
                self._ste_queue.clear()
                self._ste_flush = False
            if self._passthrough:
                self._ste_queue.append(rx.copy())
                if len(self._ste_queue) > self._ste_delay_blocks:
                    rx = self._ste_queue.popleft()
                else:
                    rx = np.zeros(frames, dtype=np.float32)

        # Build left-channel TX mix — passthrough only while qualifying signal
        # is present.  _passthrough is False once the signal drops, keeping
        # FM squelch noise off the TX during CT delay, CT, and hang.
        voice_blocking = False
        if self._passthrough and self._voice_blocks_repeat:
            with self._clip_lock:
                voice_blocking = any(
                    c.blocks_passthrough and c.remaining() > 0
                    for c in self._clips
                )

        if self._passthrough and not voice_blocking:
            tx = rx * self._repeat_gain
        else:
            tx = np.zeros(frames, dtype=np.float32)

        # Mix in queued clips
        with self._clip_lock:
            remaining = frames
            offset    = 0
            while remaining > 0 and self._clips:
                chunk = self._clips[0].read(remaining)
                if len(chunk) > 0:
                    tx[offset : offset + len(chunk)] += chunk
                    offset    += len(chunk)
                    remaining -= len(chunk)
                if self._clips[0].remaining() == 0:
                    self._clips.popleft()

        np.clip(tx, -1.0, 1.0, out=tx)

        # Apply TX pre-emphasis to the final left-channel mix
        if self._tx_preph is not None:
            tx = self._tx_preph.process(tx)
            np.clip(tx, -1.0, 1.0, out=tx)

        outdata[:, 0] = tx
        outdata[:, 1] = 0   # right channel: CTCSS encode placeholder (silence)
