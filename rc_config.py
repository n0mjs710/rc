"""
Repeater Controller — Configuration Model
Loads/saves TOML; provides typed defaults; validates values on set.
"""

from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field, asdict, fields as dc_fields
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Per-section dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DaemonConfig:
    socket_path: str = "run/rc.sock"   # relative paths resolve from config file's directory
    log_level:   str = "INFO"


@dataclass
class HardwareConfig:
    hidraw_device:    str  = ""    # empty = auto-detect by VID
    audio_device:     str  = ""    # sounddevice name/index; empty = system default
    cor_active_low:   bool = True  # True = bit clear means COR active (AllStar convention)
    ctcss_active_low: bool = True  # True = bit clear means CTCSS active


@dataclass
class AudioConfig:
    sample_rate:          int   = 48_000
    rx_hpf:               bool  = True    # 300 Hz HPF on RX (removes sub-audible)
    rx_deemphasis:        bool  = True    # FM de-emphasis on RX audio
    tx_preemphasis:       bool  = False   # FM pre-emphasis on TX mix
    repeat_gain:          float = 1.0     # RX passthrough gain multiplier
    morse_wpm:            int   = 20
    morse_pitch:          int   = 700
    morse_level:          float = 0.9
    impolite_morse_level: float = 0.3     # CW level when IDing over an active QSO
    voice_level:          float = 0.9
    voice_blocks_repeat:  bool  = False   # mute RX passthrough during VOICE clips
    pre_message_ms:       int   = 0       # dead air after PTT-on, before first CW/voice sample
    post_message_ms:      int   = 0       # dead air after last sample drains, before PTT-off
    ste_delay_ms:         int   = 0       # squelch tail elimination delay (0 = disabled)


@dataclass
class TimerConfig:
    hang:        float = 2.5    # s — hangup time: PTT hold after CT
    ct_delay:    float = 0.5    # s — delay from RX loss to courtesy tone
    kerchunk:    float = 0.5    # s — minimum COR hold to respond
    timeout:     float = 180.0  # s — TOT transmit cutoff
    id_interval: float = 600.0  # s — mandatory ID interval (FCC ≤ 10 min)
    id_anxious:  float = 60.0   # s — sneak anxious ID this far before deadline


@dataclass
class EventConfig:
    """Maps repeater events to the message names that play when they fire."""
    startup_message:        str  = ""
    initial_ids:            list = field(default_factory=list)
    mandatory_ids:          list = field(default_factory=list)
    anxious_id:             str  = ""   # single message; sneaked in before mandatory deadline
    impolite_id:            str  = ""   # single message; played over active QSO if deadline hit
    ct_message:             str  = ""
    timeout_message:        str  = ""
    timeout_cancel_message: str  = ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-port config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PortConfig:
    name:        str          = "port"    # set from TOML key [ports.<name>]; not written as a field
    access_mode: str          = "cor"     # "cor" (COR alone) or "cor_ctcss" (both required)
    hardware:    HardwareConfig = field(default_factory=HardwareConfig)
    audio:       AudioConfig    = field(default_factory=AudioConfig)
    timers:      TimerConfig    = field(default_factory=TimerConfig)
    events:      EventConfig    = field(default_factory=EventConfig)

    def describe(self) -> str:
        t = self.timers
        a = self.audio
        lines = [
            f"── Port: {self.name}  [access={self.access_mode}] ──────────────────────────────────────────",
            "",
            "── Hardware ─────────────────────────────────",
            f"  HID device     : {self.hardware.hidraw_device or '(auto-detect)'}",
            f"  Audio device   : {self.hardware.audio_device or '(system default)'}",
            f"  COR polarity   : {'active-low' if self.hardware.cor_active_low else 'active-high'}",
            f"  CTCSS polarity : {'active-low' if self.hardware.ctcss_active_low else 'active-high'}",
            "",
            "── Audio ────────────────────────────────────",
            f"  Sample rate    : {a.sample_rate} Hz",
            f"  RX HPF 300 Hz  : {'on' if a.rx_hpf else 'off'}",
            f"  RX de-emphasis : {'on' if a.rx_deemphasis else 'off'}",
            f"  TX pre-emphasis: {'on' if a.tx_preemphasis else 'off'}",
            f"  Repeat gain    : {a.repeat_gain:.2f}x",
            f"  Morse          : {a.morse_wpm} WPM  {a.morse_pitch} Hz  {a.morse_level*100:.0f}%",
            f"  Impolite level : {a.impolite_morse_level*100:.0f}% (CW over active QSO)",
            f"  Voice level    : {a.voice_level*100:.0f}%",
            f"  Voice blocks   : {'yes' if a.voice_blocks_repeat else 'no'}",
            f"  Pre-msg pad    : {a.pre_message_ms} ms",
            f"  Post-msg pad   : {a.post_message_ms} ms",
            f"  STE delay      : {a.ste_delay_ms} ms{'' if a.ste_delay_ms else ' (disabled)'}",
            "",
            "── Timers ───────────────────────────────────",
            f"  Hang           : {t.hang*1000:.0f} ms",
            f"  CT delay       : {t.ct_delay*1000:.0f} ms",
            f"  Kerchunk       : {t.kerchunk*1000:.0f} ms",
            f"  Timeout (TOT)  : {t.timeout:.0f} s",
            f"  ID interval    : {t.id_interval:.0f} s",
            f"  ID anxious     : {t.id_anxious:.0f} s before deadline",
            "",
            "── Events ───────────────────────────────────",
            f"  Initial IDs    : {', '.join(self.events.initial_ids) or '(none)'}",
            f"  Mandatory IDs  : {', '.join(self.events.mandatory_ids) or '(none)'}",
            f"  Anxious ID     : {self.events.anxious_id or '(none)'}",
            f"  Impolite ID    : {self.events.impolite_id or '(none)'}",
            f"  Startup msg    : {self.events.startup_message or '(none)'}",
            f"  CT message     : {self.events.ct_message or '(none)'}",
            f"  Timeout msg    : {self.events.timeout_message or '(none)'}",
            f"  Timeout cancel : {self.events.timeout_cancel_message or '(not configured)'}",
        ]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level config object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RepeaterConfig:
    daemon:   DaemonConfig = field(default_factory=DaemonConfig)
    messages: dict         = field(default_factory=dict)   # global pool; shared by all ports
    ports:    list         = field(default_factory=list)   # list[PortConfig]

    def describe(self) -> str:
        msg_lines = []
        for name in sorted(self.messages):
            elems    = self.messages[name]
            elem_str = _elems_display(elems)
            msg_lines.append(f"  {name:<20}  {elem_str}")

        lines = [
            "── Daemon ───────────────────────────────────",
            f"  Socket         : {self.daemon.socket_path}",
            f"  Log level      : {self.daemon.log_level}",
            "",
            "── Messages (shared) ────────────────────────",
        ] + (msg_lines or ["  (none)"])

        for pc in self.ports:
            lines.append("")
            lines.append(pc.describe())

        return "\n".join(lines)

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        lines: list[str] = []

        # [daemon]
        lines.append("[daemon]")
        for k, v in asdict(self.daemon).items():
            lines.append(f"{k} = {_toml_val(v)}")

        # [messages.*] — global pool
        for msg_name, elems in self.messages.items():
            lines.append("")
            lines.append(f"[messages.{msg_name}]")
            lines.append(f"elements = [{', '.join(_elem_toml(e) for e in elems)}]")

        # [ports.<name>] sections — port name is the TOML key, not a field
        for pc in self.ports:
            n = pc.name
            lines.append("")
            lines.append(f"[ports.{n}]")
            lines.append(f'access_mode = {_toml_val(pc.access_mode)}')

            for section_name, section_obj in [
                ("hardware", pc.hardware),
                ("audio",    pc.audio),
                ("timers",   pc.timers),
                ("events",   pc.events),
            ]:
                lines.append("")
                lines.append(f"[ports.{n}.{section_name}]")
                for k, v in asdict(section_obj).items():
                    lines.append(f"{k} = {_toml_val(v)}")

        Path(path).write_text("\n".join(lines) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "RepeaterConfig":
        data = tomllib.loads(Path(path).read_text())
        cfg  = cls()

        if "daemon" in data:
            cfg.daemon = _safe_load(DaemonConfig, data["daemon"])

        if "messages" in data:
            cfg.messages = _load_messages(data["messages"])

        # [ports.<name>] — port name comes from the TOML key
        if "ports" in data and isinstance(data["ports"], dict):
            for port_name, port_data in data["ports"].items():
                cfg.ports.append(_load_port(port_data, name=port_name))

        if not cfg.ports:
            cfg.ports.append(PortConfig())

        return cfg


def _load_port(data: dict, name: str) -> PortConfig:
    pc = PortConfig()
    pc.name = name

    if "access_mode" in data:
        pc.access_mode = data["access_mode"]
    if "hardware" in data:
        pc.hardware = _safe_load(HardwareConfig, data["hardware"])
    if "audio" in data:
        pc.audio = _safe_load(AudioConfig, data["audio"])
    if "timers" in data:
        pc.timers = _safe_load(TimerConfig, data["timers"])
    if "events" in data:
        pc.events = _safe_load(EventConfig, data["events"])

    return pc


def _load_messages(data: dict) -> dict:
    return {
        n: [_normalize_element(e) for e in msg.get("elements", [])]
        for n, msg in data.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOML helpers
# ─────────────────────────────────────────────────────────────────────────────

def _toml_val(v) -> str:
    if isinstance(v, bool):  return "true" if v else "false"
    if isinstance(v, str):   return f'"{v}"'
    if isinstance(v, list):  return "[" + ", ".join(_toml_val(i) for i in v) + "]"
    if isinstance(v, float): return repr(v)
    return str(v)


def _elem_toml(e: dict) -> str:
    """Serialise a message element as an inline TOML table."""
    parts = []
    for k, v in e.items():
        parts.append(f"{k} = {_toml_val(v)}")
    return "{" + ", ".join(parts) + "}"


def _safe_load(cls, data: dict):
    """Construct a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _normalize_element(e: dict) -> dict:
    if e.get("type") == "ct":
        return {**e, "type": "tone"}
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Element display helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tone_param_str(f1, f2, ms, amp) -> str:
    def _n(v):
        if isinstance(v, float) and v == int(v): return str(int(v))
        return str(v)
    return f"{_n(f1)}|{_n(f2)}|{int(ms)}|{_n(amp)}"


def _elems_display(elems: list[dict]) -> str:
    if not elems:
        return "(empty)"
    parts: list[str] = []
    prev_type = None
    for e in elems:
        t    = e.get("type", "?")
        norm = "tone" if t in ("tone", "ct") else t
        if norm == "voice":  val = e.get("clip", "")
        elif norm == "cw":   val = e.get("text", "")
        elif norm == "tone":
            val = _tone_param_str(e["freq1"], e.get("freq2", 0), e["ms"], e["amp"]) \
                  if "freq1" in e else "?"
        else:
            parts.append(repr(e)); prev_type = None; continue
        if norm == prev_type:
            parts[-1] += f" {val}"
        else:
            parts.append(f"{norm.upper()}: {val}")
        prev_type = norm
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# "set" command parser  (operates on a single PortConfig)
# ─────────────────────────────────────────────────────────────────────────────

_TIME_UNITS = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
}
_COMBINED_TIME = re.compile(r"^([0-9]+(?:\.[0-9]*)?)([a-zA-Z]+)$")


def _parse_time(tokens: list[str], default_unit: str = "s") -> float | None:
    for t in tokens:
        m = _COMBINED_TIME.match(t)
        if m:
            mult = _TIME_UNITS.get(m.group(2).lower())
            if mult is not None:
                return float(m.group(1)) * mult
    for i, t in enumerate(tokens):
        try:
            val = float(t)
        except ValueError:
            continue
        if i + 1 < len(tokens):
            mult = _TIME_UNITS.get(tokens[i+1].lower())
            if mult is not None:
                return val * mult
        return val * _TIME_UNITS.get(default_unit, 1.0)
    return None


def _parse_int(tokens):
    for t in tokens:
        try: return int(t)
        except ValueError: pass
    return None


def _parse_float(tokens):
    for t in tokens:
        try: return float(t)
        except ValueError: pass
    return None


_NOISE = {"set", "to", "at", "the", "a", "an", "as", "is", "="}
_BOOL_TRUE  = {"true", "yes", "on", "1", "enable", "enabled"}
_BOOL_FALSE = {"false", "no", "off", "0", "disable", "disabled"}

# (keyword_set, section, field_name)
# section is a PortConfig attribute name, or "port" for fields directly on PortConfig
_ALIASES: list[tuple[set[str], str, str]] = [
    # timers
    ({"hang", "hangup", "holdoff"},                       "timers",   "hang"),
    ({"ct", "delay", "courtesy", "pre"},                  "timers",   "ct_delay"),
    ({"kerchunk", "minimum"},                             "timers",   "kerchunk"),
    ({"timeout", "tot"},                                  "timers",   "timeout"),
    ({"id", "interval", "period"},                        "timers",   "id_interval"),
    ({"id", "anxious", "warn"},                           "timers",   "id_anxious"),
    # events
    ({"ct", "courtesy", "message"},                       "events",   "ct_message"),
    ({"timeout", "message"},                              "events",   "timeout_message"),
    # audio
    ({"morse", "speed", "wpm", "cw"},                     "audio",    "morse_wpm"),
    ({"morse", "pitch", "frequency", "freq", "cw"},       "audio",    "morse_pitch"),
    ({"morse", "level", "volume", "cw"},                  "audio",    "morse_level"),
    ({"impolite", "level", "duck"},                       "audio",    "impolite_morse_level"),
    ({"voice", "level", "volume"},                        "audio",    "voice_level"),
    ({"repeat", "gain", "rx", "passthrough"},             "audio",    "repeat_gain"),
    ({"voice", "blocks", "repeat", "mute"},               "audio",    "voice_blocks_repeat"),
    ({"pre", "message", "padding", "pad"},                "audio",    "pre_message_ms"),
    ({"post", "message", "padding", "pad"},               "audio",    "post_message_ms"),
    ({"ste", "squelch", "tail", "elimination"},           "audio",    "ste_delay_ms"),
    ({"rx", "hpf", "highpass"},                           "audio",    "rx_hpf"),
    ({"rx", "deemphasis", "de"},                          "audio",    "rx_deemphasis"),
    ({"tx", "preemphasis", "pre"},                        "audio",    "tx_preemphasis"),
    # access (directly on PortConfig)
    ({"ctcss", "access", "mode", "activation"},           "port",     "access_mode"),
    # hardware
    ({"hidraw", "hid", "device", "path"},                 "hardware", "hidraw_device"),
    ({"audio", "device", "sounddevice"},                  "hardware", "audio_device"),
]

_FIELD_TYPES: dict[str, str] = {
    "hang": "time_ms", "ct_delay": "time_ms", "kerchunk": "time_ms",
    "timeout": "time", "id_interval": "time", "id_anxious": "time",
    "morse_wpm": "int", "morse_pitch": "int",
    "morse_level": "float", "impolite_morse_level": "float",
    "pre_message_ms": "int", "post_message_ms": "int", "ste_delay_ms": "int",
    "voice_level": "float", "repeat_gain": "float",
    "voice_blocks_repeat": "bool", "rx_hpf": "bool",
    "rx_deemphasis": "bool", "tx_preemphasis": "bool",
    "access_mode": "str",
    "ct_message": "str", "timeout_message": "str",
    "hidraw_device": "str", "audio_device": "str",
}

_DISPLAY_UNIT = {
    "time_ms": lambda v: f"{v*1000:.0f} ms",
    "time":    lambda v: f"{v:.1f} s",
}


def apply_set_command(cfg: PortConfig, args: str) -> str:
    """Parse and apply a natural-language 'set' command to a port; return result string."""
    tokens = [t for t in re.split(r"\s+", args.strip()) if t.lower() not in _NOISE]
    if not tokens:
        return "Nothing to set."

    words = {t.lower() for t in tokens}
    best_match, best_score = None, 0
    for alias_words, section, field_name in _ALIASES:
        score = len(words & alias_words)
        if score > best_score:
            best_score = score
            best_match = (section, field_name)

    if not best_match or best_score == 0:
        return f"Don't know what field to set from: '{args}'"

    section, field_name = best_match
    ftype       = _FIELD_TYPES.get(field_name, "str")
    section_obj = cfg if section == "port" else getattr(cfg, section)
    alias_words = next(a[0] for a in _ALIASES if a[1] == section and a[2] == field_name)
    value_tokens = [t for t in tokens if t.lower() not in alias_words]

    if ftype == "time_ms":
        value = _parse_time(value_tokens, default_unit="ms")
        if value is None:
            return f"Couldn't parse a time from: {value_tokens}"
    elif ftype == "time":
        value = _parse_time(value_tokens, default_unit="s")
        if value is None:
            return f"Couldn't parse a time from: {value_tokens}"
    elif ftype == "int":
        value = _parse_int(value_tokens)
        if value is None:
            return f"Couldn't parse an integer from: {value_tokens}"
    elif ftype == "float":
        value = _parse_float(value_tokens)
        if value is None:
            return f"Couldn't parse a number from: {value_tokens}"
    elif ftype == "bool":
        vl = value_tokens[0].lower() if value_tokens else ""
        if   vl in _BOOL_TRUE:  value = True
        elif vl in _BOOL_FALSE: value = False
        else: return f"Expected yes/no/true/false, got: {value_tokens}"
    else:
        value = " ".join(value_tokens) if value_tokens else None
        if not value:
            return f"No value given for {field_name}"

    if value is None:
        return f"No value recognised in: '{args}'"

    setattr(section_obj, field_name, value)
    fmt     = _DISPLAY_UNIT.get(ftype)
    display = fmt(value) if fmt else (repr(value) if isinstance(value, str) else str(value))
    label   = field_name.replace("_", " ")
    prefix  = "" if section == "port" else f"{section}."
    return f"  {prefix}{label} = {display}"
