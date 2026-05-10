"""
Repeater Controller — Configuration Model
Loads/saves TOML; provides defaults; validates values.
"""

from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field, asdict, fields as dc_fields
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Sub-sections
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IdentityConfig:
    # Message names (keys in RepeaterConfig.messages) to rotate through
    # for each ID occasion.  Lists are cycled round-robin at runtime.
    initial_ids:     list = field(default_factory=lambda: ["default_cw"])
    pending_ids:     list = field(default_factory=lambda: ["default_cw"])
    mandatory_ids:   list = field(default_factory=lambda: ["default_cw"])
    # Courtesy tone message — plays at end of hang time (tail message)
    ct_message:      str  = "hang_ct"
    # Backward compat: old TOML files with hang_message are migrated in load()
    # Message played on TOT timeout; empty string = no message
    timeout_message: str  = "timeout_warn"


@dataclass
class TimerConfig:
    # Short intervals stored as seconds; shell accepts bare ms numbers.
    tail:        float = 2.5    # s — PTT hold after COS drops
    hang:        float = 0.5    # s — delay before courtesy/hang message
    kerchunk:    float = 0.5    # s — minimum COS hold to respond
    # Long intervals
    timeout:     float = 180.0  # s — TOT cutoff
    id_interval: float = 600.0  # s — mandatory ID interval (FCC ≤ 10 min)
    id_pending:  float = 30.0   # s — window before deadline to queue pending ID


@dataclass
class AudioConfig:
    morse_wpm:          int   = 20
    morse_pitch:        int   = 700
    morse_volume:       int   = 90
    voice_volume:       int   = 90     # playback level 0-100 for VOICE clips
    repeat_gain:        float = 1.0    # gain applied to RX passthrough before TX
    voice_blocks_repeat:bool  = False  # mute RX passthrough while VOICE plays
    sample_rate:        int   = 16000


@dataclass
class HardwareConfig:
    mock:       bool  = True    # True = simulate; False = real GPIO
    ptt_gpio:   int   = 17
    cos_gpio:   int   = 27
    cos_invert: bool  = False   # True if COS signal is active-low


@dataclass
class CTCSSConfig:
    mode:             str   = "software"
    access_mode:      str   = "ctcss_init"
    encode_freq:      float = 0.0
    decode_freq:      float = 0.0
    encode_level:     float = 0.15
    ste_mode:         str   = "reverse_burst"
    chicken_burst_ms: int   = 200
    reverse_burst_ms: int   = 250
    decode_time_ms:   int   = 250
    decode_threshold: float = 0.015
    decode_hold_ms:   int   = 500


# ─────────────────────────────────────────────────────────────────────────────
# TOML serialiser
# ─────────────────────────────────────────────────────────────────────────────

def _to_toml_val(v) -> str:
    if isinstance(v, bool):   return 'true' if v else 'false'
    if isinstance(v, str):    return f'"{v}"'
    if isinstance(v, dict):   return (
        '{' + ', '.join(f'{k} = {_to_toml_val(vv)}' for k, vv in v.items()) + '}'
    )
    if isinstance(v, list):   return '[' + ', '.join(_to_toml_val(i) for i in v) + ']'
    if isinstance(v, float):  return repr(v)
    return str(v)


def _dict_to_toml(d: dict) -> str:
    """
    Minimal TOML serialiser.
    - Flat dicts       → [section]
    - Dicts of dicts   → [section.name]
    - Lists/scalars    → inline
    """
    lines: list[str] = []
    first:  dict[str, dict] = {}
    second: dict[str, dict] = {}

    for k, v in d.items():
        if isinstance(v, dict):
            if any(isinstance(vv, dict) for vv in v.values()):
                second[k] = v
            else:
                first[k] = v
        else:
            lines.append(f"{k} = {_to_toml_val(v)}")

    for name, section in first.items():
        lines.append(f"\n[{name}]")
        for k, v in section.items():
            lines.append(f"{k} = {_to_toml_val(v)}")

    for name, subs in second.items():
        for subname, sub in subs.items():
            lines.append(f"\n[{name}.{subname}]")
            for k, v in sub.items():
                lines.append(f"{k} = {_to_toml_val(v)}")

    return "\n".join(lines) + "\n"


def _safe_load(cls, data: dict):
    """Construct a dataclass from a dict, ignoring unknown keys.
    Allows old config files to load without crashing on renamed fields.
    """
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


# ─────────────────────────────────────────────────────────────────────────────
# Default pools
# ─────────────────────────────────────────────────────────────────────────────

# Courtesy tone elements: [freq1_hz, freq2_hz, duration_ms, amplitude]
# Single source of truth is BUILTIN_TONES in tones.py.
from tones import BUILTIN_TONES
_DEFAULT_COURTESY_TONES: dict[str, list] = BUILTIN_TONES

# Messages: name → list of elements.
#
# Each element is a dict with type and type-specific fields:
#
#   {"type": "cw",    "text":  "W1AW"}        — Morse code characters
#   {"type": "voice", "clip":  "REPEATER"}    — pre-rendered PCM clip (filename without .wav)
#   {"type": "tone",  "tone":  "default"}     — whole courtesy tone by name
#   {"type": "time"}                          — system time readback (future)
#
# Messages can mix any combination of element types (TONE, CW, VOICE).
# Drop your own .wav files into user_pcm/ to add voice clips.
#
_DEFAULT_MESSAGES: dict[str, list] = {
    "default_cw":   [{"type": "cw",   "text": "N0CALL"}],
    "hang_ct":      [{"type": "tone", "tone": "default"}],
    "timeout_warn": [{"type": "tone", "tone": "timeout_warn"}],
}


def _convert_old_messages(old: dict) -> dict[str, list]:
    """Convert old {mode, text} id_messages format to new element lists."""
    result: dict[str, list] = {}
    for name, msg in old.items():
        mode = msg.get("mode", "cw").lower()
        text = str(msg.get("text", "")).upper()
        elements: list[dict] = []
        if mode in ("cw", "both"):
            elements.append({"type": "cw", "text": text})
        if mode in ("tts", "voice", "both"):
            # Backward compat: old configs used "tts"; treat same as "voice".
            # Each word becomes a separate voice clip element.
            for word in text.split():
                elements.append({"type": "voice", "clip": word})
        if not elements:
            elements.append({"type": "cw", "text": text})
        result[name] = elements
    return result


def _normalize_element(e: dict) -> dict:
    """Normalize legacy element types on load (ct → tone)."""
    if e.get("type") == "ct":
        return {**e, "type": "tone"}
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Top-level config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RepeaterConfig:
    identity:       IdentityConfig = field(default_factory=IdentityConfig)
    timers:         TimerConfig    = field(default_factory=TimerConfig)
    audio:          AudioConfig    = field(default_factory=AudioConfig)
    hardware:       HardwareConfig = field(default_factory=HardwareConfig)
    ctcss:          CTCSSConfig    = field(default_factory=CTCSSConfig)
    courtesy_tones: dict = field(
        default_factory=lambda: {k: list(v) for k, v in _DEFAULT_COURTESY_TONES.items()}
    )
    messages: dict = field(
        default_factory=lambda: {k: list(v) for k, v in _DEFAULT_MESSAGES.items()}
    )

    # ── serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "identity": asdict(self.identity),
            "timers":   asdict(self.timers),
            "audio":    asdict(self.audio),
            "hardware": asdict(self.hardware),
            "ctcss":    asdict(self.ctcss),
            "courtesy_tones": {
                name: {"elements": elems}
                for name, elems in self.courtesy_tones.items()
            },
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
        if "identity" in data:
            idata = dict(data["identity"])
            # Backward compat: hang_message → ct_message
            if "hang_message" in idata and "ct_message" not in idata:
                idata["ct_message"] = idata.pop("hang_message")
            cfg.identity = _safe_load(IdentityConfig, idata)
        if "timers"   in data:  cfg.timers   = _safe_load(TimerConfig,    data["timers"])
        if "audio"    in data:  cfg.audio    = _safe_load(AudioConfig,    data["audio"])
        if "hardware" in data:  cfg.hardware = _safe_load(HardwareConfig, data["hardware"])
        if "ctcss"    in data:  cfg.ctcss    = _safe_load(CTCSSConfig,    data["ctcss"])
        if "courtesy_tones" in data:
            cfg.courtesy_tones = {
                name: tone["elements"]
                for name, tone in data["courtesy_tones"].items()
            }
        if "messages" in data:
            cfg.messages = {
                name: [_normalize_element(e) for e in msg.get("elements", [])]
                for name, msg in data["messages"].items()
            }
        elif "id_messages" in data:
            # Backward-compat: convert old {mode, text} format to element lists
            cfg.messages = _convert_old_messages(data["id_messages"])
        return cfg

    def describe(self) -> str:
        c = self
        t = c.timers
        a = c.audio

        msg_lines = []
        for name in sorted(c.messages):
            elems    = c.messages[name]
            elem_str = _elems_display(elems, c.courtesy_tones)
            msg_lines.append(f"  {name:<20}  {elem_str}")

        lines = [
            "── Telemetry ────────────────────────────────",
            f"  ID interval    : {t.id_interval:.0f} s  ({t.id_interval/60:.0f} min)",
            f"  ID pending     : {t.id_pending:.0f} s before deadline",
            f"  Initial IDs    : {', '.join(c.identity.initial_ids) or '(none)'}",
            f"  Pending IDs    : {', '.join(c.identity.pending_ids) or '(none)'}",
            f"  Mandatory IDs  : {', '.join(c.identity.mandatory_ids) or '(none)'}",
            f"  CT message     : {c.identity.ct_message or '(none)'}",
            f"  Timeout message: {c.identity.timeout_message or '(none)'}",
            "",
            "── Messages ─────────────────────────────────",
        ] + (msg_lines or ["  (no messages defined)"]) + [
            "",
            "── Timers ───────────────────────────────────",
            f"  Tail           : {t.tail*1000:.0f} ms",
            f"  Hang           : {t.hang*1000:.0f} ms",
            f"  Kerchunk       : {t.kerchunk*1000:.0f} ms",
            f"  Timeout        : {t.timeout:.0f} s",
            "",
            "── Audio ────────────────────────────────────",
            f"  Morse          : {a.morse_wpm} WPM  {a.morse_pitch} Hz  {a.morse_volume}%",
            f"  Voice volume   : {a.voice_volume}%",
            f"  Repeat gain    : {a.repeat_gain:.2f}x",
            f"  Voice blocks   : {'yes (mutes RX passthrough during VOICE)' if a.voice_blocks_repeat else 'no'}",
            "",
            "── Hardware ─────────────────────────────────",
            f"  Mode           : {'SIMULATED' if c.hardware.mock else 'REAL GPIO'}",
            f"  PTT GPIO       : {c.hardware.ptt_gpio}",
            f"  COS GPIO       : {c.hardware.cos_gpio}",
            f"  COS polarity   : {'active-low' if c.hardware.cos_invert else 'active-high'}",
            "",
            "── CTCSS ────────────────────────────────────",
            f"  Mode           : {c.ctcss.mode.upper()}",
            f"  Access mode    : {c.ctcss.access_mode}",
            (f"  Encode         : {c.ctcss.encode_freq} Hz  (level {c.ctcss.encode_level:.0%})"
             if c.ctcss.encode_freq else
             f"  Encode         : disabled"),
            (f"  Decode         : {c.ctcss.decode_freq} Hz  ({c.ctcss.decode_time_ms} ms window)"
             if c.ctcss.decode_freq else
             f"  Decode         : disabled"),
            f"  STE            : {c.ctcss.ste_mode}" + (
                f"  ({c.ctcss.chicken_burst_ms} ms)" if c.ctcss.ste_mode == "chicken_burst" else
                f"  ({c.ctcss.reverse_burst_ms} ms, 120°)" if c.ctcss.ste_mode == "reverse_burst"
                else ""),
        ]
        return "\n".join(lines)


def _tone_param_str(f1, f2, ms, amp) -> str:
    """Format one tone element as pipe-separated params: freq1|freq2|ms|amp."""
    def _n(v):
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v)
    return f"{_n(f1)}|{_n(f2)}|{int(ms)}|{_n(amp)}"


def _elem_display(e: dict, courtesy_tones: dict | None = None) -> str:
    """One-line summary of a single message element for display."""
    t = e.get("type", "?")
    if t == "cw":           return f'CW:{e.get("text","")}'
    if t == "voice":        return f'VOICE:{e.get("clip","")}'
    if t in ("tone", "ct"):
        if "freq1" in e:
            # Inline tone parameters
            return f'TONE:{_tone_param_str(e["freq1"], e.get("freq2",0), e["ms"], e["amp"])}'
        tone_name = e.get("tone", "")
        if courtesy_tones and tone_name in courtesy_tones:
            params = " ".join(_tone_param_str(*el) for el in courtesy_tones[tone_name])
            return f'TONE:{params}'
        return f'TONE:{tone_name}'
    if t == "time":         return "TIME"
    return repr(e)


def _elems_display(elems: list[dict], courtesy_tones: dict | None = None) -> str:
    """Compact summary of an element list, grouping consecutive same-type runs.

    Instead of  VOICE:THIS  VOICE:IS  VOICE:W  VOICE:A  ...
    produces    VOICE: THIS IS W A ...

    Tone elements are shown as pipe-separated params (freq1|freq2|ms|amp).
    Named tone references are resolved via courtesy_tones when available.
    """
    if not elems:
        return "(empty)"
    parts: list[str] = []
    prev_type = None
    for e in elems:
        t = e.get("type", "?")
        norm = "tone" if t in ("tone", "ct") else t
        if norm == "voice":
            val = e.get("clip", "")
        elif norm == "cw":
            val = e.get("text", "")
        elif norm == "tone":
            if "freq1" in e:
                val = _tone_param_str(e["freq1"], e.get("freq2",0), e["ms"], e["amp"])
            elif "tone" in e and courtesy_tones and e["tone"] in courtesy_tones:
                val = " ".join(_tone_param_str(*el) for el in courtesy_tones[e["tone"]])
            elif "tone" in e:
                val = e["tone"]
            else:
                val = "?"
        elif norm == "time":
            if prev_type != norm:
                parts.append("TIME")
            prev_type = norm
            continue
        else:
            parts.append(repr(e))
            prev_type = None
            continue
        if norm == prev_type:
            # Continue the current group — just append the value
            parts[-1] += f" {val}"
        else:
            parts.append(f"{norm.upper()}: {val}")
        prev_type = norm
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# English-language "set" parser
# ─────────────────────────────────────────────────────────────────────────────

_TIME_UNITS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
}

_COMBINED_TIME = re.compile(r'^([0-9]+(?:\.[0-9]*)?)([a-zA-Z]+)$')


def _parse_time(tokens: list[str], default_unit: str = "s") -> float | None:
    """
    Parse a time value from tokens.  Accepts:
      2500          bare number — interpreted in default_unit (s or ms per field)
      2500ms        combined token
      2.5 s         number + unit word
      3 minutes     number + unit word
    """
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
            mult = _TIME_UNITS.get(tokens[i + 1].lower())
            if mult is not None:
                return val * mult
        return val * _TIME_UNITS.get(default_unit, 1.0)

    return None


def _parse_int(tokens: list[str]) -> int | None:
    for t in tokens:
        try: return int(t)
        except ValueError: continue
    return None


def _parse_float(tokens: list[str]) -> float | None:
    for t in tokens:
        try: return float(t)
        except ValueError: continue
    return None


_NOISE = {"set", "to", "at", "the", "a", "an", "as", "is", "="}

# (keyword_set, section, field_name)
_ALIASES: list[tuple[set[str], str, str]] = [
    # timers — ms fields: bare number = ms
    # (timers listed first so bare "set hang 500" matches the timer;
    #  "set ct message <name>" wins identity.ct_message at score 2+)
    ({"tail"},                                         "timers",   "tail"),
    ({"hang", "delay"},                                "timers",   "hang"),
    ({"kerchunk", "antikerchunk", "minimum"},          "timers",   "kerchunk"),
    # timers — s fields: bare number = seconds
    ({"timeout", "tot"},                               "timers",   "timeout"),
    ({"id", "interval", "period"},                     "timers",   "id_interval"),
    ({"id", "pending", "warn"},                        "timers",   "id_pending"),
    # identity — message slots (require "message" keyword to disambiguate)
    ({"ct", "courtesy", "message"},                    "identity", "ct_message"),
    ({"timeout", "message"},                           "identity", "timeout_message"),
    # audio — morse
    ({"morse", "speed", "wpm", "cw"},                  "audio",    "morse_wpm"),
    ({"morse", "pitch", "frequency", "freq", "cw"},    "audio",    "morse_pitch"),
    ({"morse", "volume", "level", "cw"},               "audio",    "morse_volume"),
    # audio — voice
    ({"voice", "volume"},                              "audio",    "voice_volume"),
    # audio — repeat
    ({"repeat", "gain", "rx", "passthrough", "level"}, "audio",    "repeat_gain"),
    ({"voice", "blocks", "repeat", "mute"},            "audio",    "voice_blocks_repeat"),
    # hardware
    ({"mock", "simulate", "simulation"},               "hardware", "mock"),
    ({"ptt", "gpio", "pin", "output"},                 "hardware", "ptt_gpio"),
    ({"cos", "gpio", "pin", "input", "carrier"},       "hardware", "cos_gpio"),
    ({"cos", "invert", "polarity"},                    "hardware", "cos_invert"),
    # ctcss
    ({"ctcss", "mode", "pl"},                          "ctcss",    "mode"),
    ({"ctcss", "access", "activation"},                "ctcss",    "access_mode"),
    ({"ctcss", "encode", "tx", "transmit", "freq"},    "ctcss",    "encode_freq"),
    ({"ctcss", "encode", "level", "deviation"},        "ctcss",    "encode_level"),
    ({"ctcss", "decode", "rx", "receive", "freq"},     "ctcss",    "decode_freq"),
    ({"ctcss", "decode", "window", "speed"},           "ctcss",    "decode_time_ms"),
    ({"ctcss", "decode", "threshold", "sensitivity"},  "ctcss",    "decode_threshold"),
    ({"ctcss", "decode", "hold", "hysteresis"},        "ctcss",    "decode_hold_ms"),
    ({"ste", "squelch", "elimination"},                "ctcss",    "ste_mode"),
    ({"chicken", "burst", "stop", "lead"},             "ctcss",    "chicken_burst_ms"),
    ({"reverse", "burst", "motorola"},                 "ctcss",    "reverse_burst_ms"),
]

# "time_ms" → bare number treated as milliseconds, stored as seconds internally
# "time"    → bare number treated as seconds
_FIELD_TYPES: dict[str, str] = {
    "tail":                "time_ms",
    "hang":                "time_ms",
    "kerchunk":            "time_ms",
    "timeout":             "time",
    "id_interval":         "time",
    "id_pending":          "time",
    "morse_wpm":           "int",
    "morse_pitch":         "int",
    "morse_volume":        "int",
    "voice_volume":        "int",
    "repeat_gain":         "float",
    "voice_blocks_repeat": "bool",
    "ptt_gpio":            "int",
    "cos_gpio":            "int",
    "mock":                "bool",
    "cos_invert":          "bool",
    "ct_message":          "str",
    "timeout_message":     "str",
    "mode":                "str",
    "access_mode":         "str",
    "encode_freq":         "float",
    "encode_level":        "float",
    "decode_freq":         "float",
    "decode_time_ms":      "int",
    "decode_threshold":    "float",
    "decode_hold_ms":      "int",
    "ste_mode":            "str",
    "chicken_burst_ms":    "int",
    "reverse_burst_ms":    "int",
}

_BOOL_TRUE  = {"true", "yes", "on", "1", "real", "enable", "enabled"}
_BOOL_FALSE = {"false", "no", "off", "0", "mock", "simulated", "disable", "disabled"}

_DISPLAY_UNIT = {
    "time_ms": lambda v: f"{v*1000:.0f} ms",
    "time":    lambda v: f"{v:.1f} s",
}


def apply_set_command(cfg: RepeaterConfig, args: str) -> str:
    tokens = [t for t in re.split(r'\s+', args.strip()) if t.lower() not in _NOISE]
    if not tokens:
        return "Nothing to set. Try: set hang message hang_ct  or  set tail 2500"

    words = {t.lower() for t in tokens}

    best_match, best_score = None, 0
    for alias_words, section, field_name in _ALIASES:
        score = len(words & alias_words)
        if score > best_score:
            best_score = score
            best_match = (section, field_name)

    if not best_match or best_score == 0:
        return f"Don't know what to set from: '{args}'"

    section, field_name = best_match
    ftype = _FIELD_TYPES.get(field_name, "str")
    section_obj = getattr(cfg, section)

    alias_words = next(a[0] for a in _ALIASES if a[1] == section and a[2] == field_name)
    value_tokens = [t for t in tokens if t.lower() not in alias_words]

    value = None
    if ftype == "time_ms":
        value = _parse_time(value_tokens, default_unit="ms")
        if value is None:
            return f"Couldn't parse a time from: {value_tokens}  (e.g. 2500 or 2500ms or 2.5s)"
    elif ftype == "time":
        value = _parse_time(value_tokens, default_unit="s")
        if value is None:
            return f"Couldn't parse a time from: {value_tokens}  (e.g. 180 or 3m or 180s)"
    elif ftype == "int":
        value = _parse_int(value_tokens)
        if value is None:
            return f"Couldn't parse a number from: {value_tokens}"
    elif ftype == "float":
        value = _parse_float(value_tokens)
        if value is None:
            return f"Couldn't parse a number from: {value_tokens}"
    elif ftype == "bool":
        vl = value_tokens[0].lower() if value_tokens else ""
        if   vl in _BOOL_TRUE:  value = True
        elif vl in _BOOL_FALSE: value = False
        else: return f"Expected yes/no/true/false, got: {value_tokens}"
    elif ftype == "str":
        value = " ".join(value_tokens) if value_tokens else None
        if not value:
            return f"No value given for {field_name}"

    if value is None:
        return f"No value recognised in: '{args}'"

    setattr(section_obj, field_name, value)

    label = field_name.replace("_", " ")
    fmt = _DISPLAY_UNIT.get(ftype)
    display = fmt(value) if fmt else repr(value) if isinstance(value, str) else str(value)
    return f"  {section}.{label} = {display}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for the shell's tab-completion and context-aware help
# ─────────────────────────────────────────────────────────────────────────────

CONTEXT_FIELDS: dict[str, list[str]] = {
    "timers":    ["tail", "hang", "kerchunk", "timeout", "id_interval", "id_pending"],
    "audio":     ["morse_wpm", "morse_pitch", "morse_volume",
                  "voice_volume", "repeat_gain", "voice_blocks_repeat"],
    "hardware":  ["mock", "ptt_gpio", "cos_gpio", "cos_invert"],
    "ctcss":     ["mode", "access_mode", "encode_freq", "encode_level",
                  "decode_freq", "decode_time_ms", "decode_threshold", "decode_hold_ms",
                  "ste_mode", "chicken_burst_ms", "reverse_burst_ms"],
}

FIELD_HINTS: dict[str, str] = {
    "ct_message":          "ct message      — courtesy tone message (tail message) (e.g. set ct message hang_ct)",
    "timeout_message":     "timeout message — message name to play on TOT         (e.g. set timeout message timeout_warn)",
    "tail":                "tail            ms  — PTT hold after COS drops        (e.g. set tail 2500)",
    "hang":                "hang            ms  — delay before hang message       (e.g. set hang 500)",
    "kerchunk":            "kerchunk        ms  — minimum COS to respond          (e.g. set kerchunk 500)",
    "timeout":             "timeout         s   — TOT cutoff                      (e.g. set timeout 180)",
    "id_interval":         "id interval     s   — mandatory ID period             (e.g. set id interval 600)",
    "id_pending":          "id pending      s   — queue pending ID N s early      (e.g. set id pending 30)",
    "morse_wpm":           "morse wpm       — CW speed in WPM                     (e.g. set morse wpm 20)",
    "morse_pitch":         "morse pitch     — CW tone in Hz                       (e.g. set morse pitch 700)",
    "morse_volume":        "morse volume    — CW level 0-100                      (e.g. set morse volume 90)",
    "voice_volume":        "voice volume    — VOICE clip level 0-100              (e.g. set voice volume 90)",
    "repeat_gain":         "repeat gain     — RX passthrough gain multiplier      (e.g. set repeat gain 1.5)",
    "voice_blocks_repeat": "voice blocks repeat — mute RX during VOICE playback   (e.g. set voice blocks on)",
    "mock":                "mock            — yes=simulate GPIO, no=real Pi       (e.g. set mock yes)",
    "ptt_gpio":            "ptt gpio        — PTT output pin number               (e.g. set ptt gpio 17)",
    "cos_gpio":            "cos gpio        — COS input pin number                (e.g. set cos gpio 27)",
    "cos_invert":          "cos invert      — yes if COS is active-low            (e.g. set cos invert no)",
    "mode":                "ctcss mode      — off / hardware / software",
    "access_mode":         "ctcss access    — cos / ctcss / cos_ctcss / ctcss_init",
    "encode_freq":         "ctcss encode freq  — TX PL tone Hz (0=off)            (e.g. 100.0)",
    "encode_level":        "ctcss encode level — fraction of peak deviation       (e.g. 0.15)",
    "decode_freq":         "ctcss decode freq  — required RX tone Hz (0=off)      (e.g. 100.0)",
    "decode_time_ms":      "ctcss decode window — ms integration window           (e.g. 250)",
    "decode_threshold":    "ctcss decode threshold — Goertzel power 0-1           (e.g. 0.015)",
    "decode_hold_ms":      "ctcss decode hold   — ms hysteresis after loss        (e.g. 500)",
    "ste_mode":            "ste mode        — none / chicken_burst / reverse_burst",
    "chicken_burst_ms":    "chicken burst   — ms CTCSS stops before carrier drops",
    "reverse_burst_ms":    "reverse burst   — ms duration of 120° phase shift",
}
