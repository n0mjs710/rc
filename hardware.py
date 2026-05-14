"""
CM119 USB audio/GPIO hardware driver.

Uses the Linux hidraw kernel interface directly (/dev/hidraw*) — no hidapi
library required.  hidraw gives non-exclusive access alongside the kernel
audio driver, which is why libusb-based approaches fail: the kernel's
snd_usb_audio driver already owns the USB device and libusb cannot claim it.

Supports CM108/CM119/CM119B USB audio devices (Masters Communications,
RepeaterBuilder, DMK URIx, and similar).  All share the same HID signal
mapping (AllStar chan_usbradio convention):

  HID input  byte 0 bit 1  — Vol-Up   → COR   (clear = carrier present; active-low)
  HID input  byte 0 bit 0  — Vol-Down → CTCSS  (clear = tone present; active-low)
  HID output GPIO3 bit 2   — PTT output        (set = TX active)

Note: GPIO assignments are board-specific. This mapping (COR on GPIO2/Vol-Up) matches
the Masters Communications CM119 board. Some boards (e.g. DMK URIx) use the opposite
assignment (COR on GPIO1/Vol-Down, CTCSS on GPIO2/Vol-Up).

The reader thread blocks in select() with a short timeout so close() is
responsive.  Callbacks fire edge-triggered (on state change) from that thread;
they must be fast and non-blocking.

Requirements:
  udev/99-cm119.rules installed (grants audio group access to /dev/hidraw*)
  User must be in the 'audio' group (log out and back in after usermod)
"""

from __future__ import annotations

import glob
import logging
import os
import re
import select
import threading
from typing import Callable

log = logging.getLogger("hardware")


def _usb_topology(hidraw_name: str) -> str | None:
    """Return the USB port topology string (e.g. '1-1.2') for a hidraw device.

    Extracted from the sysfs symlink target, which encodes the physical port
    path.  This string is stable across reboots as long as the cable stays in
    the same USB socket.
    """
    try:
        link = os.readlink(f'/sys/class/hidraw/{hidraw_name}')
        topology = None
        for part in link.split('/'):
            if re.match(r'^\d+-[\d.]+$', part):
                topology = part   # keep iterating — last match is most specific
        return topology
    except Exception:
        return None


def list_devices() -> list[dict]:
    """Return info for every connected CM119-compatible hidraw device.

    Each entry has 'hidraw' (e.g. '/dev/hidraw0') and 'usb_port' (e.g. '1-1.2').
    usb_port can be used as 'usb:<port>' in hidraw_device config to pin a port
    to a specific physical USB socket.
    """
    vid_hex = f"{CM119Hardware.VID:08X}"
    results = []
    for uevent_path in sorted(glob.glob('/sys/class/hidraw/*/device/uevent')):
        try:
            if vid_hex not in open(uevent_path).read().upper():
                continue
            hidraw_name = uevent_path.split('/')[4]
            results.append({
                'hidraw':    f'/dev/{hidraw_name}',
                'usb_port':  _usb_topology(hidraw_name) or '(unknown)',
            })
        except Exception:
            continue
    return results


class CM119Hardware:
    """
    CM108/CM119-family USB HID driver via Linux hidraw.

    Typical lifecycle:
        hw = CM119Hardware()          # or CM119Hardware("/dev/hidraw0")
        hw.open()
        hw.add_cor_callback(my_cb)
        hw.set_ptt(True)
        ...
        hw.close()
    """

    VID = 0x0d8c             # C-Media Electronics — all CM10x/CM11x PIDs
    _READ_TIMEOUT_S = 0.1   # select timeout (controls close() latency)

    # Input report bitmasks (byte 0 of the 4-byte HID input report)
    _COR_BIT   = 0x02        # GPIO2 = Vol-Up   = COR   (board-specific; see module docstring)
    _CTCSS_BIT = 0x01        # GPIO1 = Vol-Down = CTCSS decode

    # Output report GPIO3 bitmask (PTT)
    _PTT_BIT   = 0x04        # GPIO3 = PTT

    def __init__(self, hidraw_device: str = "",
                 cor_active_low: bool = True,
                 ctcss_active_low: bool = True) -> None:
        self._path            = hidraw_device or ""
        self._cor_active_low  = cor_active_low
        self._ctcss_active_low = ctcss_active_low
        self._fd: int | None  = None
        self._lock            = threading.Lock()
        self._cor_cbs:   list[Callable[[bool], None]] = []
        self._ctcss_cbs: list[Callable[[bool], None]] = []
        self._cor_state:   bool = False
        self._ctcss_state: bool = False
        self._ptt_active:  bool = False   # tracked so _poll() can re-assert it
        self._stop            = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public API ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the hidraw device and start the background reader thread."""
        path = self._resolve_path()
        if path is None:
            raise RuntimeError(
                f"No CM119-compatible HID device found (VID 0x{self.VID:04x}).  "
                "Check USB connection and udev rule."
            )
        self._fd = os.open(path, os.O_RDWR)
        log.info("Opened hidraw device: %s", path)

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader, daemon=True, name="cm119-hid"
        )
        self._thread.start()

    def close(self) -> None:
        """Deassert PTT, stop the reader thread, and close the hidraw device."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            if self._fd is not None:
                try:
                    # Ensure PTT is idle before closing so the radio doesn't get stuck transmitting
                    self._ptt_active = False
                    self._write_output()
                except Exception:
                    pass
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None

    def set_ptt(self, active: bool) -> None:
        """Drive the PTT output (GPIO3) active or idle."""
        with self._lock:
            self._ptt_active = active
            self._write_output()

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

    def _resolve_path(self) -> str | None:
        """Resolve self._path to an actual /dev/hidrawN path."""
        if not self._path:
            return self._autodetect()
        if self._path.startswith('usb:'):
            topology = self._path[4:]
            path = self._resolve_usb_topology(topology)
            if path is None:
                raise RuntimeError(
                    f"No CM119 device found at USB port '{topology}'.  "
                    "Run 'python daemon.py --list-devices' to see connected devices."
                )
            return path
        return self._path

    def _autodetect(self) -> str | None:
        """Find the first CM119 hidraw device via sysfs uevent files."""
        vid_hex = f"{self.VID:08X}"
        for uevent_path in glob.glob('/sys/class/hidraw/*/device/uevent'):
            try:
                if vid_hex in open(uevent_path).read().upper():
                    hidraw_name = uevent_path.split('/')[4]   # 'hidraw0', 'hidraw1', …
                    return f'/dev/{hidraw_name}'
            except Exception:
                continue
        return None

    def _resolve_usb_topology(self, topology: str) -> str | None:
        """Find the hidraw path for a device at the given USB port topology."""
        vid_hex = f"{self.VID:08X}"
        for uevent_path in glob.glob('/sys/class/hidraw/*/device/uevent'):
            try:
                if vid_hex not in open(uevent_path).read().upper():
                    continue
                hidraw_name = uevent_path.split('/')[4]
                if _usb_topology(hidraw_name) == topology:
                    return f'/dev/{hidraw_name}'
            except Exception:
                continue
        return None

    def _write_output(self) -> None:
        """Write the current PTT state as a HID output report (call under _lock).

        Writing an output report causes the CM119 to respond with a fresh
        interrupt IN report containing the current GPIO input state.  This is
        used both to set PTT and to poll COR/CTCSS when no unsolicited report
        has arrived within the select timeout.
        """
        if self._fd is None:
            return
        val = self._PTT_BIT if self._ptt_active else 0x00
        try:
            os.write(self._fd, bytes([0x00, 0x00, self._PTT_BIT, val, 0x00]))
        except Exception as exc:
            log.error("HID write error: %s", exc)

    def _reader(self) -> None:
        """Blocking HID read loop — runs until close() sets _stop."""
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._fd], [], [], self._READ_TIMEOUT_S)
                if r:
                    data = os.read(self._fd, 4)
                    if data:
                        self._process(data)
                else:
                    # No unsolicited report within the timeout window.
                    # Writing the current PTT state solicits a fresh input
                    # report from the CM119, which lets us detect COR/CTCSS
                    # transitions that the device doesn't report on its own
                    # (e.g. COR returning to idle when the line is released).
                    with self._lock:
                        self._write_output()
            except Exception as exc:
                log.error("HID read error: %s", exc)
                break

    def _process(self, data: bytes) -> None:
        """Parse one input report; fire edge callbacks on state change."""
        log.debug("HID report: %s", data.hex())
        b0        = data[0]
        raw_cor   = bool(b0 & self._COR_BIT)
        raw_ctcss = bool(b0 & self._CTCSS_BIT)
        new_cor   = (not raw_cor)   if self._cor_active_low   else raw_cor
        new_ctcss = (not raw_ctcss) if self._ctcss_active_low else raw_ctcss
        log.debug("  b0=0x%02x  COR_bit=%d(raw=%s)→cor=%s  CTCSS_bit=%d(raw=%s)→ctcss=%s",
                  b0, (b0 & self._COR_BIT) >> 1, raw_cor, new_cor,
                  b0 & self._CTCSS_BIT, raw_ctcss, new_ctcss)

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
