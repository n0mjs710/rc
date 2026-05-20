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
TAIL     — COR dropped, PTT still on; ct_delay → CT → hang timers running
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

from rc_config import PortConfig
from hardware  import CM119Hardware
from audio_engine import AudioEngine, VocabCache
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

    def __init__(self, cfg: PortConfig, hw: CM119Hardware,
                 engine: AudioEngine, messages: dict) -> None:
        self.cfg      = cfg
        self.name     = cfg.name
        self._hw      = hw
        self._engine  = engine
        self._messages = messages
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

        # Signal presence flags
        self._cor:   bool  = False
        self._ctcss: bool  = False
        self._cor_up_time: float = 0.0

        # TOT accumulation: only counts while signal is qualified; resets at CT
        self._tot_used:  float = 0.0   # accumulated on-air seconds this QSO
        self._tot_start: float = 0.0   # loop.time() when current ACTIVE began

        # ID rotation index per type
        self._id_rot: dict[str, int] = {"initial": 0, "mandatory": 0}
        self._last_id_time:       float = 0.0
        self._tx_activity:        bool  = False  # TX occurred since last ID
        self._initial_id_pending: bool  = False  # initial ID queued for COR drop (start of tail)
        self._pending_id_armed:   bool  = False  # pending-ID window is open
        self._voice_id_active:    bool  = False  # voice-element ID currently playing
        self._impolite_id_playing: bool = False  # impolite ID in progress; suppresses CT queueing
        self._id_epoch:           int   = 0      # incremented to signal interrupted coroutines

        # Timer handles
        self._hang_timer:     asyncio.TimerHandle | None = None   # PTT holdoff (hangup)
        self._ct_delay_timer: asyncio.TimerHandle | None = None   # pre-CT delay
        self._timeout_timer:  asyncio.TimerHandle | None = None
        self._ctcss_timer:    asyncio.TimerHandle | None = None
        self._id_timer:       asyncio.TimerHandle | None = None
        self._id_sub_timer:   asyncio.TimerHandle | None = None   # pending-ID window opener

        # State-change subscribers (for API push events)
        self._state_listeners: list[Callable] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._hw.add_cor_callback(self._on_cor_edge)
        self._hw.add_ctcss_callback(self._on_ctcss_edge)
        # Boot in quiet period — no ID timer until after the first initial ID fires.
        # For startup the initial ID plays inline after the startup message.
        # For the first user TX it fires at COR drop (start of tail).
        if self.cfg.events.startup_message:
            loop.create_task(self._play_startup())
        log.info("[%s] Port started — state=IDLE  access=%s",
                 self.name, self.cfg.access_mode)

    def stop(self) -> None:
        self._hw.remove_cor_callback(self._on_cor_edge)
        self._hw.remove_ctcss_callback(self._on_ctcss_edge)
        for name in ("hang", "ct_delay", "timeout", "ctcss", "id", "id_sub"):
            self._cancel(name)
        self._set_ptt(False)
        log.info("[%s] Port stopped.", self.name)

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
            await self._cor_active()
        else:
            await self._cor_idle()

    async def _cor_active(self) -> None:
        self._cor = True
        self._cor_up_time = self._loop.time()
        self._cancel("hang")
        self._cancel("ct_delay")
        am = self.cfg.access_mode
        log.info("COR ACTIVE  state=%s  access=%s", self.state.name, am)

        if am == "cor":
            if self.state in (State.IDLE, State.TAIL):
                self._set_ptt(True)
                self._transition(State.ACTIVE)

        elif am == "cor_ctcss":
            if self.state == State.PENDING and self._ctcss:
                self._cancel("ctcss")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
            elif self.state in (State.IDLE, State.TAIL):
                if self._ctcss:
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                else:
                    self._transition(State.PENDING)
                    self._schedule_ctcss_timeout()

        self._update_passthrough()

    async def _cor_idle(self) -> None:
        self._cor = False
        self._cancel("ctcss")
        duration = self._loop.time() - self._cor_up_time
        log.info("COR IDLE  held=%.2fs  state=%s", duration, self.state.name)

        if self.state == State.PENDING:
            self._cancel("timeout")
            self._transition(State.IDLE)

        elif self.state in (State.ACTIVE, State.TAIL):
            if self.state == State.ACTIVE and duration < self.cfg.timers.kerchunk:
                log.info("Kerchunk suppressed (%.2fs < %.2fs)",
                         duration, self.cfg.timers.kerchunk)
                self._cancel("timeout")
                self._tot_used = 0.0
                # Don't reward the kerchunker with a fast initial ID — cancel it
                # and let the full interval run before we ID.
                self._initial_id_pending = False
                if self._id_timer is None:
                    self._schedule_id()
                self._set_ptt(False)
                self._transition(State.IDLE)
            else:
                self._transition(State.TAIL)
                if self._pending_id_armed:
                    self._loop.create_task(self._do_pending_id())
                elif self._initial_id_pending:
                    self._initial_id_pending = False
                    self._loop.create_task(self._do_initial_id())
                elif not self._impolite_id_playing:
                    # Suppress CT delay while impolite ID is playing; it will
                    # fire one CT itself after the audio drains.
                    self._schedule_ct_delay()

        elif self.state == State.TIMEOUT:
            # Offending transmission ended; TX comes back up for cancel msg + hang,
            # just like the end of a normal QSO (minus ct_delay — the cancel msg
            # serves that purpose).
            log.info("COR idle after timeout — TX resuming for cancel message and hang")
            self._set_ptt(True)
            self._transition(State.TAIL)
            self._loop.create_task(self._do_timeout_recovery())

        self._update_passthrough()

    # ── CTCSS event handlers ──────────────────────────────────────────────────

    async def _handle_ctcss(self, active: bool) -> None:
        if active:
            await self._ctcss_active()
        else:
            await self._ctcss_idle()

    async def _ctcss_active(self) -> None:
        self._ctcss = True
        am = self.cfg.access_mode
        log.info("CTCSS ACTIVE  state=%s  access=%s", self.state.name, am)

        if am == "cor_ctcss":
            if self.state == State.PENDING and self._cor:
                self._cancel("ctcss")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
            elif self.state in (State.IDLE, State.TAIL):
                if self._cor:
                    self._cancel("ct_delay")
                    self._cancel("hang")
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                else:
                    self._transition(State.PENDING)
                    self._schedule_ctcss_timeout()

        self._update_passthrough()

    async def _ctcss_idle(self) -> None:
        self._ctcss = False
        am = self.cfg.access_mode
        log.info("CTCSS IDLE  state=%s", self.state.name)

        if am == "cor_ctcss" and self.state == State.ACTIVE:
            log.info("CTCSS idle in cor_ctcss mode → tail")
            self._transition(State.TAIL)
            if self._pending_id_armed:
                self._loop.create_task(self._do_pending_id())
            elif self._initial_id_pending:
                self._initial_id_pending = False
                self._loop.create_task(self._do_initial_id())
            elif not self._impolite_id_playing:
                self._schedule_ct_delay()
        elif self.state == State.PENDING:
            self._cancel("ctcss")
            self._transition(State.IDLE)

        self._update_passthrough()

    # ── timer scheduling ──────────────────────────────────────────────────────

    def _schedule_ct_delay(self) -> None:
        self._ct_delay_timer = self._loop.call_later(
            self.cfg.timers.ct_delay, self._on_ct_delay)

    def _schedule_hang(self) -> None:
        self._hang_timer = self._loop.call_later(self.cfg.timers.hang, self._on_hang)

    def _schedule_ctcss_timeout(self) -> None:
        self._ctcss_timer = self._loop.call_later(
            0.5, self._on_ctcss_timeout)   # 500 ms window to receive second signal

    def _schedule_id(self) -> None:
        self._cancel("id")
        self._cancel("id_sub")
        self._pending_id_armed = False
        self._voice_id_active  = False
        self._id_timer = self._loop.call_later(self.cfg.timers.id_interval, self._on_id)
        lead = self.cfg.timers.id_pending
        # Only arm the sub-timer if a pending_id message is actually configured,
        # and the window is narrower than the full interval.
        if self.cfg.events.pending_id and 0 < lead < self.cfg.timers.id_interval:
            self._id_sub_timer = self._loop.call_later(
                self.cfg.timers.id_interval - lead, self._on_id_sub)

    def _cancel(self, name: str) -> None:
        attr = f"_{name}_timer"
        h = getattr(self, attr, None)
        if h is not None:
            h.cancel()
            setattr(self, attr, None)

    # ── timer callbacks ───────────────────────────────────────────────────────

    def _on_ct_delay(self) -> None:
        self._ct_delay_timer = None
        if self.state != State.TAIL:
            return
        name = self.cfg.events.ct_message
        if name:
            log.info("CT delay — playing '%s'", name)
            self._play_message(name)
        # CT marks the end of a QSO session: reset the accumulated TOT so the
        # next operator starts with a clean timer.  TOT was already paused
        # (timer cancelled) when the signal dropped on entry to TAIL.
        self._tot_used = 0.0
        self._schedule_hang()

    def _on_hang(self) -> None:
        self._hang_timer = None
        if self._cor:
            return   # COR went active during hang; state machine will handle it
        self._set_ptt(False)
        self._transition(State.IDLE)

    def _on_timeout(self) -> None:
        self._timeout_timer = None
        log.warning("TOT — locking out repeater")
        # Transition first so _update_passthrough sees TIMEOUT and closes the gate.
        self._transition(State.TIMEOUT)
        self._update_passthrough()
        self._engine.clear_clips()
        # Play timeout message while PTT is still on, then drop PTT asynchronously
        # so the audio callback has time to consume the queued samples.
        self._loop.create_task(self._do_timeout_announce())

    def _on_ctcss_timeout(self) -> None:
        self._ctcss_timer = None
        if self.state == State.PENDING:
            missing = "COR" if self._ctcss else "CTCSS"
            log.warning("PENDING timeout — %s not received — back to IDLE", missing)
            self._transition(State.IDLE)

    def _on_id_sub(self) -> None:
        self._id_sub_timer = None
        log.debug("Pending ID window open — will ID at next COR drop")
        self._pending_id_armed = True

    def _on_id(self) -> None:
        self._id_timer = None
        self._cancel("id_sub")
        self._pending_id_armed = False
        # Mid-transmission always counts as activity — the user is on right now.
        if self.state == State.ACTIVE:
            self._tx_activity = True
        if not self._tx_activity:
            log.info("ID interval expired — no TX activity, entering quiet period")
            return
        if self.state == State.ACTIVE:
            log.warning("ID required over active QSO — transmitting impolite ID")
            self._loop.create_task(self._do_impolite_id())
        else:
            log.info("Mandatory ID timer fired")
            self._loop.create_task(self._do_mandatory_id())

    # ── async audio helpers ────────────────────────────────────────────────────

    async def _drain_clips(self) -> None:
        """Yield to the event loop until the audio engine's clip queue is empty."""
        while self._engine.is_playing():
            await asyncio.sleep(0.02)

    async def _do_timeout_announce(self) -> None:
        """Play the timeout message (PTT already on), then drop PTT."""
        name = self.cfg.events.timeout_message
        if name:
            self._play_message(name)
            await self._drain_clips()
        if self.state == State.TIMEOUT:
            self._set_ptt(False)

    async def _do_timeout_recovery(self) -> None:
        """After timeout clears: play cancel message (if configured), then hang."""
        self._note_tx_start()
        name = self.cfg.events.timeout_cancel_message
        if name:
            self._play_message(name)
            await self._drain_clips()
        if self.state == State.TAIL:
            self._schedule_hang()

    async def _do_mandatory_id(self) -> None:
        """Transmit mandatory ID, reset activity flag, restart the ID timer."""
        epoch = self._id_epoch
        await self._transmit_id("mandatory")
        if self._id_epoch != epoch:
            return
        self._last_id_time = self._loop.time()
        self._tx_activity = False
        self._schedule_id()

    async def _do_impolite_id(self) -> None:
        """ID over an active QSO at reduced CW level; collapses all COR activity
        that occurs during playback into a single CT at the end.

        Uses impolite_id if configured; falls back to the mandatory rotation.
        Never sets _voice_id_active — impolite IDs are not themselves interruptible.
        Suppresses per-COR-drop CT delays while playing (via _impolite_id_playing);
        fires exactly one CT after the audio drains if COR has dropped by then.
        """
        name = self.cfg.events.impolite_id
        if not name:
            rotation = list(self.cfg.events.mandatory_ids)
            if not rotation:
                log.warning("No impolite or mandatory ID configured")
                self._last_id_time = self._loop.time()
                self._schedule_id()
                return
            idx  = self._id_rot.get("mandatory", 0) % len(rotation)
            name = rotation[idx]
            self._id_rot["mandatory"] = (idx + 1) % len(rotation)

        log.info("Impolite ID → '%s'", name)
        self._impolite_id_playing = True
        saved = self.cfg.audio.morse_level
        self.cfg.audio.morse_level = self.cfg.audio.impolite_morse_level
        self._play_message(name)
        self.cfg.audio.morse_level = saved
        await self._drain_clips()
        self._impolite_id_playing = False

        self._last_id_time = self._loop.time()
        # Leave _tx_activity True (QSO still active).
        self._schedule_id()

        # If COR dropped while we were playing, state is TAIL — fire one CT now
        # to acknowledge the QSO, regardless of how many COR cycles happened.
        if self.state == State.TAIL and not self._cor:
            ct = self.cfg.events.ct_message
            if ct:
                self._play_message(ct)
            self._schedule_hang()

    async def _do_pending_id(self) -> None:
        """Play pending ID at the start of tail (before CT delay), reset ID cycle.

        Called from _cor_idle() / _ctcss_idle() when _pending_id_armed and COR
        drops cleanly.  Plays the message, drains, resets the ID cycle as if a
        mandatory ID had just fired, then hands off to the normal CT delay.
        """
        epoch = self._id_epoch
        name = self.cfg.events.pending_id
        if name:
            self._voice_id_active = self._message_has_voice(name)
            self._play_message(name)
            await self._drain_clips()
            self._voice_id_active = False
        if self._id_epoch != epoch:
            return
        self._last_id_time = self._loop.time()
        self._tx_activity = False
        self._schedule_id()
        if self.state == State.TAIL and not self._cor:
            self._schedule_ct_delay()

    async def _do_initial_id(self) -> None:
        """Play initial ID at COR drop (start of tail), reset ID cycle, hand to CT delay.

        Called from _cor_idle() / _ctcss_idle() when _initial_id_pending is set.
        PTT is already on (repeater is in TAIL).  Queues audio, drains, resets
        the ID cycle, then hands off to the normal CT-delay → hang → IDLE path.
        If COR goes active during the drain the epoch changes (via impolite-ID
        for voice IDs) or is detected below (for CW-only IDs); in either case
        we ensure the ID timer is running and bail without scheduling a CT delay.
        """
        epoch = self._id_epoch
        await self._transmit_id("initial")   # PTT already on; just queues audio
        await self._drain_clips()
        self._voice_id_active = False
        if self._id_epoch != epoch:
            # Interrupted (COR came back during a voice ID → impolite path took over).
            if self._id_timer is None:
                self._schedule_id()
            return
        self._last_id_time = self._loop.time()
        if self.state != State.ACTIVE:
            self._tx_activity = False
        if not self._id_timer:
            self._schedule_id()
        if self.state == State.TAIL and not self._cor:
            self._schedule_ct_delay()

    async def _play_startup(self) -> None:
        """Key TX at boot, play the startup message, then identify and go IDLE.

        The initial ID plays immediately after the startup audio — there is no
        COR drop to trigger it, so we handle it inline here.  No CT is played
        (CTs are user-facing QSO signals, not meaningful for system transmissions).
        """
        msg = self.cfg.events.startup_message
        if msg not in self._messages:
            log.warning("Startup message '%s' not found in config", msg)
            return
        log.info("Startup message → '%s'", msg)
        self._set_ptt(True)
        pre_s = self.cfg.audio.pre_message_ms / 1000.0
        if pre_s > 0 and self._message_needs_padding(msg):
            await asyncio.sleep(pre_s)
        self._play_message(msg)
        await self._drain_clips()
        # Identify immediately after startup — PTT is on so _transmit_id just queues.
        await self._transmit_id("initial")
        await self._drain_clips()
        self._voice_id_active = False
        self._last_id_time = self._loop.time()
        self._tx_activity = False
        self._schedule_id()
        post_s = self.cfg.audio.post_message_ms / 1000.0
        if post_s > 0:
            await asyncio.sleep(post_s)
        self._set_ptt(False)
        self._transition(State.IDLE)

    async def _transmit_id(self, id_type: str) -> None:
        if id_type == "initial":
            rotation = list(self.cfg.events.initial_ids)
        else:
            rotation = list(self.cfg.events.mandatory_ids)

        if not rotation:
            log.warning("No %s ID messages configured", id_type)
            return

        idx      = self._id_rot.get(id_type, 0) % len(rotation)
        msg_name = rotation[idx]
        self._id_rot[id_type] = (idx + 1) % len(rotation)
        log.info("ID (%s) → '%s'", id_type, msg_name)

        self._voice_id_active = self._message_has_voice(msg_name)

        state_before = self.state
        was_ptt = self._engine._ptt
        needs_pad = not was_ptt and self._message_needs_padding(msg_name)
        pre_s     = self.cfg.audio.pre_message_ms  / 1000.0
        post_s    = self.cfg.audio.post_message_ms / 1000.0
        if not was_ptt:
            self._set_ptt(True)
            if needs_pad and pre_s > 0:
                await asyncio.sleep(pre_s)
        self._play_message(msg_name)
        if not was_ptt:
            # PTT was off when we started; wait for audio to finish before
            # dropping it.  If the state machine took over PTT during the
            # await (e.g., COR went active, state changed), leave PTT alone.
            await self._drain_clips()
            self._voice_id_active = False
            if needs_pad and post_s > 0:
                await asyncio.sleep(post_s)
            if self.state == state_before:
                self._set_ptt(False)

    def _play_message(self, name: str) -> None:
        elements = self._messages.get(name)
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
            self._engine.play_samples(
                samples, label=f"voice:{clip_name}", blocks_passthrough=True
            )

        elif etype in ("tone", "ct"):
            if "freq1" in elem:
                audio = render_tone(
                    [[float(elem.get("freq1", 0)), float(elem.get("freq2", 0)),
                      int(elem.get("ms", 80)), float(elem.get("amp", 0.8))]],
                    sample_rate=sr
                )
                self._engine.play_samples(audio, label="tone:inline")
            else:
                log.error("TONE element missing 'freq1': %r", elem)

        else:
            log.error("Unknown element type '%s': %r", etype, elem)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _message_has_voice(self, name: str) -> bool:
        return any(e.get("type") == "voice"
                   for e in self._messages.get(name, []))

    def _message_needs_padding(self, name: str) -> bool:
        return any(e.get("type") in ("cw", "voice")
                   for e in self._messages.get(name, []))

    def _note_tx_start(self) -> None:
        """Record that the transmitter is going active for a non-ID reason.

        Call this from any code that brings PTT up outside the COR state
        machine (startup, timeout recovery, etc.).  Sets tx_activity and,
        in quiet period, queues the initial ID for the end of the next hang.
        """
        self._tx_activity = True
        if self._id_timer is None:
            self._initial_id_pending = True

    def _set_ptt(self, active: bool) -> None:
        self._hw.set_ptt(active)
        self._engine.set_ptt(active)
        log.info("PTT %s", "ON" if active else "OFF")
        self._notify_listeners()

    def _set_passthrough(self, active: bool) -> None:
        self._engine.set_passthrough(active)
        log.debug("Passthrough %s", "ON" if active else "OFF")

    def _update_passthrough(self) -> None:
        """Gate the RX audio source on live hardware signal state.

        During TIMEOUT the gate is always closed — the repeater is locked out
        and must not pass received audio regardless of COR/CTCSS state.
        Otherwise, gate tracks hardware signal edges directly so the audio
        engine closes the moment the signal drops, not when the state machine
        eventually reacts.
        """
        if self.state == State.TIMEOUT:
            self._set_passthrough(False)
            return
        am = self.cfg.access_mode
        if am == "cor_ctcss":
            active = self._cor and self._ctcss
        else:
            active = self._cor
        self._set_passthrough(active)

    def _transition(self, new_state: State) -> None:
        old = self.state
        self.state = new_state
        log.info("State: %s → %s", old.name, new_state.name)

        # TOT management: accumulates only while ACTIVE; pauses on unkey; resets at CT.
        # Timeout can only fire while the qualified signal is present — it never fires
        # during TAIL, even if the user dropped the signal fractions of a second before
        # the timer would have expired.
        if new_state == State.ACTIVE:
            self._tot_start = self._loop.time()
            self._cancel("timeout")
            self._timeout_timer = self._loop.call_later(
                max(0.0, self.cfg.timers.timeout - self._tot_used),
                self._on_timeout,
            )
            log.debug("TOT resumed — %.1fs used, %.1fs remaining",
                      self._tot_used, self.cfg.timers.timeout - self._tot_used)
        elif old == State.ACTIVE and new_state == State.TAIL:
            self._tot_used += self._loop.time() - self._tot_start
            self._cancel("timeout")
            log.debug("TOT paused — %.1fs accumulated", self._tot_used)
        elif new_state == State.TIMEOUT:
            self._tot_used = 0.0   # clean slate for post-timeout recovery

        # TX activity tracking and initial-ID queuing on ACTIVE transitions.
        if new_state == State.ACTIVE:
            self._tx_activity = True
            if old in (State.IDLE, State.PENDING) and self._id_timer is None:
                # First TX from quiet period — initial ID fires at next COR drop.
                self._initial_id_pending = True
            # If a voice-element ID is currently playing, interrupt it.
            # CW/tone IDs are readable over voice, so only voice IDs are cancelled.
            if self._voice_id_active and self._engine.is_playing():
                # Impolite ID takes over; discard any queued initial ID.
                self._initial_id_pending = False
                self._id_epoch += 1
                self._voice_id_active = False
                self._engine.clear_clips()
                self._loop.create_task(self._do_impolite_id())

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
            "access": self.cfg.access_mode,
        }
