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
from pathlib import Path

from rc_config import RepeaterConfig, PortConfig, apply_set_command
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
        self.cfg          = cfg
        self._config_path = config_path
        self._ports: list[Port] = []
        self._api:   APIServer | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        for pc in self.cfg.ports:
            hw = CM119Hardware(
                pc.hardware.hidraw_device,
                cor_active_low   = pc.hardware.cor_active_low,
                ctcss_active_low = pc.hardware.ctcss_active_low,
            )
            hw.open()
            log.info("[%s] CM119 opened — hidraw: %s",
                     pc.name, pc.hardware.hidraw_device or "(auto)")

            ac        = pc.audio
            audio_dev = pc.hardware.audio_device or None
            engine    = AudioEngine(
                sample_rate         = ac.sample_rate,
                blocksize           = ac.sample_rate // 50,   # 20 ms
                input_device        = audio_dev,
                output_device       = audio_dev,
                rx_hpf              = ac.rx_hpf,
                rx_deemphasis       = ac.rx_deemphasis,
                tx_preemphasis      = ac.tx_preemphasis,
                repeat_gain         = ac.repeat_gain,
                voice_blocks_repeat = ac.voice_blocks_repeat,
                ste_delay_ms        = ac.ste_delay_ms,
            )
            engine.start()

            port = Port(pc, hw, engine)
            port.add_state_listener(self._make_state_listener(pc.name))
            port.start(self._loop)
            self._ports.append(port)

        # API server
        self._api = APIServer(self.cfg.daemon.socket_path)
        self._api.register("state",      self._cmd_state)
        self._api.register("config",     self._cmd_config)
        self._api.register("set",        self._cmd_set)
        self._api.register("play",       self._cmd_play)
        self._api.register("ptt",        self._cmd_ptt)
        self._api.register("reload",     self._cmd_reload)
        self._api.register("shutdown",   self._cmd_shutdown)
        self._api.register("msg_list",   self._cmd_msg_list)
        self._api.register("msg_show",   self._cmd_msg_show)
        self._api.register("msg_new",    self._cmd_msg_new)
        self._api.register("msg_delete", self._cmd_msg_delete)
        self._api.register("msg_clear",  self._cmd_msg_clear)
        self._api.register("msg_add",    self._cmd_msg_add)
        self._api.register("ports",      self._cmd_ports)
        await self._api.start()

        port_names = ", ".join(p.name for p in self._ports)
        log.info("Daemon ready — socket: %s  ports: [%s]",
                 self.cfg.daemon.socket_path, port_names)
        _sd_notify("READY=1")

        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        log.info("Shutting down daemon…")
        _sd_notify("STOPPING=1")

        for port in self._ports:
            port.stop()
            port._engine.stop()
            port._hw.close()

        if self._api:
            await self._api.stop()

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        log.info("Daemon stopped.")

    # ── port lookup ────────────────────────────────────────────────────────────

    def _get_port(self, msg: dict | None) -> Port | None:
        """Return the port addressed by msg["port"] (name), or the first port."""
        if not self._ports:
            return None
        port_name = msg.get("port") if msg else None
        if port_name:
            for p in self._ports:
                if p.name == port_name:
                    return p
            return None
        return self._ports[0]

    # ── state-change → push event ─────────────────────────────────────────────

    def _make_state_listener(self, port_name: str):
        def _cb(status: dict) -> None:
            if self._api:
                self._api.push_event(
                    {"event": "status", "port": port_name, **status}
                )
        return _cb

    # ── API command handlers ───────────────────────────────────────────────────

    async def _cmd_state(self, msg: dict | None = None) -> dict:
        port = self._get_port(msg)
        port_names = [p.name for p in self._ports]
        if not port:
            return {"state": "NOT_RUNNING", "ports": port_names}
        return {"port": port.name, "ports": port_names, **port.get_status()}

    async def _cmd_ports(self, msg: dict) -> dict:
        return {"ports": [
            {"name": p.name, **p.get_status()}
            for p in self._ports
        ]}

    async def _cmd_config(self, msg: dict) -> dict:
        return {"result": self.cfg.describe()}

    async def _cmd_set(self, msg: dict) -> dict:
        args = msg.get("args", "")
        if not args:
            return {"error": "no args provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        result = apply_set_command(port.cfg, args)
        return {"result": result}

    async def _cmd_play(self, msg: dict) -> dict:
        msg_name = msg.get("msg", "")
        if not msg_name:
            return {"error": "no msg provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "port not running"}
        if msg_name not in port.cfg.messages:
            return {"error": f"unknown message: {msg_name!r}"}
        was_ptt = port._engine._ptt
        if not was_ptt:
            port._set_ptt(True)
        port._play_message(msg_name)
        if not was_ptt:
            while port._engine.is_playing():
                await asyncio.sleep(0.05)
            port._set_ptt(False)
        return {}

    async def _cmd_ptt(self, msg: dict) -> dict:
        active = bool(msg.get("active", False))
        port = self._get_port(msg)
        if port:
            port._set_ptt(active)
        return {"ptt": active}

    async def _cmd_reload(self, msg: dict) -> dict:
        if not self._config_path:
            return {"error": "no config file to reload"}
        try:
            new_cfg = RepeaterConfig.load(self._config_path)
            self.cfg = new_cfg
            # Update each running port's config by matching name
            for port in self._ports:
                for pc in new_cfg.ports:
                    if pc.name == port.name:
                        port.cfg = pc
                        break
            log.info("Config reloaded from %s", self._config_path)
            return {"reloaded": True}
        except Exception as exc:
            return {"error": str(exc)}

    async def _cmd_shutdown(self, msg: dict) -> dict:
        self._loop.call_soon(self._shutdown_event.set)
        return {}

    # ── message management (per port) ─────────────────────────────────────────

    async def _cmd_msg_list(self, msg: dict) -> dict:
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        return {"messages": {
            name: [e.get("type", "?") for e in elems if isinstance(e, dict)]
            for name, elems in port.cfg.messages.items()
        }}

    async def _cmd_msg_show(self, msg: dict) -> dict:
        name = msg.get("name", "")
        if not name:
            return {"error": "no name provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        elems = port.cfg.messages.get(name)
        if elems is None:
            return {"error": f"message '{name}' not found"}
        return {"name": name, "elements": elems}

    async def _cmd_msg_new(self, msg: dict) -> dict:
        name = msg.get("name", "")
        if not name:
            return {"error": "no name provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        if name in port.cfg.messages:
            return {"error": f"message '{name}' already exists"}
        port.cfg.messages[name] = []
        log.info("[%s] Message created: '%s'", port.name, name)
        return {}

    async def _cmd_msg_delete(self, msg: dict) -> dict:
        name = msg.get("name", "")
        if not name:
            return {"error": "no name provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        if name not in port.cfg.messages:
            return {"error": f"message '{name}' not found"}
        del port.cfg.messages[name]
        log.info("[%s] Message deleted: '%s'", port.name, name)
        return {}

    async def _cmd_msg_clear(self, msg: dict) -> dict:
        name = msg.get("name", "")
        if not name:
            return {"error": "no name provided"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        if name not in port.cfg.messages:
            return {"error": f"message '{name}' not found"}
        port.cfg.messages[name] = []
        log.info("[%s] Message cleared: '%s'", port.name, name)
        return {}

    async def _cmd_msg_add(self, msg: dict) -> dict:
        name = msg.get("name", "")
        elem = msg.get("element")
        if not name:
            return {"error": "no name provided"}
        if not isinstance(elem, dict):
            return {"error": "element must be a JSON object"}
        if "type" not in elem:
            return {"error": "element must have a 'type' field (cw, voice, tone)"}
        port = self._get_port(msg)
        if not port:
            return {"error": "no port running"}
        if name not in port.cfg.messages:
            port.cfg.messages[name] = []
        port.cfg.messages[name].append(elem)
        log.info("[%s] Element added to '%s': %r", port.name, name, elem)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_socket_path(socket_path: str, config_path: str | None) -> str:
    """Resolve a relative socket path against the config file's directory."""
    p = Path(socket_path)
    if p.is_absolute():
        return socket_path
    base = Path(config_path).parent if config_path else Path.cwd()
    return str(base / p)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    if config_path:
        cfg = RepeaterConfig.load(config_path)
    else:
        cfg = RepeaterConfig()

    cfg.daemon.socket_path = _resolve_socket_path(cfg.daemon.socket_path, config_path)

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

    async def _run():
        task = asyncio.current_task()

        def _request_shutdown():
            log.info("Shutdown signal received")
            task.cancel()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT,  _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

        try:
            await daemon.run()
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
            await daemon.shutdown()

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
