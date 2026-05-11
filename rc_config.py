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
# Config sections
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DaemonConfig:
    socket_path: str = "run/rc.sock"   # relative paths resolve from config file's directory
    log_level:   str = "INFO"


@dataclass
class HardwareConfig:
    hidraw_device:   str  = ""    # empty = auto-detect by VID
    audio_device:    str  = ""    # sounddevice name/index; empty = system default
    cor_active_low:  bool = True  # True = bit clear means COR active (AllStar/chan_usbradio convention)
    ctcss_active_low: bool = True # True = bit clear means CTCSS active


@dataclass
class AudioConfig:
    sample_rate:         int   = 48_000
    rx_hpf:              bool  = True    # 300 Hz HPF on RX (removes sub-audible)
    rx_deemphasis:       bool  = True    # FM de-emphasis on RX audio
    tx_preemphasis:      bool  = False   # FM pre-emphasis on TX mix
    repeat_gain:         float = 1.0    # RX passthrough gain multiplier
    morse_wpm:           int   = 20
    morse_pitch:         int   = 700
    morse_level:         float = 0.9
    voice_level:         float = 0.9
    voice_blocks_repeat: bool  = False   # mute RX passthrough during VOICE clips


@dataclass
class CTCSSConfig:
    access_mode: str = "cor"   # "cor" (COR alone) or "cor_ctcss" (both required)


@dataclass
class TimerConfig:
    hang:        float = 2.5    # s — hangup time: PTT hold after CT (how long before TX "hangs up")
    ct_delay:    float = 0.5    # s — delay from RX loss to courtesy tone
    kerchunk:    float = 0.5    # s — minimum COR hold to respond
    timeout:     float = 180.0  # s — TOT transmit cutoff
    id_interval: float = 600.0  # s — mandatory ID interval (FCC ≤ 10 min)
    id_pending:  float = 30.0   # s — queue pending ID this far before deadline


@dataclass
class IdentityConfig:
    startup_message:        str  = ""
    initial_ids:            list = field(default_factory=list)
    pending_ids:            list = field(default_factory=list)
    mandatory_ids:          list = field(default_factory=list)
    ct_message:             str  = ""
    timeout_message:        str  = ""
    timeout_cancel_message: str  = ""




# ─────────────────────────────────────────────────────────────────────────────
# TOML serialiser (minimal — enough for save())
# ─────────────────────────────────────────────────────────────────────────────

def _toml_val(v) -> str:
    if isinstance(v, bool):  return "true" if v else "false"
    if isinstance(v, str):   return f'"{v}"'
    if isinstance(v, list):  return "[" + ", ".join(_toml_val(i) for i in v) + "]"
    if isinstance(v, float): return repr(v)
    return str(v)


def _dict_to_toml(d: dict) -> str:
    lines: list[str] = []
    sections: list[tuple[str, dict]] = []

    for k, v in d.items():
        if isinstance(v, dict):
            sections.append((k, v))
        else:
            lines.append(f"{k} = {_toml_val(v)}")

    for name, section in sections:
        lines.append(f"\n[{name}]")
        for k, v in section.items():
            if isinstance(v, dict):
                # nested sub-tables (e.g. messages) written inline
                lines.append(f"\n[{name}.{k}]")
                for kk, vv in v.items():
                    lines.append(f"{kk} = {_toml_val(vv)}")
            else:
                lines.append(f"{k} = {_toml_val(v)}")

    return "\n".join(lines) + "\n"


def _safe_load(cls, data: dict):
    """Construct a dataclass from a dict, silently ignoring unknown keys."""
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _normalize_element(e: dict) -> dict:
    """Map legacy element types to current names."""
    if e.get("type") == "ct":
        return {**e, "type": "tone"}
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Top-level config object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RepeaterConfig:
    daemon:         DaemonConfig   = field(default_factory=DaemonConfig)
    hardware:       HardwareConfig = field(default_factory=HardwareConfig)
    audio:          AudioConfig    = field(default_factory=AudioConfig)
    ctcss:          CTCSSConfig    = field(default_factory=CTCSSConfig)
    timers:         TimerConfig    = field(default_factory=TimerConfig)
    identity:       IdentityConfig = field(default_factory=IdentityConfig)
    messages:       dict = field(default_factory=dict)

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "daemon":   asdict(self.daemon),
            "hardware": asdict(self.hardware),
            "audio":    asdict(self.audio),
            "ctcss":    asdict(self.ctcss),
            "timers":   asdict(self.timers),
            "identity": asdict(self.identity),
            "messages": {
                name: {"elements": elems}
                for name, elems in self.messages.items()
            },
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(_dict_to_toml(self.to_dict()))

    @classmethod
    def load(cls, path: str | Path) -> "RepeaterConfig":
        data = tomllib.loads(Path(path).read_text())
        cfg  = cls()

        if "daemon"   in data: cfg.daemon   = _safe_load(DaemonConfig,   data["daemon"])
        if "hardware" in data: cfg.hardware = _safe_load(HardwareConfig, data["hardware"])
        if "audio"    in data: cfg.audio    = _safe_load(AudioConfig,     data["audio"])
        if "ctcss"    in data: cfg.ctcss    = _safe_load(CTCSSConfig,     data["ctcss"])
        if "timers"   in data: cfg.timers   = _safe_load(TimerConfig,     data["timers"])

        if "identity" in data:
            idata = dict(data["identity"])
            # backward compat
            if "hang_message" in idata and "ct_message" not in idata:
                idata["ct_message"] = idata.pop("hang_message")
            cfg.identity = _safe_load(IdentityConfig, idata)

        if "messages" in data:
            cfg.messages = {
                name: [_normalize_element(e) for e in msg.get("elements", [])]
                for name, msg in data["messages"].items()
            }

        return cfg

    def describe(self) -> str:
        c = self
        t = c.timers
        a = c.audio

        msg_lines = []
        for name in sorted(c.messages):
            elems    = c.messages[name]
            elem_str = _elems_display(elems)
            msg_lines.append(f"  {name:<20}  {elem_str}")

        lines = [
            "── Daemon ───────────────────────────────────",
            f"  Socket         : {c.daemon.socket_path}",
            f"  Log level      : {c.daemon.log_level}",
            "",
            "── Hardware ─────────────────────────────────",
            f"  HID device     : {c.hardware.hidraw_device or '(auto-detect)'}",
            f"  Audio device   : {c.hardware.audio_device or '(system default)'}",
            f"  COR polarity   : {'active-low' if c.hardware.cor_active_low else 'active-high'}",
            f"  CTCSS polarity : {'active-low' if c.hardware.ctcss_active_low else 'active-high'}",
            "",
            "── Audio ────────────────────────────────────",
            f"  Sample rate    : {a.sample_rate} Hz",
            f"  RX HPF 300 Hz  : {'on' if a.rx_hpf else 'off'}",
            f"  RX de-emphasis : {'on' if a.rx_deemphasis else 'off'}",
            f"  TX pre-emphasis: {'on' if a.tx_preemphasis else 'off'}",
            f"  Repeat gain    : {a.repeat_gain:.2f}x",
            f"  Morse          : {a.morse_wpm} WPM  {a.morse_pitch} Hz  {a.morse_level*100:.0f}%",
            f"  Voice level    : {a.voice_level*100:.0f}%",
            f"  Voice blocks   : {'yes' if a.voice_blocks_repeat else 'no'}",
            "",
            "── CTCSS ────────────────────────────────────",
            f"  Access mode    : {c.ctcss.access_mode}",
            "",
            "── Timers ───────────────────────────────────",
            f"  Hang           : {t.hang*1000:.0f} ms",
            f"  CT delay       : {t.ct_delay*1000:.0f} ms",
            f"  Kerchunk       : {t.kerchunk*1000:.0f} ms",
            f"  Timeout (TOT)  : {t.timeout:.0f} s",
            f"  ID interval    : {t.id_interval:.0f} s",
            f"  ID pending     : {t.id_pending:.0f} s before deadline",
            "",
            "── Identity ─────────────────────────────────",
            f"  Initial IDs    : {', '.join(c.identity.initial_ids) or '(none)'}",
            f"  Pending IDs    : {', '.join(c.identity.pending_ids) or '(none)'}",
            f"  Mandatory IDs  : {', '.join(c.identity.mandatory_ids) or '(none)'}",
            f"  Startup msg    : {c.identity.startup_message or '(none)'}",
            f"  CT message     : {c.identity.ct_message or '(none)'}",
            f"  Timeout msg    : {c.identity.timeout_message or '(none)'}",
            f"  Timeout cancel : {c.identity.timeout_cancel_message or '(not configured)'}",
            "",
            "── Messages ─────────────────────────────────",
        ] + (msg_lines or ["  (none)"])

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Element display helpers (used in describe() and shell)
# ─────────────────────────────────────────────────────────────────────────────

def _tone_param_str(f1, f2, ms, amp) -> str:
    def _n(v):
        if isinstance(v, float) and v == int(v): return str(int(v))
        return str(v)
    return f"{_n(f1)}|{_n(f2)}|{int(ms)}|{_n(amp)}"


def _elem_display(e: dict) -> str:
    t = e.get("type", "?")
    if t == "cw":    return f'CW:{e.get("text","")}'
    if t == "voice": return f'VOICE:{e.get("clip","")}'
    if t in ("tone", "ct"):
        if "freq1" in e:
            return f'TONE:{_tone_param_str(e["freq1"], e.get("freq2",0), e["ms"], e["amp"])}'
        return f'TONE:?'
    return repr(e)


def _elems_display(elems: list[dict]) -> str:
    if not elems:
        return "(empty)"
    parts: list[str] = []
    prev_type = None
    for e in elems:
        t    = e.get("type", "?")
        norm = "tone" if t in ("tone", "ct") else t
        if norm == "voice":   val = e.get("clip", "")
        elif norm == "cw":    val = e.get("text", "")
        elif norm == "tone":
            if "freq1" in e:
                val = _tone_param_str(e["freq1"], e.get("freq2",0), e["ms"], e["amp"])
            else: val = "?"
        else:
            parts.append(repr(e)); prev_type = None; continue
        if norm == prev_type:
            parts[-1] += f" {val}"
        else:
            parts.append(f"{norm.upper()}: {val}")
        prev_type = norm
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# "set" command parser (used by the shell via the daemon API)
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
_ALIASES: list[tuple[set[str], str, str]] = [
    # timers (ms fields: bare number = ms)
    ({"hang", "hangup", "holdoff"},                       "timers",   "hang"),
    ({"ct", "delay", "courtesy", "pre"},                  "timers",   "ct_delay"),
    ({"kerchunk", "kerchunk", "minimum"},                 "timers",   "kerchunk"),
    ({"timeout", "tot"},                                  "timers",   "timeout"),
    ({"id", "interval", "period"},                        "timers",   "id_interval"),
    ({"id", "pending", "warn"},                           "timers",   "id_pending"),
    # identity
    ({"ct", "courtesy", "message"},                       "identity", "ct_message"),
    ({"timeout", "message"},                              "identity", "timeout_message"),
    # audio
    ({"morse", "speed", "wpm", "cw"},                     "audio",    "morse_wpm"),
    ({"morse", "pitch", "frequency", "freq", "cw"},       "audio",    "morse_pitch"),
    ({"morse", "level", "volume", "cw"},                  "audio",    "morse_level"),
    ({"voice", "level", "volume"},                        "audio",    "voice_level"),
    ({"repeat", "gain", "rx", "passthrough"},             "audio",    "repeat_gain"),
    ({"voice", "blocks", "repeat", "mute"},               "audio",    "voice_blocks_repeat"),
    ({"rx", "hpf", "highpass"},                           "audio",    "rx_hpf"),
    ({"rx", "deemphasis", "de"},                          "audio",    "rx_deemphasis"),
    ({"tx", "preemphasis", "pre"},                        "audio",    "tx_preemphasis"),
    # ctcss
    ({"ctcss", "access", "mode", "activation"},           "ctcss",    "access_mode"),
    # hardware
    ({"hidraw", "hid", "device", "path"},                 "hardware", "hidraw_device"),
    ({"audio", "device", "sounddevice"},                  "hardware", "audio_device"),
    # daemon
    ({"socket", "path", "sock"},                          "daemon",   "socket_path"),
    ({"log", "level"},                                    "daemon",   "log_level"),
]

_FIELD_TYPES: dict[str, str] = {
    "hang": "time_ms", "ct_delay": "time_ms", "kerchunk": "time_ms",
    "timeout": "time", "id_interval": "time", "id_pending": "time",
    "morse_wpm": "int", "morse_pitch": "int",
    "morse_level": "float", "voice_level": "float", "repeat_gain": "float",
    "voice_blocks_repeat": "bool", "rx_hpf": "bool",
    "rx_deemphasis": "bool", "tx_preemphasis": "bool",
    "access_mode": "str",
    "ct_message": "str", "timeout_message": "str",
    "hidraw_device": "str", "audio_device": "str",
    "socket_path": "str", "log_level": "str",
}

_DISPLAY_UNIT = {
    "time_ms": lambda v: f"{v*1000:.0f} ms",
    "time":    lambda v: f"{v:.1f} s",
}


def apply_set_command(cfg: RepeaterConfig, args: str) -> str:
    """Parse and apply a natural-language 'set' command; return result string."""
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
    ftype        = _FIELD_TYPES.get(field_name, "str")
    section_obj  = getattr(cfg, section)
    alias_words  = next(a[0] for a in _ALIASES if a[1] == section and a[2] == field_name)
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
    return f"  {section}.{field_name.replace('_', ' ')} = {display}"
