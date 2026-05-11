"""
Unix domain socket API server for the repeater daemon.

Protocol: newline-delimited JSON (one JSON object per line).
The daemon listens for shell connections on a Unix socket.

Commands (shell → daemon):
  {"cmd": "state"}                              → current port status
  {"cmd": "config"}                             → full config dict
  {"cmd": "set", "args": "hang 2500"}           → apply a set command
  {"cmd": "play", "msg": "default_cw"}          → queue a message
  {"cmd": "ptt", "active": true}                → force PTT on/off
  {"cmd": "reload"}                             → reload config from disk
  {"cmd": "shutdown"}                           → stop the daemon
  {"cmd": "subscribe"}                          → start receiving push events

Push events (daemon → subscribed shells):
  {"event": "status", "state": ..., "cor": ..., "ctcss": ..., "ptt": ...}
  {"event": "log", "level": "INFO", "msg": "..."}

All responses are {"ok": true, ...} or {"ok": false, "error": "..."}.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("api")


class APIServer:
    """
    Asyncio Unix socket server.  Manages connections and dispatches commands
    to handler callbacks provided by the daemon.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path  = socket_path
        self._server: asyncio.Server | None = None
        self._subscribers: list[asyncio.StreamWriter] = []

        # Command handlers registered by the daemon
        self._handlers: dict[str, Callable] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        path = Path(self._socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(path)
        )
        log.info("API server listening on %s", self._socket_path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("Server wait_closed timed out")
        for w in list(self._subscribers):
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), timeout=1.0)
            except Exception:
                pass
        self._subscribers.clear()

    # ── handler registration ──────────────────────────────────────────────────

    def register(self, cmd: str, handler: Callable) -> None:
        """Register a coroutine handler for a command name."""
        self._handlers[cmd] = handler

    # ── push events ───────────────────────────────────────────────────────────

    def push_event(self, event: dict) -> None:
        """Broadcast a push event to all subscribed clients (non-blocking)."""
        payload = _encode(event)
        dead    = []
        for w in list(self._subscribers):
            try:
                w.write(payload)
            except Exception:
                dead.append(w)
        for w in dead:
            try:
                self._subscribers.remove(w)
            except ValueError:
                pass

    # ── connection handler ────────────────────────────────────────────────────

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or "shell"
        log.info("Shell connected: %s", peer)

        # Send current status immediately on connect
        if "state" in self._handlers:
            try:
                status = await self._handlers["state"]()
                _send(writer, {"ok": True, **status})
            except Exception:
                pass

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    _send(writer, {"ok": False, "error": f"invalid JSON: {exc}"})
                    continue

                await self._dispatch(msg, writer)

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            if writer in self._subscribers:
                self._subscribers.remove(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("Shell disconnected: %s", peer)

    async def _dispatch(self, msg: dict,
                         writer: asyncio.StreamWriter) -> None:
        cmd = msg.get("cmd", "")

        if cmd == "subscribe":
            if writer not in self._subscribers:
                self._subscribers.append(writer)
            _send(writer, {"ok": True, "subscribed": True})
            return

        handler = self._handlers.get(cmd)
        if handler is None:
            _send(writer, {"ok": False, "error": f"unknown command: {cmd!r}"})
            return

        try:
            result = await handler(msg)
            if result is None:
                _send(writer, {"ok": True})
            elif isinstance(result, dict):
                _send(writer, {"ok": True, **result})
            else:
                _send(writer, {"ok": True, "result": str(result)})
        except Exception as exc:
            log.error("Handler %r raised: %s", cmd, exc)
            _send(writer, {"ok": False, "error": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _encode(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
    try:
        writer.write(_encode(obj))
    except Exception as exc:
        log.debug("Send failed: %s", exc)
