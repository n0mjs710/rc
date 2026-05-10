"""
Repeater Controller — main asyncio state machine.

Wires together:
  hardware.py    — GPIO (PTT out, COS in) via MockHardware or RealHardware
  audio_engine.py — duplex sounddevice stream with CTCSS encode/decode
  ctcss.py        — STE helpers
  tones.py        — courtesy tone rendering
  rc_config.py    — configuration dataclass

State machine
─────────────
IDLE     — quiet, PTT off
PENDING  — waiting for the missing half of the access requirement.
           In cos_ctcss / ctcss_init modes, either COS or CTCSS can arrive
           first; PENDING means one is present and the controller is waiting
           for the other.  A decode-window timeout returns to IDLE.
ACTIVE   — PTT on, passing audio
TAIL     — COS (or CTCSS) dropped, PTT still on, tail / hang timers running
TIMEOUT  — TOT exceeded — transmit locked out until COS drops
TRANSMIT — playing a message (ID / hang / announcement)

PENDING is bidirectional: COS-first waits for CTCSS; CTCSS-first waits for
COS.  The same ctcss_timeout window applies in both directions.

Logging levels
──────────────
INFO    — routine state transitions, PTT changes, ID fires, clip playback
WARNING — ADC clipping, CTCSS decode timeout, other service-impacting events
ERROR   — missing messages, unknown element types, malformed config —
          anything that would prevent a message from playing correctly

Usage:
    python3 controller.py [config.toml]
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from enum import Enum, auto
from pathlib import Path

from rc_config import RepeaterConfig
from hardware  import get_hardware, HardwareBase
from audio_engine import AudioEngine, Clip, VocabCache
from tones import render_tone
import ctcss as _ctcss

log = logging.getLogger("controller")


# ─────────────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE     = auto()   # quiet, PTT off
    PENDING  = auto()   # waiting for the other half of access requirement
    ACTIVE   = auto()   # PTT on, passing audio
    TAIL     = auto()   # COS dropped, PTT still on, tail timer running
    TIMEOUT  = auto()   # TOT exceeded — transmit locked out
    TRANSMIT = auto()   # playing ID / courtesy / announcement


# ─────────────────────────────────────────────────────────────────────────────
# Controller
# ─────────────────────────────────────────────────────────────────────────────

class RepeaterController:

    def __init__(self, cfg: RepeaterConfig):
        self.cfg   = cfg
        self.state = State.IDLE
        self._loop: asyncio.AbstractEventLoop | None = None

        # Hardware
        self._hw: HardwareBase = get_hardware(cfg.hardware.mock)

        # Audio engine
        ac = cfg.audio
        cc = cfg.ctcss
        self._engine = AudioEngine(
            sample_rate            = ac.sample_rate,
            blocksize              = ac.sample_rate // 50,   # 20 ms blocks
            ctcss_encode_freq      = cc.encode_freq,
            ctcss_encode_level     = cc.encode_level,
            ctcss_decode_freq      = cc.decode_freq,
            ctcss_decode_time_ms   = cc.decode_time_ms,
            ctcss_decode_threshold = cc.decode_threshold,
            ctcss_decode_hold_ms   = cc.decode_hold_ms,
            passthrough            = True,
            repeat_gain            = ac.repeat_gain,
            voice_blocks_repeat    = ac.voice_blocks_repeat,
        )

        # Voice cache: search vocab_pcm (built-in) then user_pcm (user-added)
        base = Path(__file__).parent
        voice_dirs = [d for d in (base / "vocab_pcm", base / "user_pcm") if d.exists()]
        if voice_dirs:
            self._vocab: VocabCache | None = VocabCache(voice_dirs, ac.sample_rate)
            log.info("Voice cache: %s", ", ".join(str(d) for d in voice_dirs))
        else:
            self._vocab = None
            log.warning("No voice directories found (vocab_pcm, user_pcm) — VOICE elements will be skipped")

        # Courtesy tones from config (already includes built-in defaults)
        self._tones = dict(cfg.courtesy_tones)

        # Timer handles
        self._tail_timer:       asyncio.TimerHandle | None = None
        self._hang_timer:       asyncio.TimerHandle | None = None
        self._id_timer:         asyncio.TimerHandle | None = None
        self._pending_id_timer: asyncio.TimerHandle | None = None
        self._timeout_timer:    asyncio.TimerHandle | None = None
        self._ctcss_timer:      asyncio.TimerHandle | None = None

        # Signal presence flags
        self._cos:  bool  = False
        self._ctcss: bool = False
        self._cos_up_time: float = 0.0

        # ID rotation: round-robin index per type
        self._id_rotation: dict[str, int] = {
            "initial": 0, "pending": 0, "mandatory": 0,
        }
        self._last_id_time: float = 0.0
        self._initial_id_sent: bool = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point.  Runs until cancelled."""
        self._loop = asyncio.get_running_loop()

        self._hw.setup(
            self.cfg.hardware.ptt_gpio,
            self.cfg.hardware.cos_gpio,
            self.cfg.hardware.cos_invert,
        )
        self._hw.add_cos_callback(self._on_cos_edge)

        self._engine.start()
        self._last_id_time = self._loop.time()
        self._schedule_id_timer()
        self._schedule_pending_id()

        log.info("Controller started — state: IDLE  access: %s  CTCSS: %s Hz",
                 self.cfg.ctcss.access_mode,
                 self.cfg.ctcss.decode_freq or "off")

        try:
            while True:
                await self._drain_ctcss_events()
                await asyncio.sleep(0.025)   # 25 ms poll interval
        except asyncio.CancelledError:
            pass
        finally:
            self._stop()

    def _stop(self) -> None:
        self._hw.set_ptt(False)
        self._engine.stop()
        self._hw.cleanup()
        log.info("Controller stopped.")

    # ── COS edge (GPIO thread → asyncio loop) ─────────────────────────────────

    def _on_cos_edge(self, active: bool) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._loop.create_task,
                self._handle_cos(active)
            )

    async def _handle_cos(self, active: bool) -> None:
        if active:
            await self._cos_up()
        else:
            await self._cos_down()

    # ── CTCSS events (audio engine queue → loop) ──────────────────────────────

    async def _drain_ctcss_events(self) -> None:
        while not self._engine.ctcss_events.empty():
            event = self._engine.ctcss_events.get_nowait()
            if event == _ctcss.__dict__.get("CTCSS_DETECTED", "ctcss_detected"):
                await self._ctcss_detected()
            elif event == _ctcss.__dict__.get("CTCSS_LOST", "ctcss_lost"):
                await self._ctcss_lost()

    # ── state machine events ──────────────────────────────────────────────────

    async def _cos_up(self) -> None:
        self._cos = True
        self._cos_up_time = self._loop.time()
        self._cancel_timer("tail")
        self._cancel_timer("hang")
        am = self.cfg.ctcss.access_mode

        log.info("COS UP  state=%s  access=%s", self.state.name, am)

        if am == "cos":
            if self.state in (State.IDLE, State.TAIL):
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()

        elif am in ("cos_ctcss", "ctcss_init"):
            if self.state == State.PENDING and self._ctcss:
                # CTCSS arrived first; COS now present → go ACTIVE
                self._cancel_timer("ctcss_timeout")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()
            elif self.state in (State.IDLE, State.TAIL):
                if self._ctcss:
                    # Both signals already present
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                    self._schedule_timeout()
                else:
                    # COS first — wait for CTCSS within decode window
                    self._transition(State.PENDING)
                    log.info("COS first — waiting for CTCSS (%d ms window)",
                             self.cfg.ctcss.decode_time_ms)
                    self._schedule_ctcss_timeout()

        elif am == "ctcss":
            log.debug("COS up — access mode ctcss, CTCSS controls PTT")

    async def _cos_down(self) -> None:
        self._cos = False
        self._cancel_timer("timeout")
        self._cancel_timer("ctcss_timeout")
        duration = self._loop.time() - self._cos_up_time
        am = self.cfg.ctcss.access_mode

        log.info("COS DOWN  duration=%.2fs  state=%s", duration, self.state.name)

        if self.state == State.PENDING:
            log.info("COS dropped during CTCSS window — kerchunk suppressed")
            self._transition(State.IDLE)

        elif self.state == State.ACTIVE:
            if duration < self.cfg.timers.kerchunk:
                log.info("COS %.2fs < kerchunk %.2fs — suppressed",
                         duration, self.cfg.timers.kerchunk)
                self._set_ptt(False)
                self._transition(State.IDLE)
            elif am == "ctcss":
                log.debug("COS down — ctcss mode, CTCSS still controls PTT")
            else:
                self._transition(State.TAIL)
                self._schedule_hang()
                self._schedule_tail()

    async def _ctcss_detected(self) -> None:
        self._ctcss = True
        am = self.cfg.ctcss.access_mode
        log.info("CTCSS confirmed (%.1f Hz)", self.cfg.ctcss.decode_freq)

        if am in ("cos_ctcss", "ctcss_init"):
            if self.state == State.PENDING and self._cos:
                # COS arrived first; CTCSS now confirmed → go ACTIVE
                self._cancel_timer("ctcss_timeout")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()
            elif self.state in (State.IDLE, State.TAIL):
                # CTCSS arrived first — wait for COS within decode window
                self._transition(State.PENDING)
                log.info("CTCSS first — waiting for COS (%d ms window)",
                         self.cfg.ctcss.decode_time_ms)
                self._schedule_ctcss_timeout()

        elif am == "ctcss":
            if self.state in (State.IDLE, State.TAIL):
                self._cancel_timer("tail")
                self._cancel_timer("hang")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()

    async def _ctcss_lost(self) -> None:
        self._ctcss = False
        am = self.cfg.ctcss.access_mode
        log.info("CTCSS lost")

        if am == "ctcss" and self.state == State.ACTIVE:
            log.info("CTCSS loss → tail (ctcss mode)")
            self._cancel_timer("timeout")
            self._transition(State.TAIL)
            self._schedule_hang()
            self._schedule_tail()

        elif am == "cos_ctcss" and self.state == State.ACTIVE:
            log.info("CTCSS loss → tail (cos_ctcss mode requires both)")
            self._cancel_timer("timeout")
            self._transition(State.TAIL)
            self._schedule_hang()
            self._schedule_tail()

        elif am == "ctcss_init" and self.state == State.PENDING:
            # Lost CTCSS while waiting for COS — back to IDLE
            self._cancel_timer("ctcss_timeout")
            log.info("CTCSS lost during PENDING — returning to IDLE")
            self._transition(State.IDLE)

        else:
            log.debug("CTCSS gone — COS still controls (access=%s)", am)

    # ── timers ────────────────────────────────────────────────────────────────

    def _schedule_hang(self) -> None:
        self._hang_timer = self._loop.call_later(
            self.cfg.timers.hang, self._on_hang)

    def _schedule_tail(self) -> None:
        self._tail_timer = self._loop.call_later(
            self.cfg.timers.tail, self._on_tail)

    def _schedule_timeout(self) -> None:
        self._timeout_timer = self._loop.call_later(
            self.cfg.timers.timeout, self._on_timeout)

    def _schedule_ctcss_timeout(self) -> None:
        delay = self.cfg.ctcss.decode_time_ms / 1000
        self._ctcss_timer = self._loop.call_later(delay, self._on_ctcss_timeout)

    def _schedule_id_timer(self) -> None:
        self._id_timer = self._loop.call_later(
            self.cfg.timers.id_interval, self._on_id)

    def _schedule_pending_id(self) -> None:
        self._cancel_timer("pending_id")
        remaining = self.cfg.timers.id_interval - (self._loop.time() - self._last_id_time)
        lead  = self.cfg.timers.id_pending
        delay = max(0, remaining - lead)
        if delay > 0:
            self._pending_id_timer = self._loop.call_later(delay, self._on_pending_id)

    def _cancel_timer(self, name: str) -> None:
        attr = f"_{name}_timer"
        h = getattr(self, attr, None)
        if h is not None:
            h.cancel()
            setattr(self, attr, None)

    # ── timer callbacks ───────────────────────────────────────────────────────

    def _on_hang(self) -> None:
        """Hang timer: play the courtesy tone (tail message)."""
        self._hang_timer = None
        name = self.cfg.identity.ct_message
        if not name:
            return
        # Try as a message first, then fall back to a legacy CT name
        if name in self.cfg.messages:
            log.info("Hang: playing message '%s'", name)
            was_ptt = self._cos
            if not was_ptt:
                self._set_ptt(True)
            self._play_message(name)
            if not was_ptt:
                self._set_ptt(False)
        elif name in self._tones:
            log.info("Hang: playing courtesy tone '%s' (legacy CT reference)", name)
            audio = render_tone(self._tones[name])
            was_ptt = self._cos
            if not was_ptt:
                self._set_ptt(True)
            self._engine.play_samples(audio, label=f"ct:{name}")
            if not was_ptt:
                self._set_ptt(False)
        else:
            log.error("Hang message/tone '%s' not found — check config", name)

    def _on_tail(self) -> None:
        """Tail timer: apply STE then drop PTT."""
        self._tail_timer = None
        if self._cos:
            return   # COS came back up while tail was running
        self._apply_ste()
        self._set_ptt(False)
        self._transition(State.IDLE)

    def _on_timeout(self) -> None:
        """Time-out timer (TOT): lock out transmit."""
        self._timeout_timer = None
        log.warning("TIMEOUT — TOT exceeded, forcing PTT off")
        self._engine.clear_clips()
        self._set_ptt(False)
        self._transition(State.TIMEOUT)

        # Play timeout message
        name = self.cfg.identity.timeout_message
        if name:
            self._set_ptt(True)
            if name in self.cfg.messages:
                self._play_message(name)
            elif name in self._tones:
                self._engine.play_samples(
                    render_tone(self._tones[name]), label=f"ct:{name}", priority=True
                )
            else:
                log.error("Timeout message '%s' not found", name)
            self._set_ptt(False)

    def _on_ctcss_timeout(self) -> None:
        """CTCSS/COS decode window elapsed without both signals present."""
        self._ctcss_timer = None
        if self.state == State.PENDING:
            missing = "COS" if self._ctcss else "CTCSS"
            log.warning("PENDING decode window elapsed — %s not received — returning to IDLE",
                        missing)
            self._transition(State.IDLE)

    def _on_pending_id(self) -> None:
        """Pending ID timer: fire a pending ID before the mandatory deadline."""
        self._pending_id_timer = None
        log.info("Pending ID timer fired")
        self._transmit_id("pending")

    def _on_id(self) -> None:
        """Mandatory ID timer: fire the next mandatory ID."""
        self._id_timer = None
        log.info("Mandatory ID timer fired")
        self._cancel_timer("pending_id")
        self._transmit_id("mandatory")
        self._last_id_time = self._loop.time()
        self._schedule_id_timer()
        self._schedule_pending_id()

    # ── message / audio helpers ───────────────────────────────────────────────

    def _transmit_id(self, id_type: str = "mandatory") -> None:
        """
        Rotate to the next message in the given ID rotation list and play it.
        """
        c = self.cfg
        if id_type == "initial":
            rotation = list(c.identity.initial_ids)
        elif id_type == "pending":
            rotation = list(c.identity.pending_ids)
        else:
            rotation = list(c.identity.mandatory_ids)

        if not rotation:
            log.warning("No %s ID messages assigned — skipping", id_type)
            return

        idx      = self._id_rotation.get(id_type, 0) % len(rotation)
        msg_name = rotation[idx]
        self._id_rotation[id_type] = (idx + 1) % len(rotation)

        log.info("ID (%s) → '%s'", id_type, msg_name)

        was_ptt = self._cos
        if not was_ptt:
            self._set_ptt(True)
        try:
            self._play_message(msg_name)
        finally:
            if not was_ptt:
                self._set_ptt(False)

    def _play_message(self, name: str) -> None:
        """
        Play a named message by executing each of its elements in sequence.

        Element types:
          cw    — Morse code via morse.py subprocess
          voice — pre-rendered PCM clip from vocab_pcm or user_pcm
          tone  — courtesy tone by name (legacy alias: ct)
          time  — system time readback (future, not yet implemented)
        """
        elements = self.cfg.messages.get(name)
        if elements is None:
            log.error("Message '%s' not found in pool — skipping", name)
            return
        if not elements:
            log.warning("Message '%s' has no elements — nothing to play", name)
            return

        for elem in elements:
            self._play_element(elem)

    def _play_element(self, elem: dict) -> None:
        """Execute one message element."""
        etype = elem.get("type", "")

        if etype == "cw":
            text = elem.get("text", "")
            if text:
                self._play_morse(text)
            else:
                log.error("CW element has no 'text' field: %r", elem)

        elif etype == "voice":
            clip = elem.get("clip", "")
            if clip:
                self._play_voice(clip)
            else:
                log.error("VOICE element has no 'clip' field: %r", elem)

        elif etype == "tone":  # "ct" is normalized to "tone" at load time
            if "tone" in elem:
                # Named tone reference — look up in courtesy_tones
                tone_name = elem["tone"]
                elements = self._tones.get(tone_name)
                if elements:
                    audio = render_tone(elements)
                    self._engine.play_samples(audio, label=f"tone:{tone_name}")
                    log.info("TONE: '%s'", tone_name)
                else:
                    log.error("Tone '%s' not found in courtesy_tones — check config", tone_name)
            elif "freq1" in elem:
                # Inline tone parameters
                f1  = float(elem.get("freq1", 0))
                f2  = float(elem.get("freq2", 0))
                ms  = int(elem.get("ms", 80))
                amp = float(elem.get("amp", 0.8))
                audio = render_tone([[f1, f2, ms, amp]])
                self._engine.play_samples(audio, label=f"tone:inline")
                log.info("TONE: inline %g+%g Hz %d ms", f1, f2, ms)
            else:
                log.error("TONE element has no 'tone' or 'freq1' field: %r", elem)

        elif etype == "time":
            log.warning("TIME element not yet implemented — skipping")

        else:
            log.error("Unknown message element type '%s' in element: %r", etype, elem)

    def _play_morse(self, text: str) -> None:
        """Play Morse CW via the morse.py subprocess (blocking)."""
        import subprocess
        script = Path(__file__).parent / "morse.py"
        ac = self.cfg.audio
        log.info("CW: '%s'  %d WPM  %d Hz", text, ac.morse_wpm, ac.morse_pitch)
        try:
            subprocess.run(
                [sys.executable, str(script),
                 str(ac.morse_wpm), str(ac.morse_pitch), str(ac.morse_volume),
                 text],
                check=True,
            )
        except Exception as exc:
            log.error("Morse playback failed: %s", exc)

    def _play_voice(self, clip_name: str) -> None:
        """
        Play a pre-rendered voice clip from vocab_pcm or user_pcm.
        Clips are named by their filename (without .wav extension, case-insensitive).
        Drop .wav files into user_pcm/ to add your own voice content.
        """
        if not self._vocab:
            log.error("No voice directories available — cannot play clip '%s'", clip_name)
            return
        samples = self._vocab.get(clip_name)
        if samples is None:
            log.error("Voice clip '%s' not found in any voice directory", clip_name)
            return
        # Apply voice volume
        vol = self.cfg.audio.voice_volume / 100.0
        if vol != 1.0:
            samples = samples * vol
        log.info("VOICE: '%s'  vol=%d%%", clip_name, self.cfg.audio.voice_volume)
        self._engine.play_samples(
            samples,
            label=f"voice:{clip_name}",
            blocks_passthrough=True,   # VOICE clips always mark themselves as blocking
        )

    def _apply_ste(self) -> None:
        ste  = self.cfg.ctcss.ste_mode
        freq = self.cfg.ctcss.encode_freq
        if not freq:
            return
        if ste == "reverse_burst":
            self._engine.send_reverse_burst(self.cfg.ctcss.reverse_burst_ms)
            log.info("STE: reverse burst (%d ms)", self.cfg.ctcss.reverse_burst_ms)
        elif ste == "chicken_burst":
            self._engine.send_chicken_burst_stop()
            log.info("STE: chicken burst — CTCSS stopped %d ms before carrier drop",
                     self.cfg.ctcss.chicken_burst_ms)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_ptt(self, active: bool) -> None:
        self._hw.set_ptt(active)
        self._engine.set_ptt(active)
        log.info("PTT %s", "ON" if active else "OFF")

    def _transition(self, new_state: State) -> None:
        old = self.state
        log.info("State: %s → %s", old.name, new_state.name)
        self.state = new_state

        # Initial ID: on first activation after being idle longer than id_interval
        if new_state == State.ACTIVE and old in (State.IDLE, State.PENDING):
            elapsed = self._loop.time() - self._last_id_time
            if elapsed >= self.cfg.timers.id_interval and not self._initial_id_sent:
                self._initial_id_sent = True
                log.info("Initial ID — idle for %.0fs (>= %.0fs interval)",
                         elapsed, self.cfg.timers.id_interval)
                self._transmit_id("initial")
                self._last_id_time = self._loop.time()
                self._cancel_timer("id")
                self._schedule_id_timer()
                self._schedule_pending_id()

        if new_state == State.IDLE:
            self._initial_id_sent = False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt= "%H:%M:%S",
    )

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    if config_path:
        cfg = RepeaterConfig.load(config_path)
        log.info("Loaded config: %s", config_path)
    else:
        cfg = RepeaterConfig()
        log.info("Using default configuration (mock hardware)")

    controller = RepeaterController(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    task = loop.create_task(controller.run())

    def _shutdown(sig, frame):
        log.info("Signal %s received — shutting down", sig)
        task.cancel()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
