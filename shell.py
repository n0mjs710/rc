#!/usr/bin/env python3
"""
Repeater Controller Shell
=========================
Interactive operator CLI.  Connects to the running daemon via its Unix socket
and provides commands for monitoring and configuration.

Usage:
    python shell.py [repeater.toml]          # uses socket_path from config
    python shell.py --socket /tmp/rc.sock    # explicit socket path
    python shell.py --watch [repeater.toml]  # stream state events and exit

Commands
────────
  state               Show current repeater state
  config              Show full configuration
  set <args>          Change a config value (e.g. "set hang 2500")
  play <message>      Trigger a named message
  ptt on|off          Force PTT on or off
  reload              Reload config from disk
  shutdown            Stop the daemon
  subscribe           Subscribe to push events (Ctrl-C to stop)
  msg list            List all defined messages
  msg show <name>     Show elements of a message
  msg new <name>      Create a new empty message
  msg delete <name>   Delete a message
  msg clear <name>    Remove all elements from a message
  msg add <name> cw <text>                    Add a CW element
  msg add <name> voice <clip>                 Add a voice clip element
  msg add <name> tone <f1> [f2] <ms> <amp>   Add a tone element
  help                Show this help
  quit / exit         Disconnect

Set examples:
  set hang 2500          2500 ms hang time (PTT holdoff)
  set hang 2.5s          same, using seconds
  set ct delay 500ms     500 ms CT delay (pre-courtesy-tone pause)
  set timeout 180
  set morse wpm 20
  set voice level 90
  set ctcss access cor_ctcss

Msg examples:
  msg new my_id
  msg add my_id cw W1AW/R
  msg add my_id voice REPEATER
  msg add my_id tone 1000 0 80 0.8
  msg show my_id
  msg delete my_id
"""

from __future__ import annotations

import argparse
import json
import readline
import socket
import sys
import threading
import time
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Low-level socket I/O (synchronous, for the interactive shell)
# ─────────────────────────────────────────────────────────────────────────────

class DaemonConnection:
    """Synchronous Unix socket connection to the daemon."""

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._sock: socket.socket | None = None
        self._buf  = b""

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._path)
        self._sock.settimeout(5.0)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        self._sock.sendall(data)

    def recv_line(self) -> dict | None:
        """Read the next newline-delimited JSON message."""
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.strip())

    def command(self, obj: dict) -> dict:
        """Send a command and return the response."""
        self.send(obj)
        resp = self.recv_line()
        if resp is None:
            raise ConnectionError("Connection closed by daemon")
        # Skip any push event lines to find the command response
        while resp.get("event"):
            resp = self.recv_line()
            if resp is None:
                raise ConnectionError("Connection closed by daemon")
        return resp

    def recv_line_noblock(self, timeout: float = 0.1) -> dict | None:
        """Try to read a line with timeout; return None on timeout."""
        self._sock.settimeout(timeout)
        try:
            while b"\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            return json.loads(line.strip())
        except socket.timeout:
            return None
        finally:
            self._sock.settimeout(5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_status(s: dict) -> str:
    state  = s.get("state", "?")
    cor    = "COR" if s.get("cor") else "---"
    ctcss  = "CTCSS" if s.get("ctcss") else "-----"
    ptt    = "PTT" if s.get("ptt") else "---"
    access = s.get("access", "?")
    return f"{state:<10}  {cor}  {ctcss}  {ptt}  (access={access})"


def _print_response(resp: dict) -> None:
    if resp.get("ok") is False:
        print(f"Error: {resp.get('error', '?')}")
        return
    # Remove housekeeping keys
    body = {k: v for k, v in resp.items() if k not in ("ok",)}
    if "state" in body:
        print(_fmt_status(body))
    elif "result" in body:
        print(body["result"])
    elif "config" in body:
        _print_config(body["config"])
    elif body:
        for k, v in body.items():
            print(f"  {k}: {v}")


def _print_config(cfg: dict) -> None:
    for section, values in cfg.items():
        if section in ("messages",):
            continue
        print(f"\n[{section}]")
        if isinstance(values, dict):
            for k, v in values.items():
                print(f"  {k:<22} = {v}")

    # Messages
    msgs = cfg.get("messages", {})
    if msgs:
        print("\n[messages]")
        for name, elems in sorted(msgs.items()):
            types = ", ".join(e.get("type", "?") for e in elems if isinstance(e, dict))
            print(f"  {name:<20}  [{types}]")


# ─────────────────────────────────────────────────────────────────────────────
# Watch mode: stream events until Ctrl-C
# ─────────────────────────────────────────────────────────────────────────────

def watch_mode(conn: DaemonConnection) -> None:
    resp = conn.command({"cmd": "subscribe"})
    if not resp.get("ok"):
        print(f"Subscribe failed: {resp.get('error')}")
        return

    print("Watching events (Ctrl-C to stop)…\n")
    conn._sock.settimeout(None)   # blocking
    try:
        while True:
            msg = conn.recv_line()
            if msg is None:
                print("Connection closed.")
                break
            if msg.get("event") == "status":
                ts = time.strftime("%H:%M:%S")
                print(f"{ts}  {_fmt_status(msg)}")
            elif msg.get("event") == "log":
                ts = time.strftime("%H:%M:%S")
                print(f"{ts}  [{msg.get('level','?')}]  {msg.get('msg','')}")
    except KeyboardInterrupt:
        print("\nStopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Interactive shell
# ─────────────────────────────────────────────────────────────────────────────

COMMANDS = [
    "state", "config", "set", "play", "ptt", "reload", "shutdown",
    "subscribe", "watch", "msg", "help", "quit", "exit",
]

def _completer(text: str, state: int):
    options = [c for c in COMMANDS if c.startswith(text)]
    return options[state] if state < len(options) else None


def _cmd_msg(conn: DaemonConnection, rest: str) -> None:
    parts = rest.split()
    if not parts:
        print("Usage: msg list|show|new|delete|clear|add ...")
        return

    sub = parts[0].lower()

    if sub == "list":
        resp = conn.command({"cmd": "msg_list"})
        if resp.get("ok") is False:
            print(f"Error: {resp.get('error', '?')}")
            return
        msgs = resp.get("messages", {})
        if not msgs:
            print("  (no messages defined)")
        else:
            for name, types in sorted(msgs.items()):
                print(f"  {name:<24}  [{', '.join(types) if types else 'empty'}]")

    elif sub == "show":
        if len(parts) < 2:
            print("Usage: msg show <name>")
            return
        resp = conn.command({"cmd": "msg_show", "name": parts[1]})
        if resp.get("ok") is False:
            print(f"Error: {resp.get('error', '?')}")
            return
        elems = resp.get("elements", [])
        if not elems:
            print("  (empty)")
        else:
            for i, e in enumerate(elems):
                print(f"  [{i}]  {e}")

    elif sub == "new":
        if len(parts) < 2:
            print("Usage: msg new <name>")
            return
        resp = conn.command({"cmd": "msg_new", "name": parts[1]})
        _print_response(resp)

    elif sub == "delete":
        if len(parts) < 2:
            print("Usage: msg delete <name>")
            return
        resp = conn.command({"cmd": "msg_delete", "name": parts[1]})
        _print_response(resp)

    elif sub == "clear":
        if len(parts) < 2:
            print("Usage: msg clear <name>")
            return
        resp = conn.command({"cmd": "msg_clear", "name": parts[1]})
        _print_response(resp)

    elif sub == "add":
        if len(parts) < 4:
            print("Usage: msg add <name> cw <text>")
            print("       msg add <name> voice <clip>")
            print("       msg add <name> tone <freq1> [freq2] <ms> <amp>")
            return
        name  = parts[1]
        etype = parts[2].lower()
        tail  = parts[3:]

        if etype == "cw":
            elem = {"type": "cw", "text": " ".join(tail)}
        elif etype == "voice":
            elem = {"type": "voice", "clip": tail[0].upper()}
        elif etype == "tone":
            try:
                if len(tail) == 3:
                    freq1, ms, amp = tail
                    freq2 = "0"
                elif len(tail) == 4:
                    freq1, freq2, ms, amp = tail
                else:
                    print("Usage: msg add <name> tone <freq1> [freq2] <ms> <amp>")
                    return
                elem = {"type": "tone", "freq1": float(freq1), "freq2": float(freq2),
                        "ms": int(ms), "amp": float(amp)}
            except ValueError as exc:
                print(f"Invalid tone parameters: {exc}")
                return
        else:
            print(f"Unknown element type {etype!r} — use cw, voice, or tone")
            return

        resp = conn.command({"cmd": "msg_add", "name": name, "element": elem})
        _print_response(resp)

    else:
        print(f"Unknown msg subcommand: {sub!r}")
        print("Usage: msg list|show|new|delete|clear|add ...")


def interactive_shell(conn: DaemonConnection) -> None:
    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")

    # Initial status
    try:
        resp = conn.recv_line()   # daemon sends status on connect
        if resp:
            print("Connected to daemon.  Current state:")
            print(" ", _fmt_status(resp))
    except Exception:
        pass

    print("Type 'help' for commands.\n")

    # Background subscriber thread for push events
    _subscribe_events(conn)

    while True:
        try:
            line = input("rc> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd   = parts[0].lower()
        rest  = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit"):
            break

        elif cmd == "help":
            print(__doc__)

        elif cmd == "state":
            resp = conn.command({"cmd": "state"})
            _print_response(resp)

        elif cmd == "config":
            resp = conn.command({"cmd": "config"})
            _print_response(resp)

        elif cmd == "set":
            if not rest:
                print("Usage: set <field> <value>  (e.g. 'set hang 2500')")
                continue
            resp = conn.command({"cmd": "set", "args": rest})
            _print_response(resp)

        elif cmd == "play":
            if not rest:
                print("Usage: play <message_name>")
                continue
            resp = conn.command({"cmd": "play", "msg": rest.strip()})
            _print_response(resp)

        elif cmd == "ptt":
            active = rest.strip().lower() in ("on", "true", "1", "yes")
            resp = conn.command({"cmd": "ptt", "active": active})
            _print_response(resp)

        elif cmd == "reload":
            resp = conn.command({"cmd": "reload"})
            _print_response(resp)

        elif cmd == "shutdown":
            confirm = input("Shut down the daemon? [y/N] ").strip().lower()
            if confirm == "y":
                conn.send({"cmd": "shutdown"})
                print("Shutdown sent.")
                break

        elif cmd in ("subscribe", "watch"):
            watch_mode(conn)

        elif cmd == "msg":
            _cmd_msg(conn, rest)

        else:
            print(f"Unknown command: {cmd!r}.  Type 'help' for commands.")


def _subscribe_events(conn: DaemonConnection) -> None:
    """Send subscribe command and consume the ack so the buffer stays clean."""
    try:
        conn.send({"cmd": "subscribe"})
        conn.recv_line()   # consume {"ok": true, "subscribed": true}
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Repeater controller shell")
    parser.add_argument("config", nargs="?", help="TOML config file")
    parser.add_argument("--socket", help="Override socket path")
    parser.add_argument("--watch", action="store_true",
                        help="Stream events and exit (non-interactive)")
    args = parser.parse_args()

    # Determine socket path
    socket_path = args.socket
    if not socket_path:
        if args.config and Path(args.config).exists():
            from rc_config import RepeaterConfig
            cfg = RepeaterConfig.load(args.config)
            raw = cfg.daemon.socket_path
            p   = Path(raw)
            socket_path = raw if p.is_absolute() else str(Path(args.config).parent / p)
        else:
            socket_path = str(Path.cwd() / "run/rc.sock")

    conn = DaemonConnection(socket_path)
    try:
        conn.connect()
    except FileNotFoundError:
        print(f"Error: socket not found: {socket_path}")
        print("Is the daemon running?  Start it with:  python daemon.py repeater.toml")
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: daemon not listening on {socket_path}")
        sys.exit(1)

    try:
        if args.watch:
            watch_mode(conn)
        else:
            interactive_shell(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
