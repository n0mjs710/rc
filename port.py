"""
Repeater port state machine.

Implements the COR/CTCSS access control and timing logic for a single
repeater port.  Hardware events arrive via callbacks from the CM119 reader
thread and are dispatched to the asyncio loop via call_soon_threadsafe.

States
──────
IDLE     — quiet, PTT off
PENDING  — one required signal is present, waiting for the other
           (cor_ctcss access mode only)
ACTIVE   — PTT on, repeating audio
TAIL     — COR dropped, PTT still on, hang/tail timers running
TIMEOUT  — TOT exceeded, TX locked out until COR drops

Access modes
────────────
"cor"       — COR alone opens the repeater
"cor_ctcss" — both COR and CTCSS required; either can arrive first
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from rc_config import RepeaterConfig
from hardware  import CM119Hardware
from audio_engine import AudioEngine, Clip, VocabCache
from tones import render_tone
import morse

log = logging.getLogger("port")


class State(Enum):
    IDLE    = auto()
    PENDING = auto()
    ACTIVE  = auto()
    TAIL    = auto()
    TIMEOUT = auto()


class Port:
    """
    Single-port repeater controller.

    Constructed by the daemon; runs entirely inside the asyncio event loop
    (except for hardware callbacks which marshal to the loop via
    call_soon_threadsafe).
    """

    def __init__(self, cfg: RepeaterConfig, hw: CM119Hardware,
                 engine: AudioEngine) -> None:
        self.cfg    = cfg
        self._hw    = hw
        self._engine = engine
        self.state  = State.IDLE
        self._loop: asyncio.AbstractEventLoop | None = None

        # Voice clip cache
        base = Path(__file__).parent
        voice_dirs = [d for d in (base / "vocab_pcm", base / "user_pcm") if d.exists()]
        self._vocab: VocabCache | None = (
            VocabCache(voice_dirs, cfg.audio.sample_rate) if voice_dirs else None
        )
        if not voice_dirs:
            log.warning("No voice directories found — VOICE elements will be skipped")

        self._tones = dict(cfg.courtesy_tones)

        # Signal presence flags
        self._cor:   bool  = False
        self._ctcss: bool  = False
        self._cor_up_time: float = 0.0

        # ID rotation index per type
        self._id_rot: dict[str, int] = {"initial": 0, "pending": 0, "mandatory": 0}
        self._last_id_time:   float = 0.0
        self._initial_id_sent: bool = False

        # Timer handles
        self._tail_timer:       asyncio.TimerHandle | None = None
        self._hang_timer:       asyncio.TimerHandle | None = None
        self._timeout_timer:    asyncio.TimerHandle | None = None
        self._ctcss_timer:      asyncio.TimerHandle | None = None
        self._id_timer:         asyncio.TimerHandle | None = None
        self._pending_id_timer: asyncio.TimerHandle | None = None

        # State-change subscribers (for API push events)
        self._state_listeners: list[Callable] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._hw.add_cor_callback(self._on_cor_edge)
        self._hw.add_ctcss_callback(self._on_ctcss_edge)
        self._last_id_time = loop.time()
        self._schedule_id()
        self._schedule_pending_id()
        log.info("Port started — state=IDLE  access=%s", self.cfg.ctcss.access_mode)

    def stop(self) -> None:
        self._hw.remove_cor_callback(self._on_cor_edge)
        self._hw.remove_ctcss_callback(self._on_ctcss_edge)
        for name in ("tail", "hang", "timeout", "ctcss", "id", "pending_id"):
            self._cancel(name)
        self._set_ptt(False)
        log.info("Port stopped.")

    # ── hardware edge callbacks (called from HID reader thread) ───────────────

    def _on_cor_edge(self, active: bool) -> None:
        self._loop.call_soon_threadsafe(
            self._loop.create_task, self._handle_cor(active)
        )

    def _on_ctcss_edge(self, active: bool) -> None:
        self._loop.call_soon_threadsafe(
            self._loop.create_task, self._handle_ctcss(active)
        )

    # ── COR event handlers ────────────────────────────────────────────────────

    async def _handle_cor(self, active: bool) -> None:
        if active:
            await self._cor_up()
        else:
            await self._cor_down()

    async def _cor_up(self) -> None:
        self._cor = True
        self._cor_up_time = self._loop.time()
        self._cancel("tail")
        self._cancel("hang")
        am = self.cfg.ctcss.access_mode
        log.info("COR UP  state=%s  access=%s", self.state.name, am)

        if am == "cor":
            if self.state in (State.IDLE, State.TAIL):
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()

        elif am == "cor_ctcss":
            if self.state == State.PENDING and self._ctcss:
                # CTCSS arrived first; COR now present
                self._cancel("ctcss")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()
            elif self.state in (State.IDLE, State.TAIL):
                if self._ctcss:
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                    self._schedule_timeout()
                else:
                    self._transition(State.PENDING)
                    self._schedule_ctcss_timeout()

    async def _cor_down(self) -> None:
        self._cor = False
        self._cancel("timeout")
        self._cancel("ctcss")
        duration = self._loop.time() - self._cor_up_time
        log.info("COR DOWN  duration=%.2fs  state=%s", duration, self.state.name)

        if self.state == State.PENDING:
            self._transition(State.IDLE)

        elif self.state in (State.ACTIVE, State.TAIL):
            if self.state == State.ACTIVE and duration < self.cfg.timers.kerchunk:
                log.info("Kerchunk suppressed (%.2fs < %.2fs)",
                         duration, self.cfg.timers.kerchunk)
                self._set_ptt(False)
                self._transition(State.IDLE)
            else:
                self._transition(State.TAIL)
                self._schedule_hang()
                self._schedule_tail()

        elif self.state == State.TIMEOUT:
            self._transition(State.IDLE)

    # ── CTCSS event handlers ──────────────────────────────────────────────────

    async def _handle_ctcss(self, active: bool) -> None:
        if active:
            await self._ctcss_up()
        else:
            await self._ctcss_down()

    async def _ctcss_up(self) -> None:
        self._ctcss = True
        am = self.cfg.ctcss.access_mode
        log.info("CTCSS UP  state=%s  access=%s", self.state.name, am)

        if am == "cor_ctcss":
            if self.state == State.PENDING and self._cor:
                self._cancel("ctcss")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule_timeout()
            elif self.state in (State.IDLE, State.TAIL):
                if self._cor:
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                    self._schedule_timeout()
                else:
                    self._transition(State.PENDING)
                    self._schedule_ctcss_timeout()

    async def _ctcss_down(self) -> None:
        self._ctcss = False
        am = self.cfg.ctcss.access_mode
        log.info("CTCSS DOWN  state=%s", self.state.name)

        if am == "cor_ctcss" and self.state == State.ACTIVE:
            log.info("CTCSS loss in cor_ctcss mode → tail")
            self._cancel("timeout")
            self._transition(State.TAIL)
            self._schedule_hang()
            self._schedule_tail()
        elif self.state == State.PENDING:
            self._cancel("ctcss")
            self._transition(State.IDLE)

    # ── timer scheduling ──────────────────────────────────────────────────────

    def _schedule_hang(self) -> None:
        self._hang_timer = self._loop.call_later(self.cfg.timers.hang, self._on_hang)

    def _schedule_tail(self) -> None:
        self._tail_timer = self._loop.call_later(self.cfg.timers.tail, self._on_tail)

    def _schedule_timeout(self) -> None:
        self._timeout_timer = self._loop.call_later(
            self.cfg.timers.timeout, self._on_timeout)

    def _schedule_ctcss_timeout(self) -> None:
        self._ctcss_timer = self._loop.call_later(
            0.5, self._on_ctcss_timeout)   # 500 ms window to receive second signal

    def _schedule_id(self) -> None:
        self._id_timer = self._loop.call_later(
            self.cfg.timers.id_interval, self._on_id)

    def _schedule_pending_id(self) -> None:
        self._cancel("pending_id")
        remaining = self.cfg.timers.id_interval - (self._loop.time() - self._last_id_time)
        lead  = self.cfg.timers.id_pending
        delay = max(0.0, remaining - lead)
        if delay > 0:
            self._pending_id_timer = self._loop.call_later(delay, self._on_pending_id)

    def _cancel(self, name: str) -> None:
        attr = f"_{name}_timer"
        h = getattr(self, attr, None)
        if h is not None:
            h.cancel()
            setattr(self, attr, None)

    # ── timer callbacks ───────────────────────────────────────────────────────

    def _on_hang(self) -> None:
        self._hang_timer = None
        name = self.cfg.identity.ct_message
        if not name:
            return
        log.info("Hang timer — playing '%s'", name)
        was_ptt = self._cor
        if not was_ptt:
            self._set_ptt(True)
        self._play_message(name)
        if not was_ptt:
            self._set_ptt(False)

    def _on_tail(self) -> None:
        self._tail_timer = None
        if self._cor:
            return   # COR came back up while tail was running
        self._set_ptt(False)
        self._transition(State.IDLE)

    def _on_timeout(self) -> None:
        self._timeout_timer = None
        log.warning("TOT — forcing PTT off")
        self._engine.clear_clips()
        self._set_ptt(False)
        self._transition(State.TIMEOUT)
        name = self.cfg.identity.timeout_message
        if name:
            self._set_ptt(True)
            self._play_message(name)
            self._set_ptt(False)

    def _on_ctcss_timeout(self) -> None:
        self._ctcss_timer = None
        if self.state == State.PENDING:
            missing = "COR" if self._ctcss else "CTCSS"
            log.warning("PENDING timeout — %s not received — back to IDLE", missing)
            self._transition(State.IDLE)

    def _on_pending_id(self) -> None:
        self._pending_id_timer = None
        log.info("Pending ID timer fired")
        self._transmit_id("pending")

    def _on_id(self) -> None:
        self._id_timer = None
        log.info("Mandatory ID timer fired")
        self._cancel("pending_id")
        self._transmit_id("mandatory")
        self._last_id_time = self._loop.time()
        self._schedule_id()
        self._schedule_pending_id()

    # ── audio helpers ──────────────────────────────────────────────────────────

    def _transmit_id(self, id_type: str) -> None:
        if id_type == "initial":
            rotation = list(self.cfg.identity.initial_ids)
        elif id_type == "pending":
            rotation = list(self.cfg.identity.pending_ids)
        else:
            rotation = list(self.cfg.identity.mandatory_ids)

        if not rotation:
            log.warning("No %s ID messages configured", id_type)
            return

        idx      = self._id_rot.get(id_type, 0) % len(rotation)
        msg_name = rotation[idx]
        self._id_rot[id_type] = (idx + 1) % len(rotation)
        log.info("ID (%s) → '%s'", id_type, msg_name)

        was_ptt = self._cor
        if not was_ptt:
            self._set_ptt(True)
        try:
            self._play_message(msg_name)
        finally:
            if not was_ptt:
                self._set_ptt(False)

    def _play_message(self, name: str) -> None:
        elements = self.cfg.messages.get(name)
        if elements is None:
            log.error("Message '%s' not found in config", name)
            return
        if not elements:
            log.warning("Message '%s' has no elements", name)
            return
        for elem in elements:
            self._play_element(elem)

    def _play_element(self, elem: dict) -> None:
        etype = elem.get("type", "")
        sr    = self.cfg.audio.sample_rate

        if etype == "cw":
            text = elem.get("text", "")
            if not text:
                log.error("CW element has no 'text': %r", elem)
                return
            ac = self.cfg.audio
            log.info("CW: '%s'  %d WPM  %d Hz", text, ac.morse_wpm, ac.morse_pitch)
            samples = morse.render(text, ac.morse_wpm, ac.morse_pitch, ac.morse_level, sr)
            if samples.size > 0:
                self._engine.play_samples(samples, label=f"cw:{text}")

        elif etype == "voice":
            clip_name = elem.get("clip", "")
            if not clip_name:
                log.error("VOICE element has no 'clip': %r", elem)
                return
            if not self._vocab:
                log.error("No voice directories — cannot play '%s'", clip_name)
                return
            samples = self._vocab.get(clip_name)
            if samples is None:
                log.error("Voice clip '%s' not found", clip_name)
                return
            level = self.cfg.audio.voice_level
            if level != 1.0:
                samples = samples * level
            log.info("VOICE: '%s'  level=%.0f%%", clip_name, level * 100)
            self._engine.play_samples(
                samples, label=f"voice:{clip_name}", blocks_passthrough=True
            )

        elif etype in ("tone", "ct"):
            if "tone" in elem:
                tone_name = elem["tone"]
                elems = self._tones.get(tone_name)
                if elems:
                    audio = render_tone(elems, sample_rate=sr)
                    self._engine.play_samples(audio, label=f"tone:{tone_name}")
                    log.info("TONE: '%s'", tone_name)
                else:
                    log.error("Tone '%s' not in courtesy_tones", tone_name)
            elif "freq1" in elem:
                audio = render_tone(
                    [[float(elem.get("freq1", 0)), float(elem.get("freq2", 0)),
                      int(elem.get("ms", 80)), float(elem.get("amp", 0.8))]],
                    sample_rate=sr
                )
                self._engine.play_samples(audio, label="tone:inline")
            else:
                log.error("TONE element missing 'tone' or 'freq1': %r", elem)

        else:
            log.error("Unknown element type '%s': %r", etype, elem)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_ptt(self, active: bool) -> None:
        self._hw.set_ptt(active)
        self._engine.set_ptt(active)
        log.info("PTT %s", "ON" if active else "OFF")
        self._notify_listeners()

    def _transition(self, new_state: State) -> None:
        old = self.state
        self.state = new_state
        log.info("State: %s → %s", old.name, new_state.name)

        # Initial ID on first activation after a long idle period
        if new_state == State.ACTIVE and old in (State.IDLE, State.PENDING):
            elapsed = self._loop.time() - self._last_id_time
            if elapsed >= self.cfg.timers.id_interval and not self._initial_id_sent:
                self._initial_id_sent = True
                log.info("Initial ID (idle %.0fs)", elapsed)
                self._transmit_id("initial")
                self._last_id_time = self._loop.time()
                self._cancel("id")
                self._schedule_id()
                self._schedule_pending_id()

        if new_state == State.IDLE:
            self._initial_id_sent = False

        self._notify_listeners()

    # ── state-change notifications (for API server push events) ───────────────

    def add_state_listener(self, cb: Callable) -> None:
        if cb not in self._state_listeners:
            self._state_listeners.append(cb)

    def remove_state_listener(self, cb: Callable) -> None:
        self._state_listeners = [c for c in self._state_listeners if c is not cb]

    def _notify_listeners(self) -> None:
        snapshot = self.get_status()
        for cb in list(self._state_listeners):
            try:
                cb(snapshot)
            except Exception as exc:
                log.error("State listener error: %s", exc)

    def get_status(self) -> dict:
        """Return a snapshot of current port state (safe to call from any thread)."""
        return {
            "state":  self.state.name,
            "cor":    self._cor,
            "ctcss":  self._ctcss,
            "ptt":    self._engine._ptt,
            "access": self.cfg.ctcss.access_mode,
        }
