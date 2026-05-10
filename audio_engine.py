"""
Audio engine for the repeater controller.

Implements an all-digital audio path using sounddevice's callback-based
duplex stream:

  RX audio  →  CTCSS decode  →  (gain-adjusted) passthrough to TX mix bus
  TX mix bus  =  passthrough + pre-rendered PCM clips + CTCSS encode
              →  sounddevice output

Key design decisions
────────────────────
• Callback-driven.  The sounddevice stream callback fires at fixed intervals
  (determined by blocksize).  All audio processing happens inside the callback
  so latency is deterministic.

• Non-blocking clip injection.  Pre-rendered WAV clips (courtesy tones, voice
  words, Morse audio) are loaded into a queue and consumed by the callback.
  The caller queues a clip with play_clip(); it plays on the next available
  buffer boundary.

• CTCSS is handled inside the callback.  The encoder adds the sub-audible
  tone to every TX sample when enabled; the decoder receives every RX sample
  and posts events to a thread-safe queue the controller reads from.

• PTT gating.  When PTT is inactive the TX output is silence.  When PTT is
  active, passthrough + clips + CTCSS tone are mixed and output.

• Clip preemption.  A higher-priority clip (e.g. timeout warning) can be
  queued with priority=True to jump ahead of queued audio.

• Repeat gain.  The RX passthrough level can be scaled up or down via
  repeat_gain before being re-transmitted.  Useful when RX audio level does
  not match the transmitter's input requirement.

• Voice-blocks-repeat.  When voice_blocks_repeat is enabled, any clip marked
  blocks_passthrough=True (i.e. voice clips) will mute the RX passthrough
  for its duration, preventing feedback between the transmitted voice and the
  received audio path.

• ADC clipping detection.  The callback monitors the RX input level and logs
  a WARNING (rate-limited to once per 5 s) if the input clips above 0.98 FS.

Thread safety
─────────────
• play_clip() and set_ptt() may be called from any thread.
• ctcss_events is a queue.Queue; the controller polls it from its asyncio
  loop using a queue-polling asyncio task.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from collections import deque
from pathlib import Path
import numpy as np
import sounddevice as sd

from ctcss import CTCSSEncoder, CTCSSDecoder

log = logging.getLogger("audio")


# ─────────────────────────────────────────────────────────────────────────────
# Pre-rendered clip loader
# ─────────────────────────────────────────────────────────────────────────────

def load_wav(path: str | Path, target_rate: int) -> np.ndarray:
    """
    Load a 16-bit mono WAV file and return a float32 array at target_rate.

    If the file's sample rate differs from target_rate a simple linear
    resampling is applied (adequate for voice/tone material).
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
# Clip (one unit of queued audio)
# ─────────────────────────────────────────────────────────────────────────────

class Clip:
    """A pre-rendered audio segment waiting to be mixed into the TX bus."""

    def __init__(self, samples: np.ndarray, label: str = "",
                 blocks_passthrough: bool = False):
        self.samples           = samples
        self.label             = label
        self.pos               = 0          # samples consumed so far
        self.blocks_passthrough = blocks_passthrough  # True for VOICE clips

    def remaining(self) -> int:
        return len(self.samples) - self.pos

    def read(self, n: int) -> np.ndarray:
        """Return up to n samples from the current position, advancing pos."""
        chunk = self.samples[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary / voice clip cache
# ─────────────────────────────────────────────────────────────────────────────

class VocabCache:
    """
    Lazily loads pre-rendered voice clip WAV files from one or more directories.

    Search order: directories are checked left-to-right; the first match wins.
    This lets user_pcm/ override vocab_pcm/ clips by name.

    Thread-safe.
    """

    def __init__(self, vocab_dirs: list[str | Path], sample_rate: int):
        self._dirs  = [Path(d) for d in vocab_dirs]
        self._rate  = sample_rate
        self._cache: dict[str, np.ndarray] = {}
        self._lock  = threading.Lock()

    def get(self, clip_name: str) -> np.ndarray | None:
        """Return float32 samples for clip_name, or None if not found."""
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
        """
        Return sorted list of (clip_name, source_dir_name) tuples for all
        discoverable .wav files across all search directories.
        """
        seen: set[str] = set()
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

# Events posted to ctcss_events queue
CTCSS_DETECTED = "ctcss_detected"
CTCSS_LOST     = "ctcss_lost"


class AudioEngine:
    """
    Duplex sounddevice engine.

    Typical lifecycle:
        engine = AudioEngine(...)
        engine.start()
        engine.set_ptt(True)
        engine.play_clip(Clip(samples, "ct:default"))
        engine.stop()
    """

    def __init__(self,
                 sample_rate:         int         = 16_000,
                 blocksize:           int         = 320,       # 20 ms @ 16 kHz
                 input_device:        int | None  = None,
                 output_device:       int | None  = None,
                 ctcss_encode_freq:   float       = 0.0,
                 ctcss_encode_level:  float       = 0.15,
                 ctcss_decode_freq:   float       = 0.0,
                 ctcss_decode_time_ms:      int   = 250,
                 ctcss_decode_threshold:    float = 0.015,
                 ctcss_decode_hold_ms:      int   = 500,
                 passthrough:         bool        = True,
                 repeat_gain:         float       = 1.0,
                 voice_blocks_repeat: bool        = False,
                 ):
        self.sample_rate        = sample_rate
        self.blocksize          = blocksize
        self.passthrough        = passthrough
        self._repeat_gain       = repeat_gain
        self._voice_blocks_repeat = voice_blocks_repeat

        # PTT state — controls whether TX output is live
        self._ptt      = False
        self._ptt_lock = threading.Lock()

        # Clip queue — consumed in callback order (deque for O(1) popleft)
        self._clips: deque[Clip]  = deque()
        self._clip_lock           = threading.Lock()

        # CTCSS encoder (None = disabled)
        self._encoder: CTCSSEncoder | None = (
            CTCSSEncoder(ctcss_encode_freq, ctcss_encode_level, sample_rate)
            if ctcss_encode_freq > 0 else None
        )
        self._encoder_muted = False          # True during chicken burst

        # CTCSS decoder (None = disabled)
        self._decoder: CTCSSDecoder | None = (
            CTCSSDecoder(ctcss_decode_freq,
                         ctcss_decode_time_ms,
                         ctcss_decode_threshold,
                         ctcss_decode_hold_ms,
                         sample_rate)
            if ctcss_decode_freq > 0 else None
        )

        # Events from decoder → controller
        self.ctcss_events: queue.Queue = queue.Queue()

        # ADC clipping rate limiter
        self._last_clip_warn: float = 0.0

        # sounddevice stream (created on start())
        self._stream: sd.Stream | None = None
        self._input_device  = input_device
        self._output_device = output_device

    # ── public API ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open and start the duplex stream."""
        self._stream = sd.Stream(
            samplerate        = self.sample_rate,
            blocksize         = self.blocksize,
            dtype             = "float32",
            channels          = 1,
            device            = (self._input_device, self._output_device),
            callback          = self._callback,
            finished_callback = self._on_finished,
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
        """Enable or disable the TX output."""
        with self._ptt_lock:
            self._ptt = active
        if active:
            self._encoder_muted = False

    def play_clip(self, clip: Clip, priority: bool = False) -> None:
        """
        Queue a clip for playback on the TX bus.
        priority=True inserts at the front of the queue (preempts current).
        """
        with self._clip_lock:
            if priority:
                self._clips.appendleft(clip)
            else:
                self._clips.append(clip)

    def play_samples(self, samples: np.ndarray, label: str = "",
                     priority: bool = False,
                     blocks_passthrough: bool = False) -> None:
        """Convenience: wrap samples in a Clip and queue it."""
        self.play_clip(Clip(samples, label, blocks_passthrough), priority=priority)

    def clear_clips(self) -> None:
        """Discard all queued clips (e.g. on timeout)."""
        with self._clip_lock:
            self._clips.clear()

    def is_playing(self) -> bool:
        """True if there are clips currently queued."""
        with self._clip_lock:
            return bool(self._clips)

    def set_repeat_gain(self, gain: float) -> None:
        """Update the RX passthrough gain (thread-safe)."""
        self._repeat_gain = gain

    # ── sounddevice callback ─────────────────────────────────────────────────

    def _callback(self,
                  indata:  np.ndarray,   # shape (blocksize, 1)
                  outdata: np.ndarray,   # shape (blocksize, 1)
                  frames:  int,
                  time_info,
                  status) -> None:

        if status:
            log.warning("Audio stream status: %s", status)

        rx = indata[:, 0]   # mono RX audio

        # ── ADC clipping detection ────────────────────────────────────────────
        peak = float(np.max(np.abs(rx)))
        if peak > 0.98:
            now = time.monotonic()
            if now - self._last_clip_warn > 5.0:
                self._last_clip_warn = now
                log.warning("ADC clipping on RX input — peak %.3f FS; "
                            "reduce hardware input level or lower repeat_gain", peak)

        # ── CTCSS decode (always running, even when PTT is off) ──────────────
        if self._decoder is not None:
            self._decoder.push(rx)
            if self._decoder.detected():
                self.ctcss_events.put_nowait(CTCSS_DETECTED)
            if self._decoder.lost():
                self.ctcss_events.put_nowait(CTCSS_LOST)

        # ── TX mix bus ───────────────────────────────────────────────────────
        with self._ptt_lock:
            ptt = self._ptt

        if not ptt:
            outdata[:] = 0
            return

        # Determine if any active clip wants to block passthrough
        voice_blocking = False
        if self._voice_blocks_repeat:
            with self._clip_lock:
                voice_blocking = any(
                    c.blocks_passthrough and c.remaining() > 0
                    for c in self._clips
                )

        # Build passthrough (optionally muted during voice, gain-adjusted)
        if self.passthrough and not voice_blocking:
            tx = rx.copy() * self._repeat_gain
        else:
            tx = np.zeros(frames, dtype=np.float32)

        # Mix in any queued clips
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

        # Add CTCSS encode tone
        if self._encoder is not None and not self._encoder_muted:
            tx += self._encoder.generate(frames)

        # Clip to ±1.0 to prevent DAC distortion
        np.clip(tx, -1.0, 1.0, out=tx)
        outdata[:, 0] = tx

    def _on_finished(self) -> None:
        pass   # hook for subclasses or cleanup

    # ── STE helpers ──────────────────────────────────────────────────────────

    def send_reverse_burst(self, duration_ms: int) -> None:
        """
        Queue a 120° Motorola reverse-burst segment using the encoder's
        current phase so the burst is seamlessly continuous with the
        preceding CTCSS tone.
        """
        if self._encoder is None:
            return
        burst = self._encoder.reverse_burst(duration_ms)
        self.play_samples(burst, label="STE:reverse_burst", priority=True)

    def send_chicken_burst_stop(self) -> None:
        """
        Stop the CTCSS encoder immediately (chicken burst: tone goes silent
        before carrier drops).  Call set_ptt(False) after chicken_burst_ms.
        The encoder is muted rather than destroyed so it can be re-enabled
        on the next TX cycle when set_ptt(True) is called.
        """
        self._encoder_muted = True


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test (no real audio device needed — uses virtual stream)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("audio_engine.py self-test")

    sr   = 16_000
    bs   = 320
    freq = 100.0

    engine = AudioEngine(
        sample_rate           = sr,
        blocksize             = bs,
        ctcss_encode_freq     = freq,
        ctcss_encode_level    = 0.15,
        ctcss_decode_freq     = freq,
        ctcss_decode_time_ms  = 250,
        ctcss_decode_threshold= 0.015,
        passthrough           = False,
        repeat_gain           = 1.2,
        voice_blocks_repeat   = True,
    )

    engine.set_ptt(True)
    clip_audio = np.ones(sr // 4, dtype=np.float32) * 0.3
    engine.play_samples(clip_audio, label="voice:TEST", blocks_passthrough=True)

    indata  = np.zeros((bs, 1), dtype=np.float32)
    outdata = np.zeros((bs, 1), dtype=np.float32)

    blocks_with_audio = 0
    for _ in range(sr // bs):
        engine._callback(indata, outdata, bs, None, None)
        if np.any(outdata != 0):
            blocks_with_audio += 1

    total_blocks = sr // bs
    print(f"  Blocks with TX audio : {blocks_with_audio}/{total_blocks}")
    print(f"  Clip queue empty     : {not engine.is_playing()}")
    print(f"  CTCSS events queued  : {engine.ctcss_events.qsize()}")

    # Test multi-dir VocabCache
    import wave as _wave, tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        # Create a dummy WAV in the temp dir
        pcm = (np.sin(np.linspace(0, 2 * np.pi * 5, 8000)) * 32767).astype(np.int16)
        wav_path = os.path.join(tmp, "HELLO.wav")
        with _wave.open(wav_path, "w") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000)
            wf.writeframes(pcm.tobytes())
        vc = VocabCache([tmp], sample_rate=16_000)
        samples = vc.get("HELLO")
        clips   = vc.available_clips()
        print(f"  VocabCache 'HELLO'   : {len(samples)} samples")
        print(f"  available_clips      : {clips}")

    # Test load_wav resampling
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tmp_wav = tf.name
    pcm2 = (np.sin(np.linspace(0, 2*np.pi*5, 8000)) * 32767).astype(np.int16)
    with _wave.open(tmp_wav, "w") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000)
        wf.writeframes(pcm2.tobytes())
    loaded = load_wav(tmp_wav, target_rate=16_000)
    os.unlink(tmp_wav)
    print(f"  load_wav 8kHz→16kHz  : {len(loaded)} samples (expected ~{len(pcm2)*2})")

    print("\nSelf-test complete.")
