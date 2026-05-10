#!/usr/bin/env python3
"""
Repeater Controller — Interactive Shell

A menu-driven, English-language configuration and simulation shell.
Run on any machine (no GPIO required) until you're ready to deploy to a Pi.

Usage:
  python3 rc_shell.py                 # start interactive shell
  python3 rc_shell.py myrepeater.toml # load config on startup

Navigation:
  messages / telemetry / configure    enter sub-menu by name
  simulate / test                     simulation and audio test
  back  or  cd ..                     go up one level
  cd /                                return to top
  cd configure/timers                 jump directly to any context
  /configure/audio                    absolute path as a command
"""

from __future__ import annotations
import cmd
import sys
from pathlib import Path
from enum import Enum, auto

from rc_config import (
    RepeaterConfig, apply_set_command,
    CONTEXT_FIELDS, FIELD_HINTS,
    _elem_display, _elems_display,
)


# ─────────────────────────────────────────────────────────────────────────────
# Simulation engine
# ─────────────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE        = auto()
    PENDING     = auto()   # waiting for the missing half of access requirement
    ACTIVE      = auto()   # COS up (and CTCSS if required), PTT on
    TAIL        = auto()   # COS dropped, PTT still on
    TIMEOUT     = auto()
    TRANSMIT    = auto()


class SimEvent:
    def __init__(self, offset_s: float, msg: str, state: State | None = None):
        self.offset_s = offset_s
        self.msg      = msg
        self.state    = state

    def __str__(self):
        m, s = divmod(int(self.offset_s), 60)
        state_tag = f"  [{self.state.name}]" if self.state else ""
        return f"  [{m:02d}:{s:02d}]  {self.msg}{state_tag}"


def _sim_tone_label(elem: dict) -> str:
    """Format a tone element for simulator log output."""
    if "tone" in elem:
        return elem["tone"]
    f1  = elem.get("freq1", 0)
    f2  = elem.get("freq2", 0)
    ms  = elem.get("ms", 0)
    amp = elem.get("amp", 0)
    if f1 <= 0 and f2 <= 0:
        return f"gap {int(ms)}ms"
    parts = f"{int(f1)}"
    if f2 > 0:
        parts += f"+{int(f2)}"
    return f"{parts}Hz {int(ms)}ms {amp}"


class Simulator:
    """
    Event-driven state machine for the repeater controller.
    Accepts manual events (cos_up, cos_down, ctcss_detected, etc.) and
    records a timeline of what the real controller would do.

    PENDING is bidirectional: either COS or CTCSS can arrive first.
    The decode window timer fires if the other signal doesn't show up in time.
    """

    def __init__(self, cfg: RepeaterConfig):
        self.cfg     = cfg
        self.state   = State.IDLE
        self.ptt     = False
        self.cos     = False
        self.ctcss   = False
        self.log: list[SimEvent] = []
        self._t      = 0.0
        self._cos_up_at: float | None = None
        self._pending: list[tuple[float, str]] = []
        self._id_rotation: dict[str, int] = {"initial": 0, "pending": 0, "mandatory": 0}
        self._record(f"Simulator started. Mode: {'MOCK' if cfg.hardware.mock else 'REAL GPIO'}")
        self._record(
            f"Access mode: {cfg.ctcss.access_mode}  |  "
            + (f"CTCSS decode: {cfg.ctcss.decode_freq} Hz  ({cfg.ctcss.decode_time_ms} ms)"
               if cfg.ctcss.decode_freq else "CTCSS decode: off")
        )

    # ── public event interface ───────────────────────────────────────────────

    def advance(self, seconds: float = 0.0) -> list[str]:
        before = len(self.log)
        self._t += seconds
        self._fire_pending()
        return [str(e) for e in self.log[before:]]

    def cos_up(self) -> list[str]:
        before = len(self.log)
        self.cos = True
        self._cos_up_at = self._t
        self._cancel_pending("tail")
        self._cancel_pending("hang")
        am = self.cfg.ctcss.access_mode

        if am == "cos":
            if self.state in (State.IDLE, State.TAIL):
                self._set_ptt(True)
                self._transition(State.ACTIVE)
            self._schedule(self.cfg.timers.timeout, "timeout")

        elif am in ("cos_ctcss", "ctcss_init"):
            if self.state == State.PENDING and self.ctcss:
                # CTCSS arrived first; COS now present → ACTIVE
                self._cancel_pending("ctcss_timeout")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule(self.cfg.timers.timeout, "timeout")
            elif self.state in (State.IDLE, State.TAIL):
                if self.ctcss:
                    self._set_ptt(True)
                    self._transition(State.ACTIVE)
                    self._schedule(self.cfg.timers.timeout, "timeout")
                else:
                    # COS first — wait for CTCSS
                    self._transition(State.PENDING)
                    decode_s = self.cfg.ctcss.decode_time_ms / 1000
                    self._record(f"COS first — waiting for CTCSS ({self.cfg.ctcss.decode_time_ms} ms window)")
                    self._schedule(decode_s, "ctcss_timeout")

        elif am == "ctcss":
            self._record("COS up (access mode = ctcss — CTCSS controls PTT)")

        return [str(e) for e in self.log[before:]]

    def cos_down(self) -> list[str]:
        before = len(self.log)
        self.cos = False
        self._cancel_pending("timeout")
        self._cancel_pending("ctcss_timeout")
        duration = self._t - (self._cos_up_at if self._cos_up_at is not None else self._t)
        am = self.cfg.ctcss.access_mode

        if self.state == State.PENDING:
            self._record("COS dropped during decode window — kerchunked or too short")
            self._transition(State.IDLE)

        elif self.state == State.ACTIVE:
            if duration < self.cfg.timers.kerchunk:
                self._record(f"COS duration {duration:.2f}s < kerchunk "
                             f"{self.cfg.timers.kerchunk}s — ignored")
                self._set_ptt(False)
                self._transition(State.IDLE)
            elif am == "ctcss":
                self._record("COS down (access mode = ctcss — CTCSS still controls)")
            else:
                self._transition(State.TAIL)
                self._record(f"Tail timer: {self.cfg.timers.tail*1000:.0f} ms")
                self._schedule(self.cfg.timers.hang, "hang")
                self._schedule(self.cfg.timers.tail, "tail")

        elif self.state == State.TAIL:
            pass

        return [str(e) for e in self.log[before:]]

    def ctcss_detected(self) -> list[str]:
        before = len(self.log)
        self.ctcss = True
        am = self.cfg.ctcss.access_mode
        self._record(f"CTCSS confirmed ({self.cfg.ctcss.decode_freq} Hz)")

        if am in ("cos_ctcss", "ctcss_init"):
            if self.state == State.PENDING and self.cos:
                # COS arrived first; CTCSS now confirmed → ACTIVE
                self._cancel_pending("ctcss_timeout")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule(self.cfg.timers.timeout, "timeout")
            elif self.state in (State.IDLE, State.TAIL):
                # CTCSS arrived first — wait for COS
                self._transition(State.PENDING)
                decode_s = self.cfg.ctcss.decode_time_ms / 1000
                self._record(f"CTCSS first — waiting for COS ({self.cfg.ctcss.decode_time_ms} ms window)")
                self._schedule(decode_s, "ctcss_timeout")

        elif am == "ctcss":
            if self.state in (State.IDLE, State.TAIL):
                self._cancel_pending("tail")
                self._cancel_pending("hang")
                self._set_ptt(True)
                self._transition(State.ACTIVE)
                self._schedule(self.cfg.timers.timeout, "timeout")

        return [str(e) for e in self.log[before:]]

    def ctcss_lost(self) -> list[str]:
        before = len(self.log)
        self.ctcss = False
        am = self.cfg.ctcss.access_mode
        self._record("CTCSS tone lost")

        if am in ("ctcss", "cos_ctcss") and self.state == State.ACTIVE:
            self._record(f"Access mode '{am}' — CTCSS loss forces tail")
            self._cancel_pending("timeout")
            self._transition(State.TAIL)
            self._schedule(self.cfg.timers.hang, "hang")
            self._schedule(self.cfg.timers.tail, "tail")

        elif am in ("cos", "ctcss_init"):
            self._record("CTCSS gone — COS still controls")

        elif self.state == State.PENDING:
            self._cancel_pending("ctcss_timeout")
            self._record("CTCSS lost during PENDING — returning to IDLE")
            self._transition(State.IDLE)

        return [str(e) for e in self.log[before:]]

    def dtmf(self, digit: str) -> list[str]:
        before = len(self.log)
        self._record(f"DTMF digit: {digit.upper()}")
        return [str(e) for e in self.log[before:]]

    def trigger_id(self, id_type: str = "mandatory") -> list[str]:
        """Fire an ID for the given type, rotating through the assigned message list."""
        before = len(self.log)
        c = self.cfg

        if id_type == "initial":
            rotation = list(c.identity.initial_ids)
        elif id_type == "pending":
            rotation = list(c.identity.pending_ids)
        else:
            id_type = "mandatory"
            rotation = list(c.identity.mandatory_ids)

        if not rotation:
            self._record(f"ID ({id_type}) — no messages assigned to this type")
            return [str(e) for e in self.log[before:]]

        idx      = self._id_rotation.get(id_type, 0) % len(rotation)
        msg_name = rotation[idx]
        self._id_rotation[id_type] = (idx + 1) % len(rotation)

        elements = c.messages.get(msg_name)
        if not elements:
            self._record(f"ID ({id_type}) → '{msg_name}'  [message not found or empty]")
            return [str(e) for e in self.log[before:]]

        self._record(f"ID ({id_type}) → '{msg_name}'  ({len(elements)} element(s))")
        for elem in elements:
            etype = elem.get("type", "")
            if etype == "cw":
                self._play_audio(
                    f"CW: {elem.get('text','')}  @ {c.audio.morse_wpm} WPM  {c.audio.morse_pitch} Hz"
                )
            elif etype == "voice":
                self._play_audio(f"VOICE: {elem.get('clip','')}")
            elif etype in ("tone", "ct"):
                self._play_audio(f"TONE: {_sim_tone_label(elem)}")
            elif etype == "time":
                self._play_audio("TIME: (system time readback)")
            else:
                self._record(f"  Unknown element type: '{etype}'")

        return [str(e) for e in self.log[before:]]

    def reset(self) -> None:
        self.state  = State.IDLE
        self.ptt    = False
        self.cos    = False
        self._t     = 0.0
        self._cos_up_at = None
        self._pending.clear()
        self._id_rotation = {"initial": 0, "pending": 0, "mandatory": 0}
        self.log.clear()
        self._record("Simulator reset")

    # ── internal helpers ─────────────────────────────────────────────────────

    def _record(self, msg: str, state: State | None = None):
        self.log.append(SimEvent(self._t, msg, state))

    def _transition(self, new_state: State):
        self._record(f"State: {self.state.name} → {new_state.name}", new_state)
        self.state = new_state

    def _set_ptt(self, on: bool):
        if on != self.ptt:
            self.ptt = on
            self._record(f"PTT {'ON  ← transmitter keyed' if on else 'OFF ← transmitter unkeyed'}")

    def _play_audio(self, desc: str):
        self._record(f"▶ AUDIO: {desc}")

    def _schedule(self, delay: float, action: str):
        self._cancel_pending(action)
        self._pending.append((self._t + delay, action))

    def _cancel_pending(self, action: str):
        self._pending = [(t, a) for t, a in self._pending if a != action]

    def _fire_pending(self):
        fired = [(t, a) for t, a in self._pending if t <= self._t]
        self._pending = [(t, a) for t, a in self._pending if t > self._t]
        for _, action in sorted(fired):
            self._on_timer(action)

    def _on_timer(self, action: str):
        if action == "hang":
            name = self.cfg.identity.ct_message
            elements = self.cfg.messages.get(name, [])
            if elements:
                self._record(f"Hang: playing message '{name}'")
                for elem in elements:
                    etype = elem.get("type", "")
                    if etype == "cw":         self._play_audio(f"CW: {elem.get('text','')}")
                    elif etype == "voice":    self._play_audio(f"VOICE: {elem.get('clip','')}")
                    elif etype in ("tone", "ct"): self._play_audio(f"TONE: {_sim_tone_label(elem)}")
            else:
                self._record(f"Hang: message '{name}' not found")

        elif action == "tail":
            if not self.cos:
                ste = self.cfg.ctcss.ste_mode
                if ste == "reverse_burst" and self.cfg.ctcss.encode_freq:
                    self._record(f"STE: 120° reverse burst ({self.cfg.ctcss.reverse_burst_ms} ms)")
                elif ste == "chicken_burst" and self.cfg.ctcss.encode_freq:
                    self._record(f"STE: chicken burst — CTCSS stopped "
                                 f"{self.cfg.ctcss.chicken_burst_ms} ms before carrier drop")
                self._set_ptt(False)
                self._transition(State.IDLE)

        elif action == "ctcss_timeout":
            if self.state == State.PENDING:
                missing = "COS" if self.ctcss else "CTCSS"
                self._record(f"Decode window elapsed — {missing} not received — returning to IDLE")
                self._transition(State.IDLE)

        elif action == "timeout":
            self._record("⚠ TIMEOUT — COS held too long, forcing PTT off")
            self._set_ptt(False)
            self._transition(State.TIMEOUT)
            # Play timeout message
            name = self.cfg.identity.timeout_message
            if name:
                elements = self.cfg.messages.get(name, [])
                if elements:
                    self._record(f"Timeout: playing message '{name}'")
                    for elem in elements:
                        etype = elem.get("type", "")
                        if etype in ("tone", "ct"): self._play_audio(f"TONE: {_sim_tone_label(elem)}")
                        elif etype == "cw":         self._play_audio(f"CW: {elem.get('text','')}")
                        elif etype == "voice":      self._play_audio(f"VOICE: {elem.get('clip','')}")

    def status(self) -> str:
        pending_str = ", ".join(
            f"{a}@+{(t - self._t):.1f}s" for t, a in sorted(self._pending)
        ) or "none"
        return (f"  State  : {self.state.name}\n"
                f"  PTT    : {'ON' if self.ptt else 'OFF'}\n"
                f"  COS    : {'UP' if self.cos else 'DOWN'}\n"
                f"  CTCSS  : {'PRESENT' if self.ctcss else 'absent'}\n"
                f"  Clock  : {self._t:.1f} s\n"
                f"  Timers : {pending_str}")


# ─────────────────────────────────────────────────────────────────────────────
# Menu context definitions
# ─────────────────────────────────────────────────────────────────────────────

_VALID_PATHS: dict[str, list[str]] = {
    "messages":   [],
    "telemetry":  [],
    "configure":  [],
    "simulate":   [],
    "test":       [],
    "timers":     ["configure"],
    "audio":      ["configure"],
    "hardware":   ["configure"],
    "ctcss":      ["configure"],
}

MENUS = {
    "main": {
        "title": "Main Menu",
        "items": [
            ("messages",   "Build, edit, and delete messages"),
            ("telemetry",  "Assign messages to ID, courtesy, timeout, etc."),
            ("configure",  "System settings (timers, audio, CTCSS, hardware)"),
            ("simulate",   "Run the simulator"),
            ("test",       "Test audio and hardware"),
            ("show",       "Show current configuration"),
            ("save",       "Save configuration to file"),
            ("load",       "Load configuration from file"),
        ],
    },
    "messages": {
        "title": "Messages — Build and Manage",
        "items": [
            ("list",                    "List all messages with elements"),
            ("show <name>",             "Show one message's elements in detail"),
            ("new <name>",              "Create an empty message"),
            ("add <n> cw <txt>",        "Append CW (Morse) element"),
            ("add <n> voice <w...>",    "Append VOICE elements (one per word)"),
            ("add <n> tone <f1> <f2> <ms> <amp>", "Append TONE element (f2=0 single, f1=0 silence)"),
            ("edit <name>",             "Interactive element editor"),
            ("clear <name>",            "Remove all elements from a message"),
            ("delete <name>",           "Delete message (removes from all slots)"),
            ("play <name>",             "Play a message (real audio)"),
            ("vocabulary [pattern]",     "List available voice words"),
        ],
    },
    "telemetry": {
        "title": "Telemetry — Assign Messages to Functions",
        "items": [
            ("list",                       "Show all assignments and rotations"),
            ("assign <msg> initial",       "Add to initial ID rotation"),
            ("assign <msg> pending",       "Add to pending ID rotation"),
            ("assign <msg> mandatory",     "Add to mandatory ID rotation"),
            ("assign <msg> ct",            "Set as courtesy tone (tail message)"),
            ("assign <msg> timeout",       "Set as timeout (TOT) message"),
            ("unassign <msg> [slot]",      "Remove from slot(s)"),
        ],
    },
    "configure": {
        "title": "Configure — System Settings",
        "items": [
            ("timers",     "Tail, hang, kerchunk, timeout, ID interval"),
            ("audio",      "Morse, voice volume, repeat gain"),
            ("ctcss",      "CTCSS encode/decode, access mode, STE"),
            ("hardware",   "GPIO pins, mock vs real"),
        ],
    },
    "simulate": {
        "title": "Simulate",
        "items": [
            ("cos up",            "Simulate carrier detect (COS high)"),
            ("cos down",          "Simulate carrier drop  (COS low)"),
            ("ctcss on",          "Simulate CTCSS tone detected"),
            ("ctcss off",         "Simulate CTCSS tone lost"),
            ("dtmf <d>",          "Simulate DTMF digit"),
            ("id [type]",         "Fire ID now  (initial / pending / mandatory)"),
            ("advance <s>",       "Advance simulated clock by N seconds"),
            ("log",               "Show full simulation log"),
            ("status",            "Show simulator state"),
            ("reset",             "Reset simulator"),
        ],
    },
    "test": {
        "title": "Audio / Hardware Test",
        "items": [
            ("play id",           "Play next ID from mandatory rotation"),
            ("play id <name>",    "Play a named message directly"),
            ("play morse <txt>",  "Play Morse code"),
            ("play voice <clip>", "Play a VOICE clip by name"),
            ("play ct",           "Play courtesy tone (tail message)"),
            ("vocabulary",        "List available voice words"),
            ("gpio",              "Show GPIO pin assignments"),
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Context-scoped command access
# ─────────────────────────────────────────────────────────────────────────────
# Which do_<name> commands are callable in each context.  Commands not
# in the set for the current context are rejected (use /path/cmd to
# reach them from elsewhere).

CONTEXT_COMMANDS: dict[str, frozenset[str]] = {
    "main":      frozenset({"messages", "telemetry", "configure", "simulate",
                            "test", "load", "set"}),
    "messages":  frozenset({"list", "new", "add", "edit", "clear",
                            "delete", "play", "vocabulary"}),
    "telemetry": frozenset({"list", "assign", "unassign"}),
    "configure": frozenset({"timers", "audio", "hardware", "ctcss"}),
    "timers":    frozenset({"set", "timers"}),
    "audio":     frozenset({"set", "audio"}),
    "hardware":  frozenset({"set", "hardware"}),
    "ctcss":     frozenset({"set", "ctcss"}),
    "simulate":  frozenset({"cos", "ctcss", "dtmf", "id", "advance", "log",
                            "status", "reset"}),
    "test":      frozenset({"play", "vocabulary", "gpio"}),
}

GLOBAL_COMMANDS = frozenset({
    "menu", "back", "cd", "quit", "exit", "q", "EOF", "help",
    "save", "show",
})


# ─────────────────────────────────────────────────────────────────────────────
# Shell
# ─────────────────────────────────────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════════╗
║       Repeater Controller Shell  v0.4                ║
║  Type 'menu' for navigation  |  'help' for commands  ║
║  Navigate: cd /  cd ..  cd configure/timers          ║
╚══════════════════════════════════════════════════════╝
"""


class RepeaterShell(cmd.Cmd):

    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.cfg  = RepeaterConfig()
        self.sim  = Simulator(self.cfg)
        self._ctx: list[str] = ["main"]
        self._config_path: str | None = config_path
        self._id_rotation: dict[str, int] = {"initial": 0, "pending": 0, "mandatory": 0}

        if config_path and Path(config_path).exists():
            try:
                self.cfg = RepeaterConfig.load(config_path)
                self.sim = Simulator(self.cfg)
                self._out(f"Loaded: {config_path}")
            except Exception as e:
                self._out(f"Could not load {config_path}: {e}")

        self._update_prompt()

    # ── prompt ───────────────────────────────────────────────────────────────

    def _update_prompt(self):
        ctx = "/".join(self._ctx[1:]) or ""
        tag = f"({ctx})" if ctx else ""
        self.prompt = f"\nrepeater{tag}> "

    def _ctx_name(self) -> str:
        return self._ctx[-1]

    def _go(self, *path: str) -> None:
        self._ctx = ["main", *path]
        self._update_prompt()

    def _pop(self) -> str:
        if len(self._ctx) > 1:
            self._ctx.pop()
        self._update_prompt()
        return self._ctx_name()

    # ── unix-style path navigation ────────────────────────────────────────────

    def _navigate_path(self, path: str) -> bool:
        path = path.strip()
        if not path or path == "/":
            self._go()
            self._show_menu()
            return True

        if path.startswith("/"):
            parts = [p for p in path.split("/") if p]
        else:
            current = list(self._ctx[1:])
            parts = current
            for segment in path.split("/"):
                if segment == "..":
                    if parts:
                        parts = parts[:-1]
                elif segment and segment != ".":
                    parts = parts + [segment]

        if not self._is_valid_nav_path(parts):
            return False

        self._go(*parts)
        self._show_menu()
        return True

    def _is_valid_nav_path(self, parts: list[str]) -> bool:
        if not parts:
            return True
        valid = set(MENUS.keys()) | {"timers", "audio", "hardware", "ctcss"}
        return all(p in valid for p in parts)

    # ── output helpers ───────────────────────────────────────────────────────

    def _out(self, text: str = ""):
        print(text)

    def _banner(self, title: str):
        w = 54
        self._out(f"\n{'─' * w}")
        self._out(f"  {title}")
        self._out(f"{'─' * w}")

    # ── menu display ─────────────────────────────────────────────────────────

    def _show_menu(self, name: str | None = None):
        name = name or self._ctx_name()
        if name not in MENUS:
            return
        m = MENUS[name]
        self._banner(m["title"])
        for cmd_name, desc in m["items"]:
            self._out(f"  {cmd_name:<28}  {desc}")
        if len(self._ctx) > 1:
            self._out(f"\n  {'back  /  cd ..':<28}  Return to previous menu")
        self._out(f"  {'menu':<28}  Show this menu")
        self._out(f"  {'quit':<28}  Exit\n")

    # ── tab completion ────────────────────────────────────────────────────────

    def completenames(self, text: str, *ignored):
        ctx = self._ctx_name()
        allowed = CONTEXT_COMMANDS.get(ctx, frozenset()) | GLOBAL_COMMANDS
        return sorted(name for name in allowed if name.startswith(text))

    def completedefault(self, text: str, line: str, begidx: int, endidx: int):
        parts = line[:begidx].strip().lower().split()
        cmd_name = parts[0] if parts else ""
        ctx = self._ctx_name()
        allowed = CONTEXT_COMMANDS.get(ctx, frozenset())

        # Only offer sub-completions for commands available in this context
        if cmd_name == "set" and "set" in allowed:
            fields = CONTEXT_FIELDS.get(ctx, [])
            suggestions = []
            for f in fields:
                kw = f.split("_")[0]
                if kw.startswith(text) and kw not in suggestions:
                    suggestions.append(kw)
            return suggestions

        if cmd_name == "show":
            if ctx == "messages":
                # show <name> — complete with message names
                return [n for n in self.cfg.messages if n.startswith(text)]
            else:
                return []

        if cmd_name in ("add", "edit", "clear", "delete", "play") and ctx == "messages":
            # Complete with message names
            return [n for n in self.cfg.messages if n.startswith(text)]

        if cmd_name == "assign" and "assign" in allowed:
            msg_names = list(self.cfg.messages.keys())
            slots = ["initial", "pending", "mandatory", "ct", "timeout"]
            return [o for o in msg_names + slots if o.startswith(text)]

        if cmd_name == "unassign" and "unassign" in allowed:
            msg_names = list(self.cfg.messages.keys())
            slots = ["initial", "pending", "mandatory", "ct", "timeout"]
            return [o for o in msg_names + slots if o.startswith(text)]

        if cmd_name == "play" and "play" in allowed and ctx != "messages":
            opts = ["id", "morse", "voice", "ct"]
            return [o for o in opts if o.startswith(text)]

        if cmd_name == "cd":
            ctx_path = self._ctx[1:]
            if ctx_path and ctx_path[-1] == "configure":
                children = ["timers", "audio", "hardware", "ctcss"]
            elif not ctx_path:
                children = ["messages", "telemetry", "configure", "simulate", "test"]
            else:
                children = [".."]
            opts = [".."] + children + ["/"]
            return [o for o in opts if o.startswith(text)]

        return []

    # ── emptyline: context-sensitive help ────────────────────────────────────

    def emptyline(self):
        self._show_menu()
        ctx = self._ctx_name()
        fields = CONTEXT_FIELDS.get(ctx)
        if fields:
            self._out(f"  Settable fields (use 'set <field> <value>'):\n")
            for f in fields:
                hint = FIELD_HINTS.get(f, f)
                self._out(f"    {hint}")
            self._out("")

    # ── built-in shell commands ───────────────────────────────────────────────

    def preloop(self):
        print(BANNER)
        self._show_menu()

    # ── context-scoped dispatch ────────────────────────────────────────────

    def onecmd(self, line: str):
        """Dispatch with context gating and /path/cmd support."""
        line = line.strip()
        if not line:
            return self.emptyline()

        # /path/command dispatch (but not "/ " which is cd /)
        if line.startswith("/"):
            return self._dispatch_absolute(line)

        # ".." shortcut
        if line == "..":
            return self.do_back("")

        # Parse the command name (first word)
        cmd_name = line.split(None, 1)[0]

        # Always allow global commands
        if cmd_name in GLOBAL_COMMANDS:
            return super().onecmd(line)

        # Check if command is allowed in current context
        ctx = self._ctx_name()
        allowed = CONTEXT_COMMANDS.get(ctx, frozenset())
        if cmd_name in allowed:
            return super().onecmd(line)

        # Not allowed here
        self._suggest_path(cmd_name)

    def _dispatch_absolute(self, line: str):
        """Handle /messages/list, /test/play morse HELLO, etc.

        Also handles bare navigation: /configure/timers
        """
        path = line[1:]  # strip leading /
        segments = [s for s in path.split("/") if s]

        if not segments:
            # bare "/"
            self._navigate_path("/")
            return

        # Try the full path as navigation first (e.g. /configure/timers)
        if self._is_valid_nav_path(segments):
            self._go(*segments)
            # Re-enter the target: run the do_<last> method to show context
            last = segments[-1]
            method = getattr(self, f"do_{last}", None)
            if method:
                method("")
            else:
                self._show_menu()
            return

        # Otherwise: last segment(s) are the command.
        # Walk backwards to find the split between path and command.
        for split in range(len(segments) - 1, 0, -1):
            nav_parts = segments[:split]
            cmd_text = " ".join(segments[split:])
            if self._is_valid_nav_path(nav_parts):
                target_ctx = nav_parts[-1]
                cmd_name = cmd_text.split(None, 1)[0]
                allowed = CONTEXT_COMMANDS.get(target_ctx, frozenset())
                if cmd_name not in allowed and cmd_name not in GLOBAL_COMMANDS:
                    self._out(f"  '{cmd_name}' is not available in "
                              f"/{'/'.join(nav_parts)}")
                    return
                # Save context, run command in target context, restore
                saved_ctx = list(self._ctx)
                self._ctx = ["main"] + list(nav_parts)
                self._update_prompt()
                try:
                    super().onecmd(cmd_text)
                finally:
                    self._ctx = saved_ctx
                    self._update_prompt()
                return

        self._out(f"  Unknown path: '{line}'")

    def _suggest_path(self, cmd_name: str):
        """Tell the user where a command is available."""
        homes = []
        for ctx, cmds in CONTEXT_COMMANDS.items():
            if cmd_name in cmds:
                # Build the full nav path
                parent = _VALID_PATHS.get(ctx, [])
                if parent:
                    homes.append(f"/{'/'.join(parent)}/{ctx}")
                elif ctx != "main":
                    homes.append(f"/{ctx}")
                else:
                    homes.append("/")
        if homes:
            self._out(f"  '{cmd_name}' is not available here.  Try: {', '.join(homes)}")
        else:
            self._out(f"  Unknown command: '{cmd_name}'  (type 'menu' or 'help')")

    def default(self, line: str):
        self._out(f"  Unknown command: '{line.strip()}'  (type 'menu' or 'help')")

    def do_menu(self, _):
        self._show_menu()

    def do_back(self, _):
        if len(self._ctx) > 1:
            self._pop()
            self._show_menu()
        else:
            self._out("  Already at the top level.")

    def do_cd(self, args: str):
        """Navigate like a unix filesystem.
          cd ..               go up one level
          cd /                return to root
          cd configure        enter configure
          cd configure/timers jump to timers
        """
        if not self._navigate_path(args.strip() or "/"):
            self._out(f"  Unknown path: '{args.strip()}'")
            self._out("  Valid paths: messages, telemetry, configure, simulate, test, "
                      "configure/timers, configure/audio, configure/hardware, configure/ctcss")

    def do_quit(self, _):
        self._out("Goodbye.")
        return True

    do_exit = do_quit
    do_q    = do_quit

    def do_EOF(self, _):
        print()
        return self.do_quit(_)

    # ── navigation into sub-menus ────────────────────────────────────────────

    def do_messages(self, args: str):
        """Enter the messages menu."""
        self._go("messages")
        if args.strip():
            # Route "messages list" etc. as a command in messages context
            super().onecmd(args.strip())
        else:
            self._show_menu()

    def do_telemetry(self, args: str):
        """Enter the telemetry menu, or show assignments."""
        if not args.strip():
            self._go("telemetry")
            self._show_telemetry()
            self._show_menu()
        else:
            # Allow "telemetry list", "telemetry assign ...", etc.
            parts = args.strip().split()
            sub = parts[0].lower()
            if sub == "list":
                self._show_telemetry()
            elif sub == "assign" and len(parts) >= 3:
                self._assign(parts[1], parts[2])
            elif sub == "unassign" and len(parts) >= 2:
                slot = parts[2] if len(parts) >= 3 else None
                self._unassign(parts[1], slot)
            else:
                self._go("telemetry")
                self._show_telemetry()
                self._show_menu()

    def do_configure(self, _):
        self._go("configure")
        self._show_menu()

    def do_simulate(self, args: str):
        if not args.strip():
            self._go("simulate")
            self._out(self.sim.status())
            self._show_menu()
        else:
            self._run_sim_command(args)

    def do_test(self, args: str):
        if not args.strip():
            self._go("test")
            self._show_menu()
        else:
            self._run_test_command(args)

    # ── configuration section entries ────────────────────────────────────────

    def do_timers(self, _):
        """Show timer settings and navigate to timers context."""
        self._go("configure", "timers")
        t = self.cfg.timers
        self._out("\n  Timer settings — use 'set' to change:\n")
        self._out(f"  Tail           : {t.tail*1000:.0f} ms  — PTT hold after COS drops")
        self._out(f"  Hang           : {t.hang*1000:.0f} ms  — delay before hang message")
        self._out(f"  Kerchunk       : {t.kerchunk*1000:.0f} ms  — minimum COS to respond")
        self._out(f"  Timeout        : {t.timeout:.0f} s   — TOT cutoff")
        self._out(f"  ID interval    : {t.id_interval:.0f} s   — mandatory ID period "
                  f"({t.id_interval/60:.1f} min)")
        self._out(f"  ID pending     : {t.id_pending:.0f} s   — queue pending ID this early")
        self._out("\n  bare number = ms for tail/hang/kerchunk, s for timeout/id timers:\n")
        self._out("    set tail 2500           set tail 2.5s")
        self._out("    set hang 500            set hang 0.5s")
        self._out("    set kerchunk 500        set kerchunk 500ms")
        self._out("    set timeout 180         set timeout 3m")
        self._out("    set id interval 600     set id interval 10m")
        self._out("    set id pending 30")

    def do_audio(self, _):
        """Show audio settings and navigate to audio context."""
        self._go("configure", "audio")
        c = self.cfg.audio
        self._out("\n  Audio settings — use 'set' to change:\n")
        self._out(f"  Morse speed    : {c.morse_wpm} WPM")
        self._out(f"  Morse pitch    : {c.morse_pitch} Hz")
        self._out(f"  Morse volume   : {c.morse_volume}%")
        self._out(f"  Voice volume   : {c.voice_volume}%")
        self._out(f"  Repeat gain    : {c.repeat_gain:.2f}x  "
                  f"(scales RX passthrough before re-transmission)")
        self._out(f"  Voice blocks   : {'yes' if c.voice_blocks_repeat else 'no'}  "
                  f"(mute RX passthrough while VOICE plays)")
        self._out("\n  Examples:")
        self._out("    set morse wpm 20")
        self._out("    set voice volume 80")
        self._out("    set repeat gain 1.5")
        self._out("    set voice blocks on")

    def do_hardware(self, _):
        """Show hardware settings and navigate to hardware context."""
        self._go("configure", "hardware")
        c = self.cfg.hardware
        self._out("\n  Hardware settings — use 'set' to change:\n")
        self._out(f"  Mode           : {'SIMULATED' if c.mock else 'REAL GPIO'}")
        self._out(f"  PTT GPIO pin   : {c.ptt_gpio}")
        self._out(f"  COS GPIO pin   : {c.cos_gpio}")
        self._out(f"  COS polarity   : {'active-low (inverted)' if c.cos_invert else 'active-high'}")
        self._out("\n  Examples:")
        self._out("    set mock on          (simulate GPIO)")
        self._out("    set mock off         (use real GPIO on Pi)")
        self._out("    set ptt gpio 17")
        self._out("    set cos gpio 27")
        self._out("    set cos invert on")

    def do_ctcss(self, args: str):
        """Configure CTCSS (no args), or simulate ctcss on/off."""
        a = args.strip().lower()
        if a in ("on", "off", "detected", "lost", "present", "gone", "up", "down"):
            self._run_sim_command(f"ctcss {args}")
        else:
            self._go("configure", "ctcss")
            self._show_ctcss_settings()

    def _show_ctcss_settings(self):
        c = self.cfg.ctcss
        self._out("\n  CTCSS settings — use 'set' to change:\n")
        self._out(f"  Mode           : {c.mode.upper()}")
        self._out(f"  Access mode    : {c.access_mode}")
        self._out(f"  Encode freq    : "
                  + (f"{c.encode_freq} Hz  (level {c.encode_level:.0%})"
                     if c.encode_freq else "disabled"))
        self._out(f"  Decode freq    : "
                  + (f"{c.decode_freq} Hz  ({c.decode_time_ms} ms window, "
                     f"threshold {c.decode_threshold})"
                     if c.decode_freq else "disabled"))
        self._out(f"  Decode hold    : {c.decode_hold_ms} ms")
        ste_detail = {
            "reverse_burst": f"  ({c.reverse_burst_ms} ms, 120° Motorola)",
            "chicken_burst": f"  ({c.chicken_burst_ms} ms CTCSS lead drop)",
        }.get(c.ste_mode, "")
        self._out(f"  STE            : {c.ste_mode}{ste_detail}")
        self._out("\n  Access modes:  cos | ctcss | cos_ctcss | ctcss_init")
        self._out("  Note: in cos_ctcss/ctcss_init, either COS or CTCSS may arrive")
        self._out("        first — controller waits for the other within decode window.")
        self._out("  STE modes:     none | reverse_burst | chicken_burst\n")
        self._out("  Examples:")
        self._out("    set ctcss encode freq 100.0")
        self._out("    set ctcss decode freq 100.0")
        self._out("    set ctcss access ctcss_init")
        self._out("    set ste reverse burst")

    # ── set / show ────────────────────────────────────────────────────────────

    def do_set(self, args: str):
        """Set a configuration value in plain English."""
        result = apply_set_command(self.cfg, args)
        self._out(result)
        self.sim.cfg = self.cfg

    def do_show(self, args: str):
        """Context-sensitive show — displays config for the current level.
          show                 show config for current context
          show <name>          (messages) show a specific message's elements
        """
        ctx = self._ctx_name()
        a = args.strip()

        if ctx == "messages":
            if a:
                self._msg_show(a.split()[0])
            else:
                self._msg_list()
        elif ctx == "telemetry":
            self._show_telemetry()
        elif ctx == "timers":
            t = self.cfg.timers
            self._out(f"\n  Tail           : {t.tail*1000:.0f} ms")
            self._out(f"  Hang           : {t.hang*1000:.0f} ms")
            self._out(f"  Kerchunk       : {t.kerchunk*1000:.0f} ms")
            self._out(f"  Timeout        : {t.timeout:.0f} s")
            self._out(f"  ID interval    : {t.id_interval:.0f} s  ({t.id_interval/60:.1f} min)")
            self._out(f"  ID pending     : {t.id_pending:.0f} s")
        elif ctx == "audio":
            c = self.cfg.audio
            self._out(f"\n  Morse speed    : {c.morse_wpm} WPM")
            self._out(f"  Morse pitch    : {c.morse_pitch} Hz")
            self._out(f"  Morse volume   : {c.morse_volume}%")
            self._out(f"  Voice volume   : {c.voice_volume}%")
            self._out(f"  Repeat gain    : {c.repeat_gain:.2f}x")
            self._out(f"  Voice blocks   : {'yes' if c.voice_blocks_repeat else 'no'}")
        elif ctx == "hardware":
            c = self.cfg.hardware
            self._out(f"\n  Mode           : {'SIMULATED' if c.mock else 'REAL GPIO'}")
            self._out(f"  PTT GPIO pin   : {c.ptt_gpio}")
            self._out(f"  COS GPIO pin   : {c.cos_gpio}")
            self._out(f"  COS polarity   : {'active-low' if c.cos_invert else 'active-high'}")
        elif ctx == "ctcss":
            self._show_ctcss_settings()
        elif ctx == "simulate":
            self._out("\n" + self.sim.status())
        elif ctx == "configure":
            # One-level summary of all sub-sections
            t = self.cfg.timers
            c = self.cfg.audio
            h = self.cfg.hardware
            self._out(f"\n  Timers   : tail={t.tail*1000:.0f}ms  hang={t.hang*1000:.0f}ms  "
                      f"kerchunk={t.kerchunk*1000:.0f}ms  timeout={t.timeout:.0f}s  "
                      f"id={t.id_interval:.0f}s")
            self._out(f"  Audio    : morse={c.morse_wpm}WPM/{c.morse_pitch}Hz  "
                      f"voice={c.voice_volume}%  gain={c.repeat_gain:.2f}x")
            self._out(f"  Hardware : {'SIMULATED' if h.mock else 'REAL GPIO'}  "
                      f"PTT={h.ptt_gpio}  COS={h.cos_gpio}")
            self._out(f"  CTCSS    : {self.cfg.ctcss.mode}  "
                      f"encode={self.cfg.ctcss.encode_freq}Hz  "
                      f"decode={self.cfg.ctcss.decode_freq}Hz")
        else:
            # main / root — full config
            self._out("\n" + self.cfg.describe())

    def do_status(self, args: str):
        """Show simulator state (in simulate context) or full config."""
        if self._ctx_name() == "simulate":
            self._out("\n" + self.sim.status())
        else:
            self.do_show(args)

    # ── telemetry management ──────────────────────────────────────────────────

    def do_assign(self, args: str):
        """Assign a message to a telemetry slot.
          assign <msg> initial      Add to initial ID rotation
          assign <msg> pending      Add to pending ID rotation
          assign <msg> mandatory    Add to mandatory ID rotation
          assign <msg> ct           Set as courtesy tone (tail message)
          assign <msg> timeout      Set as timeout (TOT) message
        """
        parts = args.strip().split()
        if len(parts) < 2:
            self._out("  Usage: assign <message_name> <slot>")
            self._out("  Slots: initial, pending, mandatory, ct, timeout")
            return
        self._assign(parts[0], parts[1])

    def do_unassign(self, args: str):
        """Remove a message from telemetry slot(s).
          unassign <msg>            Remove from all slots
          unassign <msg> mandatory  Remove from one slot
        """
        parts = args.strip().split()
        if not parts:
            self._out("  Usage: unassign <message_name> [slot]")
            return
        slot = parts[1] if len(parts) >= 2 else None
        self._unassign(parts[0], slot)

    def do_list(self, _):
        """List items in the current context."""
        ctx = self._ctx_name()
        if ctx == "messages":
            self._msg_list()
        elif ctx == "telemetry":
            self._show_telemetry()
        else:
            self._out("  Nothing to list here.")

    def _show_telemetry(self):
        c = self.cfg.identity
        t = self.cfg.timers
        self._out(f"\n  ── ID Rotations ──────────────────────────────────\n")
        self._out(f"    ID interval  : {t.id_interval:.0f} s  ({t.id_interval/60:.1f} min)")
        self._out(f"    ID pending   : {t.id_pending:.0f} s before deadline")
        self._out(f"\n    Initial      : {', '.join(c.initial_ids)   or '(none assigned)'}")
        self._out(f"    Pending      : {', '.join(c.pending_ids)   or '(none assigned)'}")
        self._out(f"    Mandatory    : {', '.join(c.mandatory_ids) or '(none assigned)'}")
        self._out(f"\n  ── Message Slots ─────────────────────────────────\n")
        self._out(f"    CT           : {c.ct_message      or '(none)'}  ← courtesy tone (tail message)")
        self._out(f"    Timeout      : {c.timeout_message or '(none)'}  ← plays on TOT")
        self._out(f"\n  ── Message Pool ({len(self.cfg.messages)} messages) ──────────────────\n")
        for name in sorted(self.cfg.messages):
            elems    = self.cfg.messages[name]
            elem_str = _elems_display(elems, self.cfg.courtesy_tones)
            self._out(f"    {name:<20}  {elem_str}")
        self._out(f"\n  Examples:")
        self._out(f"    assign my_id mandatory")
        self._out(f"    assign my_ct ct")
        self._out(f"    unassign old_id mandatory")

    def _assign(self, name: str, slot: str):
        slot = slot.lower()
        c = self.cfg.identity

        if slot in ("initial", "pending", "mandatory"):
            if name not in self.cfg.messages:
                self._out(f"  Message '{name}' not found — create it first with 'new'")
                return
            lst = getattr(c, f"{slot}_ids")
            if name not in lst:
                lst.append(name)
                self._out(f"  '{name}' added to {slot} rotation "
                          f"({len(lst)} message{'s' if len(lst) != 1 else ''} in rotation)")
            else:
                self._out(f"  '{name}' is already in {slot} rotation")

        elif slot == "ct":
            if name not in self.cfg.messages:
                self._out(f"  Message '{name}' not found")
                return
            c.ct_message = name
            self._out(f"  CT message → '{name}'")

        elif slot == "timeout":
            if name not in self.cfg.messages:
                self._out(f"  Message '{name}' not found")
                return
            c.timeout_message = name
            self._out(f"  Timeout message → '{name}'")

        else:
            self._out(f"  Slot must be: initial, pending, mandatory, ct, or timeout")

    def _unassign(self, name: str, slot: str | None = None):
        c = self.cfg.identity
        removed = []

        if slot is None or slot in ("initial", "pending", "mandatory"):
            for stype in ("initial", "pending", "mandatory"):
                if slot and stype != slot:
                    continue
                lst = getattr(c, f"{stype}_ids")
                if name in lst:
                    lst.remove(name)
                    removed.append(stype)

        if slot is None or slot == "ct":
            if c.ct_message == name:
                c.ct_message = ""
                removed.append("ct")

        if slot is None or slot == "timeout":
            if c.timeout_message == name:
                c.timeout_message = ""
                removed.append("timeout")

        if removed:
            self._out(f"  '{name}' removed from: {', '.join(removed)}")
        else:
            self._out(f"  '{name}' was not assigned to any{'  (slot: '+slot+')' if slot else ''} slot")

    # ── message management ──────────────────────────────────────────────────
    # These do_* methods are the flat commands available in the messages context.

    def do_new(self, args: str):
        """Create an empty message: new <name>"""
        name = args.strip().split()[0] if args.strip() else ""
        if not name:
            self._out("  Usage: new <name>")
            return
        self._msg_new(name)

    def do_add(self, args: str):
        """Append an element: add <name> cw|voice|tone <content...>"""
        parts = args.strip().split()
        if len(parts) < 3:
            self._out("  Usage: add <name> cw|voice|tone <content...>")
            return
        self._msg_add_element(parts[0], parts[1], " ".join(parts[2:]))

    def do_edit(self, args: str):
        """Interactive element editor: edit <name>"""
        name = args.strip().split()[0] if args.strip() else ""
        if not name:
            self._out("  Usage: edit <name>")
            return
        self._msg_edit(name)

    def do_clear(self, args: str):
        """Remove all elements from a message: clear <name>"""
        name = args.strip().split()[0] if args.strip() else ""
        if not name:
            self._out("  Usage: clear <name>")
            return
        self._msg_clear(name)

    def do_delete(self, args: str):
        """Delete a message: delete <name>"""
        name = args.strip().split()[0] if args.strip() else ""
        if not name:
            self._out("  Usage: delete <name>")
            return
        self._msg_delete(name)

    def do_vocabulary(self, args: str):
        """List available voice words.  Supports wildcards: vocabulary G*"""
        self._show_vocabulary(args.strip())

    def _msg_list(self):
        msgs = self.cfg.messages
        if not msgs:
            self._out("  (no messages defined)")
            return
        self._out(f"\n  Messages ({len(msgs)}):\n")
        self._out(f"  {'Name':<20}  Elements")
        self._out(f"  {'─'*20}  {'─'*40}")
        for name in sorted(msgs):
            elems    = msgs[name]
            elem_str = _elems_display(elems, self.cfg.courtesy_tones)
            self._out(f"  {name:<20}  {elem_str}")

    def _msg_show(self, name: str):
        elems = self.cfg.messages.get(name)
        if elems is None:
            avail = ", ".join(sorted(self.cfg.messages)) or "(none)"
            self._out(f"  Message '{name}' not found.  Available: {avail}")
            return
        self._out(f"\n  Message : {name}")
        self._out(f"  Elements: {len(elems)}")
        if not elems:
            self._out("    (empty)")
        else:
            for i, e in enumerate(elems, 1):
                self._out(f"    {i}. {_elem_display(e, self.cfg.courtesy_tones)}")

    def _msg_new(self, name: str):
        if name in self.cfg.messages:
            self._out(f"  Message '{name}' already exists — use 'show {name}' or 'edit {name}'")
            return
        self.cfg.messages[name] = []
        self._out(f"  Created empty message '{name}' — use 'add {name} cw|voice|tone ...' to add elements")

    def _msg_add_element(self, name: str, etype: str, content: str):
        if name not in self.cfg.messages:
            self._out(f"  Message '{name}' not found — create it first with 'new {name}'")
            return
        etype = etype.lower()
        # Accept "ct" as legacy alias for "tone"
        if etype == "ct":
            etype = "tone"
        if etype == "cw":
            elem: dict = {"type": "cw", "text": content.upper()}
            self.cfg.messages[name].append(elem)
            n = len(self.cfg.messages[name])
            self._out(f"  '{name}' element {n}: {_elem_display(elem, self.cfg.courtesy_tones)}")
        elif etype == "voice":
            # Each word is a separate clip (each .wav file is one word)
            words = content.upper().split()
            if not words:
                self._out(f"  No clip names given — usage: add {name} voice CLIP1 CLIP2 ...")
                return
            for word in words:
                elem = {"type": "voice", "clip": word}
                self.cfg.messages[name].append(elem)
                n = len(self.cfg.messages[name])
                self._out(f"  '{name}' element {n}: {_elem_display(elem, self.cfg.courtesy_tones)}")
        elif etype == "tone":
            parts = content.split()
            # Inline tone: 4 numbers → freq1 freq2 ms amplitude
            if parts and parts[0].replace('.','',1).replace('-','',1).isdigit():
                try:
                    f1  = float(parts[0])
                    f2  = float(parts[1]) if len(parts) > 1 else 0.0
                    ms  = int(float(parts[2])) if len(parts) > 2 else 80
                    amp = float(parts[3]) if len(parts) > 3 else 0.8
                except (ValueError, IndexError):
                    self._out("  Usage: add <msg> tone <freq1> <freq2> <ms> <amplitude>")
                    self._out("  Example: add my_msg tone 1000 0 80 0.8")
                    return
                if not (0.0 <= amp <= 1.0):
                    self._out(f"  Amplitude must be 0.0–1.0, got {amp}")
                    return
                elem = {"type": "tone", "freq1": f1, "freq2": f2,
                        "ms": ms, "amp": amp}
            else:
                self._out("  Usage: add <msg> tone <freq1> <freq2> <ms> <amplitude>")
                self._out("  Example: add my_msg tone 1000 0 80 0.8")
                self._out("           add my_msg tone 0 0 30 0      (silence gap)")
                return
            self.cfg.messages[name].append(elem)
            n = len(self.cfg.messages[name])
            self._out(f"  '{name}' element {n}: {_elem_display(elem, self.cfg.courtesy_tones)}")
        else:
            self._out(f"  Element type must be cw, voice, or tone — got '{etype}'")

    def _msg_clear(self, name: str):
        if name not in self.cfg.messages:
            self._out(f"  Message '{name}' not found")
            return
        old_count = len(self.cfg.messages[name])
        self.cfg.messages[name] = []
        self._out(f"  Cleared {old_count} element(s) from '{name}'")

    def _msg_edit(self, name: str):
        """Interactively edit a message's element list."""
        if name not in self.cfg.messages:
            self._out(f"  Message '{name}' not found")
            return
        elements = self.cfg.messages[name]

        def show_elements():
            self._out(f"\n  '{name}' — {len(elements)} element(s):")
            if not elements:
                self._out("    (empty)")
            else:
                for i, e in enumerate(elements, 1):
                    self._out(f"    {i}. {_elem_display(e, self.cfg.courtesy_tones)}")

        def show_help():
            self._out("")
            self._out("    add cw <text>                    append CW element")
            self._out("    add voice <words...>             append VOICE elements")
            self._out("    add tone <f1> <f2> <ms> <amp>   append TONE element")
            self._out("    del <n>                          delete element n")
            self._out("    move <from> <to>                 move element to new position")
            self._out("    done                             finish editing")
            self._out("")

        show_elements()
        show_help()

        while True:
            try:
                inp = input("  edit> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._out("\n  Edit cancelled.")
                return

            if not inp:
                show_elements()
                show_help()
                continue

            ep = inp.split()
            ecmd = ep[0].lower()

            if ecmd == "done":
                break

            elif ecmd == "del" and len(ep) >= 2:
                try:
                    n = int(ep[1]) - 1
                    if 0 <= n < len(elements):
                        removed = elements.pop(n)
                        self._out(f"    removed {n+1}: {_elem_display(removed, self.cfg.courtesy_tones)}")
                        show_elements()
                    else:
                        self._out(f"    invalid — message has {len(elements)} elements")
                except ValueError:
                    self._out("    del <number>")

            elif ecmd == "move" and len(ep) >= 3:
                try:
                    src = int(ep[1]) - 1
                    dst = int(ep[2]) - 1
                    if 0 <= src < len(elements) and 0 <= dst < len(elements):
                        elem = elements.pop(src)
                        elements.insert(dst, elem)
                        self._out(f"    moved {src+1} → {dst+1}")
                        show_elements()
                    else:
                        self._out(f"    invalid — message has {len(elements)} elements")
                except ValueError:
                    self._out("    move <from> <to>")

            elif ecmd == "add" and len(ep) >= 3:
                self._msg_add_element(name, ep[1], " ".join(ep[2:]))
                show_elements()

            else:
                show_help()

        show_elements()
        self._out("")

    def _msg_delete(self, name: str):
        if name not in self.cfg.messages:
            self._out(f"  Message '{name}' not found")
            return
        del self.cfg.messages[name]
        # Remove from all telemetry slots
        c = self.cfg.identity
        for lst in (c.initial_ids, c.pending_ids, c.mandatory_ids):
            while name in lst:
                lst.remove(name)
        if c.ct_message == name:
            c.ct_message = ""
        if c.timeout_message == name:
            c.timeout_message = ""
        self._out(f"  Deleted message '{name}' and removed from all slots")

    # ── id command (simulator + shorthand) ────────────────────────────────────

    def do_id(self, args: str):
        """Fire a simulated ID, or shorthand message create.

        Simulator:
          id                               Fire next mandatory ID
          id initial                       Fire next initial ID
          id pending                       Fire next pending ID
          id mandatory                     Fire next mandatory ID

        Shorthand message create (single element):
          id add <name> cw <text>          Create CW message and add to pool
          id add <name> voice <words>      Create VOICE message and add to pool
          id add <name> tone <f1> <f2> <ms> <amp>  Create TONE message and add to pool
        """
        parts = args.strip().split()
        subcmd = parts[0].lower() if parts else ""

        if subcmd == "add":
            if len(parts) < 4:
                self._out("  Usage: id add <name> cw|voice|tone <content...>")
            else:
                self._id_add_shorthand(parts[1], parts[2], " ".join(parts[3:]))

        elif subcmd == "list":
            self._show_telemetry()

        elif subcmd in ("assign", "unassign"):
            self._out(f"  Use the '{subcmd}' command directly (it's now in telemetry)")
            self._out(f"  Example: {subcmd} my_msg mandatory")

        elif subcmd in ("show", "delete"):
            self._out(f"  Use '{subcmd}' in the /messages context  (cd /messages, then '{subcmd} <name>')")

        else:
            id_type = subcmd if subcmd in ("initial", "pending", "mandatory") else "mandatory"
            for line in self.sim.trigger_id(id_type):
                self._out(line)

    def _id_add_shorthand(self, name: str, etype: str, content: str):
        """Create a message and add it to the pool."""
        etype = etype.lower()
        if etype == "ct":
            etype = "tone"
        if etype == "cw":
            elems = [{"type": "cw", "text": content.upper()}]
        elif etype == "voice":
            # Each word is a separate clip (each .wav file is one word)
            words = content.upper().split()
            if not words:
                self._out(f"  No clip names given")
                return
            elems = [{"type": "voice", "clip": w} for w in words]
        elif etype == "tone":
            parts = content.split()
            if parts and parts[0].replace('.','',1).replace('-','',1).isdigit():
                try:
                    f1  = float(parts[0])
                    f2  = float(parts[1]) if len(parts) > 1 else 0.0
                    ms  = int(float(parts[2])) if len(parts) > 2 else 80
                    amp = float(parts[3]) if len(parts) > 3 else 0.8
                except (ValueError, IndexError):
                    self._out("  Usage: id add <name> tone <freq1> <freq2> <ms> <amplitude>")
                    return
                if not (0.0 <= amp <= 1.0):
                    self._out(f"  Amplitude must be 0.0–1.0, got {amp}")
                    return
                elems = [{"type": "tone", "freq1": f1, "freq2": f2,
                          "ms": ms, "amp": amp}]
            else:
                self._out("  Usage: id add <name> tone <freq1> <freq2> <ms> <amplitude>")
                return
        else:
            self._out(f"  Element type must be cw, voice, or tone — got '{etype}'")
            return
        self.cfg.messages[name] = elems
        display = _elems_display(elems, self.cfg.courtesy_tones)
        self._out(f"  Created message '{name}': {display}")
        self._out(f"  (use 'assign {name} mandatory' to add it to a rotation)")

    # ── vocabulary display ─────────────────────────────────────────────────────

    def _show_vocabulary(self, pattern: str = ""):
        """List available voice words in columns.  Supports glob patterns (e.g. G*)."""
        import fnmatch

        dirs = []
        for dirname in ("user_pcm", "vocab_pcm"):
            d = Path(__file__).parent / dirname
            if d.exists():
                dirs.append(d)

        if not dirs:
            self._out("  No voice directories found (vocab_pcm, user_pcm)")
            self._out("  Drop .wav files into user_pcm/ to add voice content")
            return

        seen: set[str] = set()
        all_words: list[str] = []
        for d in dirs:
            for wav in sorted(d.glob("*.wav")):
                name = wav.stem.upper()
                if name not in seen:
                    seen.add(name)
                    all_words.append(name)
        all_words.sort()

        if not all_words:
            self._out("  No .wav files found in voice directories")
            return

        if pattern:
            pat = pattern.upper()
            words = [w for w in all_words if fnmatch.fnmatch(w, pat)]
            if not words:
                self._out(f"  No words matching '{pattern}'  ({len(all_words)} total)")
                return
            self._out(f"\n  Vocabulary matching '{pattern}' ({len(words)} of {len(all_words)}):\n")
        else:
            words = all_words
            self._out(f"\n  Vocabulary ({len(words)} words):\n")

        # Print in columns
        col_width = max(len(w) for w in words) + 2
        term_width = 78
        cols = max(1, term_width // col_width)
        rows = (len(words) + cols - 1) // cols
        for r in range(rows):
            line = ""
            for c in range(cols):
                idx = r + c * rows
                if idx < len(words):
                    line += f"{words[idx]:<{col_width}}"
            self._out(f"  {line}")
        self._out("")

    # ── save / load ──────────────────────────────────────────────────────────

    def do_save(self, args: str):
        """Save configuration to a TOML file.
          save              (uses last loaded file, or repeater.toml)
          save myconfig.toml
        """
        path = args.strip() or self._config_path or "repeater.toml"
        try:
            self.cfg.save(path)
            self._config_path = path
            self._out(f"  Saved to {path}")
        except Exception as e:
            self._out(f"  Save failed: {e}")

    def do_load(self, args: str):
        """Load configuration from a TOML file.
          load myconfig.toml
        """
        path = args.strip()
        if not path:
            self._out("  Usage: load <filename>")
            return
        try:
            self.cfg = RepeaterConfig.load(path)
            self.sim = Simulator(self.cfg)
            self._config_path = path
            self._out(f"  Loaded {path}")
        except FileNotFoundError:
            self._out(f"  File not found: {path}")
        except Exception as e:
            self._out(f"  Load failed: {e}")

    # ── simulation commands ───────────────────────────────────────────────────

    def _run_sim_command(self, args: str):
        parts = args.strip().lower().split()
        verb  = parts[0] if parts else ""

        if verb == "cos":
            direction = parts[1] if len(parts) > 1 else ""
            if direction in ("up", "on", "high"):
                for line in self.sim.cos_up():
                    self._out(line)
            elif direction in ("down", "off", "low"):
                for line in self.sim.cos_down():
                    self._out(line)
            else:
                self._out("  Usage: cos up | cos down")

        elif verb == "ctcss":
            direction = parts[1] if len(parts) > 1 else ""
            if direction in ("up", "on", "detected", "present"):
                for line in self.sim.ctcss_detected():
                    self._out(line)
            elif direction in ("down", "off", "lost", "gone"):
                for line in self.sim.ctcss_lost():
                    self._out(line)
            else:
                self._out("  Usage: ctcss on | ctcss off")

        elif verb == "dtmf":
            digit = parts[1] if len(parts) > 1 else ""
            if digit:
                for line in self.sim.dtmf(digit):
                    self._out(line)
            else:
                self._out("  Usage: dtmf <digit>")

        elif verb == "id":
            id_type = parts[1] if len(parts) > 1 else "mandatory"
            if id_type not in ("initial", "pending", "mandatory"):
                id_type = "mandatory"
            for line in self.sim.trigger_id(id_type):
                self._out(line)

        elif verb == "advance":
            try:
                secs = float(parts[1]) if len(parts) > 1 else 1.0
                new_lines = self.sim.advance(secs)
                if new_lines:
                    for line in new_lines:
                        self._out(line)
                else:
                    self._out(f"  Clock advanced {secs}s — no timer events fired")
            except (ValueError, IndexError):
                self._out("  Usage: advance <seconds>")

        elif verb == "log":
            if not self.sim.log:
                self._out("  No events yet.")
            else:
                self._out("\n  ── Simulation Log ──────────────────────────────")
                for entry in self.sim.log:
                    self._out(str(entry))

        elif verb in ("status", "state"):
            self._out("\n" + self.sim.status())

        elif verb == "reset":
            self.sim.reset()
            self._out("  Simulator reset.")

        else:
            self._out(f"  Unknown simulation command: '{args}'")
            self._out("  Try: cos up, cos down, ctcss on, dtmf 1, id, "
                      "advance 5, log, status, reset")

    def do_cos(self, args: str):
        """Simulate COS: 'cos up' or 'cos down'"""
        self._run_sim_command(f"cos {args}")

    def do_dtmf(self, args: str):
        """Simulate a DTMF digit: 'dtmf 5'"""
        self._run_sim_command(f"dtmf {args}")

    def do_advance(self, args: str):
        """Advance simulated clock: 'advance 10' (seconds)"""
        self._run_sim_command(f"advance {args}")

    def do_log(self, _):
        """Show the full simulation event log."""
        self._run_sim_command("log")

    def do_reset(self, _):
        """Reset the simulator."""
        self._run_sim_command("reset")

    # ── test commands ─────────────────────────────────────────────────────────

    def _run_test_command(self, args: str):
        parts = args.strip().split()
        verb  = parts[0].lower() if parts else ""
        rest  = parts[1:]

        if verb == "play":
            what = rest[0].lower() if rest else ""
            if what == "id":
                target = rest[1] if rest[1:] else None
                self._play_id(target)
            elif what == "morse" and rest[1:]:
                self._play_subprocess_morse(" ".join(rest[1:]))
            elif what == "voice" and rest[1:]:
                self._play_subprocess_voice(rest[1])
            elif what == "msg" and rest[1:]:
                self._play_id_message(rest[1])
            elif what in ("courtesy", "ct"):
                self._play_ct_message()
            else:
                self._out("  Usage: play id [name]  |  play msg <name>  |  "
                          "play morse <text>  |  play voice <clip>  |  play ct")

        elif verb in ("vocabulary", "vocab", "words", "voices", "show"):
            self._show_vocabulary()

        elif verb == "gpio":
            c = self.cfg.hardware
            self._out(f"\n  GPIO assignments:")
            self._out(f"    PTT output : GPIO {c.ptt_gpio}")
            self._out(f"    COS input  : GPIO {c.cos_gpio}  "
                      f"({'active-low' if c.cos_invert else 'active-high'})")
            self._out(f"    Mode       : {'SIMULATED' if c.mock else 'REAL'}")
        else:
            self._out(f"  Unknown test command: '{args}'")

    def do_play(self, args: str):
        """Context-sensitive play.
          messages:  play <name>          play a message
          test:      play id [name]       play ID / named message
                     play morse <text>    play Morse
                     play voice <clip>    play a VOICE clip
                     play ct              play courtesy tone
        """
        ctx = self._ctx_name()
        if ctx == "messages":
            name = args.strip().split()[0] if args.strip() else ""
            if name:
                self._play_id_message(name)
            else:
                self._out("  Usage: play <message_name>")
        else:
            self._run_test_command(f"play {args}")

    def do_gpio(self, _):
        """Show GPIO pin assignments."""
        self._run_test_command("gpio")

    # ── audio subprocess helpers ──────────────────────────────────────────────

    def _play_id(self, target: str | None = None):
        """Play an ID message.  Default: next in mandatory rotation."""
        msgs = self.cfg.messages

        if target and target in msgs:
            self._play_id_message(target)
            return

        if target and target in ("initial", "pending", "mandatory"):
            rotation = getattr(self.cfg.identity, f"{target}_ids", [])
            if not rotation:
                self._out(f"  No {target} IDs assigned")
                return
            idx      = self._id_rotation.get(target, 0) % len(rotation)
            self._id_rotation[target] = (idx + 1) % len(rotation)
            self._play_id_message(rotation[idx])
            return

        if target:
            self._out(f"  Unknown message or type: '{target}'")
            self._out(f"  Available messages: {', '.join(sorted(msgs)) or '(none)'}")
            self._out(f"  Or: play id initial | pending | mandatory")
            return

        # Default: next in mandatory rotation
        rotation = self.cfg.identity.mandatory_ids
        if not rotation:
            self._out("  No mandatory IDs assigned — use 'assign <name> mandatory'")
            return
        idx      = self._id_rotation.get("mandatory", 0) % len(rotation)
        self._id_rotation["mandatory"] = (idx + 1) % len(rotation)
        self._play_id_message(rotation[idx])

    def _play_id_message(self, name: str):
        """Play all elements of a named message.

        Consecutive VOICE elements are concatenated into a single audio
        stream to eliminate subprocess-startup gaps between words.
        """
        elements = self.cfg.messages.get(name)
        if elements is None:
            self._out(f"  Message '{name}' not found")
            return
        if not elements:
            self._out(f"  Message '{name}' has no elements")
            return
        self._out(f"  ▶ Message '{name}' ({len(elements)} element(s)):")

        # Group consecutive elements of the same type so VOICE runs
        # can be concatenated and played as one audio stream.
        groups: list[tuple[str, list[dict]]] = []
        for elem in elements:
            etype = elem.get("type", "")
            norm  = "tone" if etype in ("tone", "ct") else etype
            if groups and groups[-1][0] == norm:
                groups[-1][1].append(elem)
            else:
                groups.append((norm, [elem]))

        for gtype, gelems in groups:
            if gtype == "cw":
                for e in gelems:
                    self._play_subprocess_morse(e.get("text", ""))
            elif gtype == "voice":
                clips = [e.get("clip", "") for e in gelems]
                self._play_voice_concat(clips)
            elif gtype == "tone":
                self._play_tone_concat(gelems)
            elif gtype == "time":
                self._out("    TIME: (system time readback — not yet implemented)")
            else:
                for e in gelems:
                    self._out(f"    Unknown element type: '{e.get('type','')}'")

    def _play_ct_message(self):
        """Play the configured courtesy tone (tail message)."""
        name = self.cfg.identity.ct_message
        if not name:
            self._out("  No CT message configured (assign <name> ct)")
            return
        if name in self.cfg.messages:
            self._play_id_message(name)
        else:
            self._out(f"  CT message '{name}' not found")

    def _play_subprocess_morse(self, text: str):
        import subprocess
        c = self.cfg.audio
        script = Path(__file__).parent / "morse.py"
        cmd_args = [sys.executable, str(script),
                    str(c.morse_wpm), str(c.morse_pitch), str(c.morse_volume), text]
        self._out(f"    ▶ CW: {text}  ({c.morse_wpm} WPM, {c.morse_pitch} Hz)")
        try:
            subprocess.run(cmd_args, check=True)
        except FileNotFoundError:
            self._out("    morse.py not found")
        except subprocess.CalledProcessError as e:
            self._out(f"    Morse playback failed: {e}")

    def _play_subprocess_voice(self, clip_name: str):
        """Play a single voice clip (used by 'play voice <clip>')."""
        self._play_voice_concat([clip_name])

    def _find_voice_wav(self, clip_name: str) -> Path | None:
        """Locate a voice clip WAV file, user_pcm first."""
        for dirname in ("user_pcm", "vocab_pcm"):
            wav_path = Path(__file__).parent / dirname / f"{clip_name.upper()}.wav"
            if wav_path.exists():
                return wav_path
        return None

    def _play_voice_concat(self, clips: list[str]):
        """Concatenate voice clips in memory and play as a single stream.

        Inserts a short silence (60 ms) between words — same gap the
        runtime VocabCache.speak() uses.
        """
        import subprocess, tempfile, wave

        base = Path(__file__).parent
        SILENCE_MS = 60
        TARGET_RATE = self.cfg.audio.sample_rate

        # Resolve all WAV paths first
        paths: list[tuple[str, Path]] = []
        missing = []
        for clip in clips:
            p = self._find_voice_wav(clip)
            if p:
                paths.append((clip.upper(), p))
            else:
                missing.append(clip.upper())

        if missing:
            for m in missing:
                self._out(f"    VOICE: {m}  (not found)")
            self._out(f"    Use 'show vocabulary' to list available words")
        if not paths:
            return

        # Display what we're about to play
        words = " ".join(name for name, _ in paths)
        sources = ", ".join(f"{p.parent.name}/{p.name}" for _, p in paths)
        self._out(f"    ▶ VOICE: {words}")

        # Read and concatenate all clips with inter-word silence
        silence_frames = int(SILENCE_MS / 1000 * TARGET_RATE)
        silence_bytes  = b'\x00\x00' * silence_frames  # 16-bit silence
        raw_chunks: list[bytes] = []

        for name, wav_path in paths:
            try:
                with wave.open(str(wav_path), "r") as wf:
                    file_rate = wf.getframerate()
                    raw = wf.readframes(wf.getnframes())
                    if file_rate != TARGET_RATE:
                        # Simple linear resample via the audio_engine helper
                        import numpy as np
                        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                        new_len = int(len(samples) * TARGET_RATE / file_rate)
                        idx = np.linspace(0, len(samples) - 1, new_len)
                        samples = np.interp(idx, np.arange(len(samples)), samples)
                        raw = (samples * 32767).astype(np.int16).tobytes()
                    if raw_chunks:
                        raw_chunks.append(silence_bytes)
                    raw_chunks.append(raw)
            except Exception as e:
                self._out(f"    Could not load {wav_path.name}: {e}")

        if not raw_chunks:
            return

        combined = b''.join(raw_chunks)

        # Write a temp WAV and play it in one subprocess call
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            with wave.open(tmp.name, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(TARGET_RATE)
                wf.writeframes(combined)

            if sys.platform == "darwin":
                cmd = ["afplay", tmp.name]
            else:
                cmd = ["aplay", "-q", tmp.name]
            subprocess.run(cmd, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            self._out(f"    Playback failed: {e}")
        finally:
            try:
                Path(tmp.name).unlink()
            except OSError:
                pass

    def _play_tone_concat(self, gelems: list[dict]):
        """Render a group of consecutive tone elements into one audio stream.

        Each element is either inline {freq1, freq2, ms, amp} or a named
        reference {tone: name} that expands via courtesy_tones.  All are
        collected into a single render_tone() call — one player launch.
        """
        raw_elements: list[list] = []
        for e in gelems:
            if "freq1" in e:
                raw_elements.append([
                    float(e["freq1"]), float(e.get("freq2", 0)),
                    int(e["ms"]), float(e["amp"]),
                ])
            elif "tone" in e:
                tone_name = e["tone"]
                ct = self.cfg.courtesy_tones.get(tone_name)
                if ct:
                    raw_elements.extend(ct)
                else:
                    self._out(f"    Tone '{tone_name}' not found — skipping")
        if not raw_elements:
            return
        total_ms = sum(int(el[2]) for el in raw_elements)
        self._out(f"    ▶ TONE: {len(raw_elements)} element(s), {total_ms} ms")
        try:
            from tones import render_tone, SAMPLE_RATE
            audio = render_tone(raw_elements)
            if audio.size == 0:
                return
            import sounddevice as sd
            sd.play(audio, samplerate=SAMPLE_RATE)
            sd.wait()
        except ImportError:
            self._out("    (tones.py / sounddevice not available)")

    # ── help ─────────────────────────────────────────────────────────────────

    def do_help(self, arg: str):
        if arg:
            super().do_help(arg)
        else:
            self._show_menu()
            self._out("  Navigation:")
            self._out("    cd ..  /  back              go up one level")
            self._out("    cd /                        return to root")
            self._out("    cd configure/timers          jump directly")
            self._out("")
            self._out("  Use /path/command to run commands from other menus:")
            self._out("    /test/play morse HELLO")
            self._out("    /messages/list")
            self._out("    /configure/timers/set tail 2500")
            self._out("")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    elif Path("repeater.toml").exists():
        config_path = "repeater.toml"
    else:
        config_path = None
    try:
        RepeaterShell(config_path).cmdloop()
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
