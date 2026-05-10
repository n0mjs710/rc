"""
CM119 USB audio/GPIO hardware driver.

Supports CM108/CM119/CM119B USB audio devices (Masters Communications,
RepeaterBuilder, DMK URIx, and similar).  All share the same HID signal
mapping (AllStar chan_usbradio convention):

  HID input  byte 0 bit 0  — Vol-Down → COR   (set = carrier present)
  HID input  byte 0 bit 1  — Vol-Up   → CTCSS  (set = tone present)
  HID output GPIO3 bit 2   — PTT output        (set = TX active)

The reader thread blocks on hid.read() with a short timeout so close() is
responsive.  Callbacks fire edge-triggered (on state change) from that thread;
they must be fast and non-blocking.

Requirements:
  pip install hidapi
  sudo apt install libhidapi-hidraw0
  udev/99-cm119.rules installed (see repo)
"""

from __future__ import annotations
import logging
import threading
from typing import Callable

log = logging.getLogger("hardware")


class CM119Hardware:
    """
    CM108/CM119-family USB HID driver.

    Typical lifecycle:
        hw = CM119Hardware()          # or CM119Hardware("/dev/hidraw0")
        hw.open()
        hw.add_cor_callback(my_cb)
        hw.set_ptt(True)
        ...
        hw.close()
    """

    VID = 0x0d8c             # C-Media Electronics — all CM10x/CM11x PIDs
    _READ_TIMEOUT_MS = 100   # blocking read interval (controls close() latency)

    # Input report bitmasks (byte 0 of the 4-byte HID input report)
    _COR_BIT   = 0x01        # GPIO1 = Vol-Down = COR
    _CTCSS_BIT = 0x02        # GPIO2 = Vol-Up   = CTCSS decode

    # Output report GPIO3 bitmask (PTT)
    _PTT_BIT   = 0x04        # GPIO3 = PTT

    def __init__(self, hidraw_device: str = "") -> None:
        try:
            import hid
        except ImportError:
            raise ImportError(
                "hidapi not found.  "
                "Install: pip install hidapi  &&  "
                "sudo apt install libhidapi-hidraw0"
            )
        self._hid             = hid
        self._path            = hidraw_device.encode() if hidraw_device else None
        self._device          = None
        self._lock            = threading.Lock()
        self._cor_cbs:   list[Callable[[bool], None]] = []
        self._ctcss_cbs: list[Callable[[bool], None]] = []
        self._cor_state:   bool = False
        self._ctcss_state: bool = False
        self._stop            = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public API ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the HID device and start the background reader thread."""
        self._device = self._hid.device()
        if self._path:
            self._device.open_path(self._path)
        else:
            path = self._autodetect()
            if path is None:
                raise RuntimeError(
                    f"No CM119-compatible HID device found (VID 0x{self.VID:04x}).  "
                    "Check the udev rule and USB connection."
                )
            self._device.open_path(path)

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader, daemon=True, name="cm119-hid"
        )
        self._thread.start()

    def close(self) -> None:
        """Deassert PTT, stop the reader thread, and close the HID device."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            if self._device is not None:
                try:
                    # Deassert PTT before closing so the radio doesn't get stuck transmitting
                    self._device.write(bytes([0x00, 0x00, self._PTT_BIT, 0x00, 0x00]))
                except Exception:
                    pass
                self._device.close()
                self._device = None

    def set_ptt(self, active: bool) -> None:
        """Drive the PTT output (GPIO3) high or low."""
        val = self._PTT_BIT if active else 0x00
        with self._lock:
            if self._device is not None:
                self._device.write(bytes([0x00, 0x00, self._PTT_BIT, val, 0x00]))

    def get_cor(self) -> bool:
        """Return the current COR input state."""
        with self._lock:
            return self._cor_state

    def get_ctcss(self) -> bool:
        """Return the current hardware CTCSS decode state."""
        with self._lock:
            return self._ctcss_state

    def add_cor_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            if cb not in self._cor_cbs:
                self._cor_cbs.append(cb)

    def remove_cor_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            self._cor_cbs = [c for c in self._cor_cbs if c is not cb]

    def add_ctcss_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            if cb not in self._ctcss_cbs:
                self._ctcss_cbs.append(cb)

    def remove_ctcss_callback(self, cb: Callable[[bool], None]) -> None:
        with self._lock:
            self._ctcss_cbs = [c for c in self._ctcss_cbs if c is not cb]

    # ── internal ───────────────────────────────────────────────────────────────

    def _autodetect(self) -> bytes | None:
        devs = self._hid.enumerate(self.VID, 0)
        return devs[0]["path"] if devs else None

    def _reader(self) -> None:
        """Blocking HID read loop — runs until close() sets _stop."""
        while not self._stop.is_set():
            try:
                data = self._device.read(4, timeout_ms=self._READ_TIMEOUT_MS)
            except Exception as exc:
                log.error("HID read error: %s", exc)
                break
            if data:
                self._process(bytes(data))

    def _process(self, data: bytes) -> None:
        """Parse one input report; fire edge callbacks on state change."""
        b0        = data[0]
        new_cor   = bool(b0 & self._COR_BIT)
        new_ctcss = bool(b0 & self._CTCSS_BIT)

        cor_cbs = ctcss_cbs = []
        cor_val = ctcss_val = False

        with self._lock:
            if new_cor != self._cor_state:
                self._cor_state = new_cor
                cor_val = new_cor
                cor_cbs = list(self._cor_cbs)
            if new_ctcss != self._ctcss_state:
                self._ctcss_state = new_ctcss
                ctcss_val = new_ctcss
                ctcss_cbs = list(self._ctcss_cbs)

        for cb in cor_cbs:
            try:
                cb(cor_val)
            except Exception as exc:
                log.error("COR callback error: %s", exc)

        for cb in ctcss_cbs:
            try:
                cb(ctcss_val)
            except Exception as exc:
                log.error("CTCSS callback error: %s", exc)
