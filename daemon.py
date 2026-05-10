"""
Repeater Controller Daemon
==========================
Main process: loads config, opens CM119 hardware, starts the audio engine,
runs the port state machine, and serves the Unix socket API.

Usage:
    python daemon.py [repeater.toml]

Signals:
    SIGTERM / SIGINT — graceful shutdown

Systemd:
    Sends sd_notify READY=1 after startup completes (if NOTIFY_SOCKET is set).
    Sends STOPPING=1 on shutdown.  Use Type=notify in the unit file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from dataclasses import asdict
from pathlib import Path

from rc_config import RepeaterConfig, apply_set_command
from hardware  import CM119Hardware
from audio_engine import AudioEngine
from port      import Port
from api_server import APIServer

log = logging.getLogger("daemon")


# ─────────────────────────────────────────────────────────────────────────────
# Systemd sd_notify helper
# ─────────────────────────────────────────────────────────────────────────────

def _sd_notify(msg: str) -> None:
    """Send a sd_notify message if NOTIFY_SOCKET is set."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(sock_path)
            s.sendall(msg.encode())
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Daemon
# ─────────────────────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self, cfg: RepeaterConfig, config_path: str | None) -> None:
        self.cfg         = cfg
        self._config_path = config_path
        self._port:   Port | None       = None
        self._engine: AudioEngine | None = None
        self._hw:     CM119Hardware | None = None
        self._api:    APIServer | None   = None
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        # Hardware
        self._hw = CM119Hardware(self.cfg.hardware.hidraw_device)
        self._hw.open()
        log.info("CM119 opened — hidraw: %s",
                 self.cfg.hardware.hidraw_device or "(auto)")

        # Audio engine
        ac = self.cfg.audio
        self._engine = AudioEngine(
            sample_rate         = ac.sample_rate,
            blocksize           = ac.sample_rate // 50,   # 20 ms
            rx_hpf              = ac.rx_hpf,
            rx_deemphasis       = ac.rx_deemphasis,
            tx_preemphasis      = ac.tx_preemphasis,
            repeat_gain         = ac.repeat_gain,
            voice_blocks_repeat = ac.voice_blocks_repeat,
        )
        self._engine.start()

        # Port state machine
        self._port = Port(self.cfg, self._hw, self._engine)
        self._port.add_state_listener(self._on_port_state_change)
        self._port.start(self._loop)

        # API server
        self._api = APIServer(self.cfg.daemon.socket_path)
        self._api.register("state",    self._cmd_state)
        self._api.register("config",   self._cmd_config)
        self._api.register("set",      self._cmd_set)
        self._api.register("play",     self._cmd_play)
        self._api.register("ptt",      self._cmd_ptt)
        self._api.register("reload",   self._cmd_reload)
        self._api.register("shutdown", self._cmd_shutdown)
        await self._api.start()

        log.info("Daemon ready — socket: %s  access: %s",
                 self.cfg.daemon.socket_path, self.cfg.ctcss.access_mode)
        _sd_notify("READY=1")

        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        log.info("Shutting down daemon…")
        _sd_notify("STOPPING=1")

        if self._port:
            self._port.stop()
        if self._engine:
            self._engine.stop()
        if self._hw:
            self._hw.close()
        if self._api:
            await self._api.stop()

        log.info("Daemon stopped.")

    # ── API command handlers ───────────────────────────────────────────────────

    async def _cmd_state(self, msg: dict | None = None) -> dict:
        if not self._port:
            return {"state": "NOT_RUNNING"}
        return self._port.get_status()

    async def _cmd_config(self, msg: dict) -> dict:
        return {"config": self.cfg.to_dict()}

    async def _cmd_set(self, msg: dict) -> dict:
        args = msg.get("args", "")
        if not args:
            return {"error": "no args provided"}
        result = apply_set_command(self.cfg, args)
        return {"result": result}

    async def _cmd_play(self, msg: dict) -> dict:
        msg_name = msg.get("msg", "")
        if not msg_name:
            return {"error": "no msg provided"}
        if not self._port:
            return {"error": "port not running"}
        if msg_name not in self.cfg.messages:
            return {"error": f"unknown message: {msg_name!r}"}
        was_ptt = self._port._cor
        if not was_ptt:
            self._hw.set_ptt(True)
            self._engine.set_ptt(True)
        self._port._play_message(msg_name)
        if not was_ptt:
            self._hw.set_ptt(False)
            self._engine.set_ptt(False)
        return {}

    async def _cmd_ptt(self, msg: dict) -> dict:
        active = bool(msg.get("active", False))
        if self._hw:
            self._hw.set_ptt(active)
        if self._engine:
            self._engine.set_ptt(active)
        return {"ptt": active}

    async def _cmd_reload(self, msg: dict) -> dict:
        if not self._config_path:
            return {"error": "no config file to reload"}
        try:
            self.cfg = RepeaterConfig.load(self._config_path)
            if self._port:
                self._port.cfg = self.cfg
            log.info("Config reloaded from %s", self._config_path)
            return {"reloaded": True}
        except Exception as exc:
            return {"error": str(exc)}

    async def _cmd_shutdown(self, msg: dict) -> dict:
        self._loop.call_soon(self._shutdown_event.set)
        return {}

    # ── state-change → push event ─────────────────────────────────────────────

    def _on_port_state_change(self, status: dict) -> None:
        if self._api:
            self._api.push_event({"event": "status", **status})


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    if config_path:
        cfg = RepeaterConfig.load(config_path)
    else:
        cfg = RepeaterConfig()

    logging.basicConfig(
        level  = getattr(logging, cfg.daemon.log_level.upper(), logging.INFO),
        format = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt= "%H:%M:%S",
    )

    if config_path:
        log.info("Config: %s", config_path)

    daemon = Daemon(cfg, config_path)
    loop   = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task: asyncio.Task | None = None

    def _sig_handler(sig, frame):
        log.info("Signal %s — shutting down", sig)
        if main_task is not None:
            main_task.cancel()

    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    async def _run():
        nonlocal main_task
        main_task = asyncio.current_task()
        try:
            await daemon.run()
        except asyncio.CancelledError:
            pass
        finally:
            await daemon.shutdown()

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
