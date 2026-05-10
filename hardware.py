"""
GPIO abstraction layer for the repeater controller.

Two implementations are provided:

  MockHardware  — pure Python, no GPIO dependency.  Logs all pin changes to
                  a list so the simulator / test suite can inspect them.
                  Runs on any machine.

  RealHardware  — thin wrapper around RPi.GPIO.  Only importable on a
                  Raspberry Pi; raises ImportError otherwise.

Usage:
    from hardware import get_hardware
    hw = get_hardware(mock=cfg.hardware.mock)
    hw.setup(ptt_gpio=17, cos_gpio=27, cos_invert=False)
    hw.set_ptt(True)
    cos_state = hw.get_cos()
    hw.cleanup()

Both classes implement the HardwareBase interface; the controller only
depends on that, making it straightforward to add new back-ends.
"""

from __future__ import annotations
import threading
from abc import ABC, abstractmethod
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────────────────────

class HardwareBase(ABC):
    """
    Minimal GPIO abstraction required by the repeater controller.

    All methods are thread-safe.  Callbacks registered with
    add_cos_callback() are called from a background thread whenever the
    COS input changes state; the argument is the new boolean value
    (True = carrier present).
    """

    @abstractmethod
    def setup(self, ptt_gpio: int, cos_gpio: int,
              cos_invert: bool = False) -> None:
        """Configure GPIO pins.  Call once before any other method."""

    @abstractmethod
    def set_ptt(self, active: bool) -> None:
        """Drive the PTT output pin high (active=True) or low."""

    @abstractmethod
    def get_cos(self) -> bool:
        """Return the current COS input state (True = carrier present)."""

    @abstractmethod
    def add_cos_callback(self, cb: Callable[[bool], None]) -> None:
        """Register a callback invoked on every COS edge."""

    @abstractmethod
    def remove_cos_callback(self, cb: Callable[[bool], None]) -> None:
        """Unregister a previously added COS callback."""

    @abstractmethod
    def cleanup(self) -> None:
        """Release GPIO resources.  Call on shutdown."""


# ─────────────────────────────────────────────────────────────────────────────
# Mock implementation
# ─────────────────────────────────────────────────────────────────────────────

class MockHardware(HardwareBase):
    """
    Fully in-process GPIO mock.  No external dependencies.

    PTT and COS state can be read or driven directly for testing:
        hw.simulate_cos(True)    # pretend the radio keyed up
        print(hw.ptt)            # check PTT state

    All pin changes are recorded in hw.log as (direction, pin, value) tuples.
    """

    def __init__(self):
        self._lock: threading.Lock        = threading.Lock()
        self._callbacks: list[Callable]   = []
        self._ptt_gpio:  int | None       = None
        self._cos_gpio:  int | None       = None
        self._cos_invert: bool            = False
        self._ptt:       bool             = False
        self._cos:       bool             = False
        self.log:        list[tuple]      = []   # (event, pin, value)

    # ── HardwareBase implementation ──────────────────────────────────────────

    def setup(self, ptt_gpio: int, cos_gpio: int,
              cos_invert: bool = False) -> None:
        with self._lock:
            self._ptt_gpio   = ptt_gpio
            self._cos_gpio   = cos_gpio
            self._cos_invert = cos_invert
            self.log.append(("setup", ptt_gpio, cos_gpio, cos_invert))

    def set_ptt(self, active: bool) -> None:
        with self._lock:
            self._ptt = active
            self.log.append(("ptt", self._ptt_gpio, active))

    def get_cos(self) -> bool:
        with self._lock:
            return self._cos

    def add_cos_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def remove_cos_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            self._callbacks = [c for c in self._callbacks if c is not cb]

    def cleanup(self) -> None:
        with self._lock:
            self._ptt = False
            self._callbacks.clear()
            self.log.append(("cleanup", None, None))

    # ── Mock-only helpers ────────────────────────────────────────────────────

    @property
    def ptt(self) -> bool:
        """Read the current PTT output state."""
        return self._ptt

    def simulate_cos(self, active: bool) -> None:
        """
        Simulate a COS edge from external hardware.
        Sets the internal COS state and fires all registered callbacks.
        """
        with self._lock:
            if active == self._cos:
                return   # no change
            self._cos = active
            self.log.append(("cos_edge", self._cos_gpio, active))
            callbacks = list(self._callbacks)

        # Fire callbacks outside the lock to avoid deadlocks
        for cb in callbacks:
            try:
                cb(active)
            except Exception as exc:
                print(f"[hardware] COS callback raised: {exc}")

    def print_log(self) -> None:
        """Print the event log to stdout."""
        for entry in self.log:
            print("  ", entry)


# ─────────────────────────────────────────────────────────────────────────────
# Real RPi.GPIO implementation
# ─────────────────────────────────────────────────────────────────────────────

class RealHardware(HardwareBase):
    """
    RPi.GPIO back-end.  Raises ImportError if RPi.GPIO is not installed.

    Uses BCM pin numbering.  COS edges are detected with GPIO.add_event_detect
    on BOTH edges; cos_invert=True inverts the active-high assumption
    (use when COS goes LOW on carrier detect).
    """

    def __init__(self):
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            raise ImportError(
                "RPi.GPIO is not available.  "
                "Use MockHardware (mock=True) for non-Pi development."
            )
        self._GPIO       = GPIO
        self._lock       = threading.Lock()
        self._callbacks: list[Callable] = []
        self._ptt_gpio:  int | None     = None
        self._cos_gpio:  int | None     = None
        self._cos_invert: bool          = False

    def setup(self, ptt_gpio: int, cos_gpio: int,
              cos_invert: bool = False) -> None:
        GPIO = self._GPIO
        self._ptt_gpio   = ptt_gpio
        self._cos_gpio   = cos_gpio
        self._cos_invert = cos_invert

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(ptt_gpio, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(cos_gpio, GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
        GPIO.add_event_detect(cos_gpio, GPIO.BOTH,
                              callback=self._gpio_cos_edge, bouncetime=20)

    def set_ptt(self, active: bool) -> None:
        self._GPIO.output(self._ptt_gpio,
                          self._GPIO.HIGH if active else self._GPIO.LOW)

    def get_cos(self) -> bool:
        raw = bool(self._GPIO.input(self._cos_gpio))
        return (not raw) if self._cos_invert else raw

    def add_cos_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def remove_cos_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            self._callbacks = [c for c in self._callbacks if c is not cb]

    def cleanup(self) -> None:
        self._GPIO.cleanup()
        with self._lock:
            self._callbacks.clear()

    def _gpio_cos_edge(self, channel: int) -> None:
        """Called by RPi.GPIO on any COS edge."""
        active = self.get_cos()
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(active)
            except Exception as exc:
                print(f"[hardware] COS callback raised: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def get_hardware(mock: bool = True) -> HardwareBase:
    """
    Return the appropriate hardware back-end.

      mock=True  → MockHardware  (safe on any platform)
      mock=False → RealHardware  (requires Raspberry Pi + RPi.GPIO)
    """
    if mock:
        return MockHardware()
    return RealHardware()


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("hardware.py self-test (MockHardware)")

    hw = MockHardware()
    hw.setup(ptt_gpio=17, cos_gpio=27, cos_invert=False)

    events: list[tuple] = []

    def on_cos(active: bool):
        events.append(("callback", active))
        print(f"  COS callback: {'UP' if active else 'DOWN'}")

    hw.add_cos_callback(on_cos)

    hw.set_ptt(True)
    print(f"  PTT on  : {hw.ptt}")

    hw.simulate_cos(True)
    hw.simulate_cos(False)
    hw.simulate_cos(True)

    hw.set_ptt(False)
    print(f"  PTT off : {hw.ptt}")

    hw.cleanup()

    print(f"\n  Log entries : {len(hw.log)}")
    print(f"  COS callbacks fired: {len(events)}")
    hw.print_log()
    print("\nSelf-test complete.")
