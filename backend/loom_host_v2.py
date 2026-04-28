#!/usr/bin/env python3
"""
Enhanced SMIT loom host implementation.

Adds to the original version:
- SQLite persistence for raw frames and decoded events
- Minimal built-in HTTP dashboard and JSON API
- Same TCP/UDP/TS protocol handling as the original script

Usage:
    python loom_host_v2.py --config loom_host_v2_config.example.json --log-level INFO

Key HTTP endpoints:
    GET /                 -> HTML dashboard
    GET /health           -> plain-text OK
    GET /api/looms        -> JSON snapshot of current loom state
    GET /api/events       -> JSON list of recent decoded events
    GET /api/frames       -> JSON list of recent raw frames
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# -----------------------------
# Constants / protocol commands
# -----------------------------
CMD_STATUS = 0x0A              # 10 decimal
CMD_COMPLETE_STATUS = 0x0B     # 11 decimal
CMD_FULL_STATUS = 0x0C         # 12 decimal
CMD_DECLARATION = 0x14         # 20 decimal
CMD_POPUP = 0x17               # 23 decimal
CMD_LIGHTS = 0x28              # 40 decimal
CMD_SPEED = 0x32               # 50 decimal

DEFAULT_TS_PORT = 13000
DEFAULT_HOST_PORT = 13001
DEFAULT_HTTP_PORT = 18080
MAX_STR_LEN = 255
RELEASE_VERSION = "release_v2"
RELEASE_DATE = "2026-04-20"


class ProtocolError(Exception):
    pass


# -----------------------------
# Data models
# -----------------------------
@dataclass
class Frame:
    cmd: int
    length: int
    payload: bytes
    trailer: bytes = b""
    transport: str = "tcp"
    direction: str = "rx"
    peer_ip: str = ""
    peer_port: int = 0
    recv_ts: float = field(default_factory=time.time)

    def as_hex(self) -> str:
        return (bytes([self.cmd, self.length]) + self.payload + self.trailer).hex().upper()


@dataclass
class StatusEvent:
    stat1: int
    stat2: int
    stat3: int
    source: str


@dataclass
class CompleteStatusEvent:
    stat1: int
    stat2: int
    stat3: int
    speed_rpm: int
    total_picks: int
    source: str


@dataclass
class FullStatusI40Event:
    stat1: int
    stat2: int
    stat3: int
    speed_rpm: int
    total_picks: int
    shift: int
    efficiency_x100: int
    alarm_code: int
    density_weft_per_dm: int
    warp_tensions: List[int]
    meters: int
    source: str


@dataclass
class DeclarationRequest:
    code_str: str
    completed_text: Optional[str] = None


@dataclass
class LoomRuntimeState:
    name: str
    ip: str
    ts_port: int = DEFAULT_TS_PORT
    last_ts_poll_ts: float = 0.0
    last_tc_rx_ts: float = 0.0
    last_status: Optional[Dict[str, Any]] = None
    tcp_connected: bool = False
    current_peer_port: Optional[int] = None
    supports_qt5_full_status: bool = False


# -----------------------------
# Utilities
# -----------------------------
def u16(msb: int, lsb: int) -> int:
    return (msb << 8) | lsb


def decode_speed_rpm_bytepair(msb: int, lsb: int) -> float:
    """Decode loom speed from the two speed bytes used in status frames.

    Some QT5/2FAST terminals return the speed as a one-byte value followed by a
    reserved 0x00 byte. Example: C8 00 means about 200 rpm, not 512.0 rpm.
    Other terminals use a normal big-endian word or a x100 scaled word.
    """
    msb = int(msb) & 0xFF
    lsb = int(lsb) & 0xFF

    # Observed on the user's loom: [speed, 0x00].
    if lsb == 0 and 0 < msb <= 250:
        return float(msb)

    # Alternate compact form: [0x00, speed].
    if msb == 0 and 0 <= lsb <= 250:
        return float(lsb)

    raw = u16(msb, lsb)
    if raw > 10000:
        return round(raw / 100.0, 1)
    if raw > 2500:
        candidate = raw / 100.0
        if 0 <= candidate <= 2500:
            return round(candidate, 1)
    return float(raw)


def u32(b3: int, b2: int, b1: int, b0: int) -> int:
    return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_local_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ensure_ascii_nul_terminated(text: str) -> bytes:
    raw = text.encode("ascii", errors="strict") + b"\x00"
    if len(raw) > MAX_STR_LEN:
        raise ValueError(f"string too long: {len(raw)} > {MAX_STR_LEN}")
    return raw


# -----------------------------
# Parser
# -----------------------------
class LoomStreamParser:
    def __init__(self) -> None:
        self._state = "WAIT_CMD"
        self._cmd: Optional[int] = None
        self._length: Optional[int] = None
        self._payload = bytearray()

    def feed(self, data: bytes) -> List[Frame]:
        out: List[Frame] = []
        i = 0
        while i < len(data):
            if self._state == "WAIT_CMD":
                self._cmd = data[i]
                i += 1
                self._state = "WAIT_LEN"
            elif self._state == "WAIT_LEN":
                self._length = data[i]
                i += 1
                self._payload.clear()
                if self._length == 0:
                    out.append(Frame(cmd=self._cmd or 0, length=0, payload=b""))
                    self._state = "WAIT_CMD"
                else:
                    self._state = "WAIT_PAYLOAD"
            elif self._state == "WAIT_PAYLOAD":
                assert self._length is not None
                need = self._length - len(self._payload)
                take = min(need, len(data) - i)
                self._payload.extend(data[i:i + take])
                i += take
                if len(self._payload) == self._length:
                    trailer = b""
                    if self._cmd == CMD_DECLARATION and self._length == 3:
                        if i < len(data) and data[i] == 0x0A:
                            trailer = b"\x0A"
                            i += 1
                    out.append(Frame(
                        cmd=self._cmd or 0,
                        length=self._length,
                        payload=bytes(self._payload),
                        trailer=trailer,
                    ))
                    self._state = "WAIT_CMD"
            else:
                raise RuntimeError(f"invalid parser state: {self._state}")
        return out


# -----------------------------
# Encoder helpers
# -----------------------------
def encode_status_request() -> bytes:
    return bytes([CMD_STATUS, 0x00])


def encode_full_status_request() -> bytes:
    return bytes([CMD_FULL_STATUS, 0x00])


def encode_speed_request() -> bytes:
    return bytes([CMD_SPEED, 0x00])


def encode_declaration_reply(text: str) -> bytes:
    raw = ensure_ascii_nul_terminated(text)
    return bytes([CMD_DECLARATION, len(raw)]) + raw


def encode_popup_message(text: str) -> bytes:
    raw = ensure_ascii_nul_terminated(text)
    return bytes([CMD_POPUP, len(raw)]) + raw


# -----------------------------
# Decoders
# -----------------------------
def decode_status(payload: bytes, *, source: str) -> StatusEvent:
    if len(payload) < 3:
        raise ProtocolError(f"short 0x0A status payload: {len(payload)}")
    return StatusEvent(stat1=payload[0], stat2=payload[1], stat3=payload[2], source=source)


def decode_complete_status(payload: bytes, *, source: str) -> CompleteStatusEvent:
    if len(payload) != 9:
        raise ProtocolError(f"invalid 0x0B payload length: {len(payload)} != 9")
    return CompleteStatusEvent(
        stat1=payload[0],
        stat2=payload[1],
        stat3=payload[2],
        speed_rpm=decode_speed_rpm_bytepair(payload[3], payload[4]),
        total_picks=u32(payload[5], payload[6], payload[7], payload[8]),
        source=source,
    )


def decode_full_status(payload: bytes, *, source: str) -> FullStatusI40Event:
    payload_len = len(payload)
    if payload_len not in (0x1A, 0x1C):
        raise ProtocolError(f"invalid 0x0C payload length: {payload_len} (supported: 26 or 28)")

    # Two observed variants:
    # - 28-byte QT5-style frame, with 2 reserved bytes before meters
    # - 26-byte variant (seen on some looms / software families such as 5Plane),
    #   where the reserved bytes are omitted and meters starts immediately at byte 22.
    meters_offset = 24 if payload_len == 0x1C else 22

    return FullStatusI40Event(
        stat1=payload[0],
        stat2=payload[1],
        stat3=payload[2],
        speed_rpm=decode_speed_rpm_bytepair(payload[3], payload[4]),
        total_picks=u32(payload[5], payload[6], payload[7], payload[8]),
        shift=payload[9],
        efficiency_x100=u16(payload[10], payload[11]),
        alarm_code=u16(payload[12], payload[13]),
        density_weft_per_dm=u16(payload[14], payload[15]),
        warp_tensions=[u16(payload[16], payload[17]), u16(payload[18], payload[19]), u16(payload[20], payload[21])],
        meters=u32(payload[meters_offset], payload[meters_offset + 1], payload[meters_offset + 2], payload[meters_offset + 3]),
        source=source,
    )


def decode_declaration_request(frame: Frame) -> DeclarationRequest:
    if frame.length < 1:
        raise ProtocolError(f"declaration request too short: {frame.length}")

    # Some looms send a strict 3-digit request code (e.g. "565"),
    # while others send the completed declaration text back with or without
    # the 3-digit code prefix. Be permissive here so the dashboard can still
    # capture the completion.
    try:
        text = frame.payload.decode("ascii", errors="ignore")
    except Exception as exc:
        raise ProtocolError(f"declaration payload decode failed: {exc}") from exc

    text = text.rstrip("\x00\r\n")
    compact = text.strip()
    if not compact:
        raise ProtocolError("empty declaration payload")

    # Strict request form: exactly the 3-digit declaration code.
    if len(compact) == 3 and compact.isdigit():
        return DeclarationRequest(code_str=compact)

    # Completion form with a 3-digit prefix.
    if len(compact) >= 3 and compact[:3].isdigit():
        rest = compact[3:].strip()
        if rest:
            return DeclarationRequest(code_str=compact[:3], completed_text=rest)
        return DeclarationRequest(code_str=compact[:3])

    # Completion form without the code prefix; preserve the text so the
    # dashboard can still parse employee / plan / package values.
    return DeclarationRequest(code_str="", completed_text=compact)


# -----------------------------
# Human-readable status helpers
# -----------------------------
def interpret_status(event: StatusEvent | CompleteStatusEvent | FullStatusI40Event) -> Dict[str, Any]:
    stat1 = event.stat1
    stat2 = event.stat2
    stat3 = event.stat3

    running = (stat1 & 0xF8) == 0x00
    weft_stop = (stat1 & 0xF8) == 0xC0
    warp_stop = (stat1 & 0xF8) == 0xA0
    other_stop = (stat1 & 0xF8) == 0x90
    kpick_toggle = bool(stat1 & 0x04)

    result: Dict[str, Any] = {
        "raw": {"stat1": stat1, "stat2": stat2, "stat3": stat3},
        "running": running,
        "kpick_toggle": kpick_toggle,
        "category": "unknown",
        "detail": None,
    }

    if running:
        result["category"] = "running"
        return result

    if weft_stop:
        result["category"] = "weft_stop"
        mapping = {0x80: "weft_before_exchange", 0x40: "double_weft", 0x20: "weft_after_exchange"}
        result["detail"] = mapping.get(stat2, "unknown_weft_stop")
        result["weft_no"] = stat3
        return result

    if warp_stop:
        result["category"] = "warp_stop"
        mapping = {0x80: "warp_1", 0x40: "warp_2"}
        result["detail"] = mapping.get(stat2, "unknown_warp_stop")
        return result

    if other_stop:
        result["category"] = "other_stop"
        mapping = {
            0x80: "operator_stop",
            0x40: "auxiliary_halt",
            0x20: "mechanical",
            0x10: "empty_prewinder",
            0x08: "preselection",
            0x04: "beam_end",
        }
        result["detail"] = mapping.get(stat2, "unknown_other_stop")
        if stat2 == 0x10:
            result["weft_no"] = stat3
        return result

    result["category"] = "generic_stop"
    return result


# -----------------------------
# SQLite logger
# -----------------------------
class SQLiteStore:
    def __init__(self, db_path: str, logger: logging.Logger) -> None:
        self.db_path = db_path
        self.log = logger
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS raw_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                peer_ip TEXT NOT NULL,
                peer_port INTEGER NOT NULL,
                transport TEXT NOT NULL,
                direction TEXT NOT NULL,
                cmd INTEGER NOT NULL,
                length INTEGER NOT NULL,
                hex TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_frames_ts ON raw_frames(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_raw_frames_ip ON raw_frames(peer_ip, ts DESC);

            CREATE TABLE IF NOT EXISTS loom_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                peer_ip TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_loom_events_ts ON loom_events(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_loom_events_ip ON loom_events(peer_ip, ts DESC);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def log_frame(self, frame: Frame) -> None:
        self.conn.execute(
            """
            INSERT INTO raw_frames (ts, ts_iso, peer_ip, peer_port, transport, direction, cmd, length, hex)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                frame.recv_ts,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frame.recv_ts)),
                frame.peer_ip,
                frame.peer_port,
                frame.transport,
                frame.direction,
                frame.cmd,
                frame.length,
                frame.as_hex(),
            ),
        )
        self.conn.commit()

    def log_event(self, peer_ip: str, event_type: str, source: str, payload: Dict[str, Any]) -> None:
        ts = time.time()
        self.conn.execute(
            """
            INSERT INTO loom_events (ts, ts_iso, peer_ip, event_type, source, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), peer_ip, event_type, source, json.dumps(payload)),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, ts_iso, peer_ip, event_type, source, payload_json FROM loom_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "ts_iso": row["ts_iso"],
                "peer_ip": row["peer_ip"],
                "event_type": row["event_type"],
                "source": row["source"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def recent_frames(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, ts_iso, peer_ip, peer_port, transport, direction, cmd, length, hex FROM raw_frames ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


# -----------------------------
# Registry / declarations
# -----------------------------
class LoomRegistry:
    def __init__(self, loom_configs: List[Dict[str, Any]]) -> None:
        self.by_ip: Dict[str, LoomRuntimeState] = {}
        self.config_by_ip: Dict[str, Dict[str, Any]] = {}
        for entry in loom_configs:
            ip = entry["ip"]
            state = LoomRuntimeState(
                name=entry.get("name", ip),
                ip=ip,
                ts_port=int(entry.get("ts_port", DEFAULT_TS_PORT)),
                supports_qt5_full_status=bool(entry.get("supports_qt5_full_status", False)),
            )
            self.by_ip[ip] = state
            self.config_by_ip[ip] = entry

    def get_or_create_unknown(self, ip: str) -> LoomRuntimeState:
        if ip not in self.by_ip:
            self.by_ip[ip] = LoomRuntimeState(name=f"unknown-{ip}", ip=ip)
            self.config_by_ip[ip] = {"name": f"unknown-{ip}", "ip": ip, "declarations": {}}
        return self.by_ip[ip]

    def mark_tc_connected(self, ip: str, port: int) -> LoomRuntimeState:
        state = self.get_or_create_unknown(ip)
        state.tcp_connected = True
        state.current_peer_port = port
        state.last_tc_rx_ts = time.time()
        return state

    def mark_tc_disconnected(self, ip: str) -> None:
        state = self.get_or_create_unknown(ip)
        state.tcp_connected = False
        state.current_peer_port = None

    def mark_tc_rx(self, ip: str) -> None:
        self.get_or_create_unknown(ip).last_tc_rx_ts = time.time()

    def mark_ts_poll(self, ip: str) -> None:
        self.get_or_create_unknown(ip).last_ts_poll_ts = time.time()

    def set_last_status(self, ip: str, event: StatusEvent | CompleteStatusEvent | FullStatusI40Event) -> None:
        state = self.get_or_create_unknown(ip)
        state.last_status = {
            "event": asdict(event),
            "interpreted": interpret_status(event),
            "updated_at": time.time(),
            "updated_at_iso": now_iso(),
        }

    def declaration_reply_for(self, ip: str, code_str: str) -> Optional[str]:
        return self.config_by_ip.get(ip, {}).get("declarations", {}).get(code_str)

    def all_known_looms(self) -> List[LoomRuntimeState]:
        return list(self.by_ip.values())

    def snapshot(self) -> Dict[str, Any]:
        return {
            ip: {
                "name": state.name,
                "ip": state.ip,
                "ts_port": state.ts_port,
                "tcp_connected": state.tcp_connected,
                "current_peer_port": state.current_peer_port,
                "last_tc_rx_ts": state.last_tc_rx_ts,
                "last_ts_poll_ts": state.last_ts_poll_ts,
                "last_status": state.last_status,
                "supports_qt5_full_status": state.supports_qt5_full_status,
            }
            for ip, state in sorted(self.by_ip.items())
        }


# -----------------------------
# Event sink
# -----------------------------
class EventSink:
    def __init__(self, store: Optional[SQLiteStore], logger: logging.Logger) -> None:
        self.store = store
        self.log = logger

    def log_frame(self, frame: Frame) -> None:
        if self.store:
            self.store.log_frame(frame)

    def log_event(self, peer_ip: str, event_type: str, source: str, payload: Dict[str, Any]) -> None:
        if self.store:
            self.store.log_event(peer_ip, event_type, source, payload)


# -----------------------------
# TS client
# -----------------------------
class TsClient:
    def __init__(self, connect_timeout: float, reply_timeout: float, logger: logging.Logger, sink: EventSink) -> None:
        self.connect_timeout = connect_timeout
        self.reply_timeout = reply_timeout
        self.log = logger
        self.sink = sink

    async def transact(self, loom_ip: str, port: int, request: bytes) -> Frame:
        self.log.debug("TS connect %s:%s req=%s", loom_ip, port, request.hex().upper())
        reader, writer = await asyncio.wait_for(asyncio.open_connection(loom_ip, port), timeout=self.connect_timeout)
        try:
            tx_frame = Frame(
                cmd=request[0],
                length=request[1] if len(request) > 1 else 0,
                payload=request[2:],
                transport="tcp",
                direction="tx",
                peer_ip=loom_ip,
                peer_port=port,
            )
            self.sink.log_frame(tx_frame)
            writer.write(request)
            await writer.drain()

            parser = LoomStreamParser()
            deadline = time.monotonic() + self.reply_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"TS reply timeout from {loom_ip}:{port}")
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
                if not chunk:
                    raise ConnectionError(f"loom closed before complete TS reply: {loom_ip}:{port}")
                frames = parser.feed(chunk)
                if frames:
                    frame = frames[0]
                    frame.transport = "tcp"
                    frame.direction = "rx"
                    frame.peer_ip = loom_ip
                    frame.peer_port = port
                    self.sink.log_frame(frame)
                    return frame
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# -----------------------------
# TC TCP server
# -----------------------------
class TcTcpServer:
    def __init__(
        self,
        registry: LoomRegistry,
        declaration_resolver: Callable[[str, str], Optional[str]],
        logger: logging.Logger,
        sink: EventSink,
    ) -> None:
        self.registry = registry
        self.resolve_declaration = declaration_resolver
        self.log = logger
        self.sink = sink

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip, peer_port = (peer[0], peer[1]) if peer else ("unknown", 0)
        self.registry.mark_tc_connected(peer_ip, peer_port)
        self.log.info("TC TCP connected from %s:%s", peer_ip, peer_port)
        parser = LoomStreamParser()

        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    self.log.info("TC TCP EOF from %s:%s", peer_ip, peer_port)
                    break
                self.registry.mark_tc_rx(peer_ip)
                for frame in parser.feed(chunk):
                    frame.transport = "tcp"
                    frame.direction = "rx"
                    frame.peer_ip = peer_ip
                    frame.peer_port = peer_port
                    self.sink.log_frame(frame)
                    await self._handle_frame(frame, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("TC TCP session error from %s:%s", peer_ip, peer_port)
        finally:
            self.registry.mark_tc_disconnected(peer_ip)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self.log.info("TC TCP disconnected from %s:%s", peer_ip, peer_port)

    async def _handle_frame(self, frame: Frame, writer: asyncio.StreamWriter) -> None:
        ip = frame.peer_ip
        self.log.debug("TC RX %s cmd=0x%02X len=%d raw=%s", ip, frame.cmd, frame.length, frame.as_hex())

        if frame.cmd == CMD_STATUS:
            event = decode_status(frame.payload, source="TC_PUSH")
            self.registry.set_last_status(ip, event)
            self.sink.log_event(ip, "status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
            self.log.info("TC status from %s: %s", ip, interpret_status(event))
            return

        if frame.cmd == CMD_COMPLETE_STATUS:
            event = decode_complete_status(frame.payload, source="TC_PUSH")
            self.registry.set_last_status(ip, event)
            self.sink.log_event(ip, "complete_status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
            self.log.info("TC complete-status from %s: speed=%s picks=%s status=%s", ip, event.speed_rpm, event.total_picks, interpret_status(event))
            return

        if frame.cmd == CMD_DECLARATION:
            decl = decode_declaration_request(frame)
            if decl.completed_text is None:
                reply = self.resolve_declaration(ip, decl.code_str)
                payload = {"code": decl.code_str, "reply": reply, "interactive": bool(reply and "_" in reply)}
                self.sink.log_event(ip, "declaration_request", "TC_PUSH", payload)
                self.log.info("TC declaration request from %s: code=%s reply=%r", ip, decl.code_str, reply)
                if reply is not None:
                    raw = encode_declaration_reply(reply)
                    tx_frame = Frame(
                        cmd=raw[0],
                        length=raw[1],
                        payload=raw[2:],
                        transport="tcp",
                        direction="tx",
                        peer_ip=ip,
                        peer_port=frame.peer_port,
                    )
                    self.sink.log_frame(tx_frame)
                    writer.write(raw)
                    await writer.drain()
            else:
                payload = {"code": decl.code_str, "completed_text": decl.completed_text}
                self.sink.log_event(ip, "declaration_completion", "TC_PUSH", payload)
                self.log.info("TC administrative declaration completion from %s: code=%s text=%r", ip, decl.code_str, decl.completed_text)
            return

        self.sink.log_event(ip, "unhandled_frame", frame.transport, {"cmd": frame.cmd, "length": frame.length, "hex": frame.as_hex()})
        self.log.warning("Unhandled TC TCP frame from %s: cmd=0x%02X len=%d", ip, frame.cmd, frame.length)


# -----------------------------
# TC UDP listener
# -----------------------------
class TcUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, registry: LoomRegistry, logger: logging.Logger, sink: EventSink) -> None:
        self.registry = registry
        self.log = logger
        self.sink = sink

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        ip, port = addr
        try:
            frame = parse_udp_datagram(data, ip, port)
            self.registry.mark_tc_rx(ip)
            self.sink.log_frame(frame)
            self.log.debug("TC UDP RX %s:%s raw=%s", ip, port, frame.as_hex())

            if frame.cmd == CMD_COMPLETE_STATUS:
                event = decode_complete_status(frame.payload, source="TC_UDP_PUSH")
                self.registry.set_last_status(ip, event)
                self.sink.log_event(ip, "complete_status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
                self.log.info("TC UDP complete-status from %s: speed=%s picks=%s status=%s", ip, event.speed_rpm, event.total_picks, interpret_status(event))
            elif frame.cmd == CMD_STATUS:
                event = decode_status(frame.payload, source="TC_UDP_PUSH")
                self.registry.set_last_status(ip, event)
                self.sink.log_event(ip, "status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
                self.log.info("TC UDP status from %s: %s", ip, interpret_status(event))
            else:
                self.sink.log_event(ip, "unhandled_udp_frame", "TC_UDP_PUSH", {"cmd": frame.cmd, "length": frame.length, "hex": frame.as_hex()})
                self.log.warning("Unhandled TC UDP frame from %s: cmd=0x%02X len=%d", ip, frame.cmd, frame.length)
        except Exception:
            self.log.exception("TC UDP parse/handle error from %s:%s data=%s", ip, port, data.hex().upper())


def parse_udp_datagram(data: bytes, ip: str, port: int) -> Frame:
    if len(data) < 2:
        raise ProtocolError("short UDP datagram")
    cmd = data[0]
    length = data[1]
    if len(data) < 2 + length:
        raise ProtocolError(f"truncated UDP datagram: total={len(data)} need={2 + length}")
    return Frame(
        cmd=cmd,
        length=length,
        payload=data[2:2 + length],
        trailer=data[2 + length:],
        transport="udp",
        direction="rx",
        peer_ip=ip,
        peer_port=port,
    )


# -----------------------------
# Polling manager
# -----------------------------
class PollManager:
    def __init__(self, registry: LoomRegistry, ts_client: TsClient, logger: logging.Logger, sink: EventSink) -> None:
        self.registry = registry
        self.ts = ts_client
        self.log = logger
        self.sink = sink
        self._tasks: List[asyncio.Task[Any]] = []

    def start(self) -> None:
        for state in self.registry.all_known_looms():
            cfg = self.registry.config_by_ip[state.ip]
            enabled = bool(cfg.get("enabled", True))
            if not enabled:
                self.log.info("Skipping background polling for disabled loom %s", state.ip)
                continue
            status_every = float(cfg.get("poll_status_every_seconds", 5))
            full_every = float(cfg.get("poll_full_status_every_seconds", 10))
            supports_qt5 = bool(cfg.get("supports_qt5_full_status", False))
            self._tasks.append(asyncio.create_task(self._poll_status_loop(state.ip, state.ts_port, status_every)))
            if supports_qt5:
                if full_every < 6.0:
                    self.log.warning("Configured full-status poll interval for %s is %.3fs; enforcing minimum 6.0s", state.ip, full_every)
                    full_every = 6.0
                self._tasks.append(asyncio.create_task(self._poll_full_status_loop(state.ip, state.ts_port, full_every)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _poll_status_loop(self, ip: str, port: int, interval_s: float) -> None:
        await asyncio.sleep(0.2)
        while True:
            try:
                frame = await self.ts.transact(ip, port, encode_status_request())
                if frame.cmd != CMD_STATUS:
                    self.log.warning("Unexpected TS reply cmd from %s: 0x%02X", ip, frame.cmd)
                else:
                    event = decode_status(frame.payload, source="TS_REPLY")
                    self.registry.mark_ts_poll(ip)
                    self.registry.set_last_status(ip, event)
                    self.sink.log_event(ip, "status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
                    self.log.info("TS status from %s: %s", ip, interpret_status(event))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("TS status poll failed for %s:%s", ip, port)
            await asyncio.sleep(interval_s)

    async def _poll_full_status_loop(self, ip: str, port: int, interval_s: float) -> None:
        await asyncio.sleep(1.0)
        while True:
            try:
                frame = await self.ts.transact(ip, port, encode_full_status_request())
                if frame.cmd != CMD_FULL_STATUS:
                    self.log.warning("Unexpected TS full-status reply cmd from %s: 0x%02X", ip, frame.cmd)
                else:
                    event = decode_full_status(frame.payload, source="TS_REPLY")
                    self.registry.mark_ts_poll(ip)
                    self.registry.set_last_status(ip, event)
                    self.sink.log_event(ip, "full_status", event.source, {"event": asdict(event), "interpreted": interpret_status(event)})
                    self.log.info("TS full-status from %s: speed=%s picks=%s shift=%s eff=%s alarm=%s meters=%s status=%s", ip, event.speed_rpm, event.total_picks, event.shift, event.efficiency_x100, event.alarm_code, event.meters, interpret_status(event))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("TS full-status poll failed for %s:%s", ip, port)
            await asyncio.sleep(interval_s)


# -----------------------------
# HTTP dashboard
# -----------------------------
class HttpDashboardServer:
    def __init__(self, registry: LoomRegistry, sink: EventSink, store: Optional[SQLiteStore], logger: logging.Logger) -> None:
        self.registry = registry
        self.sink = sink
        self.store = store
        self.log = logger

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            first_line = req.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            method, target, _ = first_line.split(" ", 2)
            if method != "GET":
                await self._send_response(writer, 405, "text/plain; charset=utf-8", b"Method Not Allowed")
                return

            parsed = urlparse(target)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/health":
                await self._send_response(writer, 200, "text/plain; charset=utf-8", b"OK\n")
                return

            if path == "/api/looms":
                body = json.dumps(self.registry.snapshot(), indent=2, default=str).encode("utf-8")
                await self._send_response(writer, 200, "application/json; charset=utf-8", body)
                return

            if path == "/api/events":
                limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
                body = json.dumps(self.store.recent_events(limit) if self.store else [], indent=2, default=str).encode("utf-8")
                await self._send_response(writer, 200, "application/json; charset=utf-8", body)
                return

            if path == "/api/frames":
                limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
                body = json.dumps(self.store.recent_frames(limit) if self.store else [], indent=2, default=str).encode("utf-8")
                await self._send_response(writer, 200, "application/json; charset=utf-8", body)
                return

            page = self._render_dashboard_html()
            await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
        except asyncio.IncompleteReadError:
            pass
        except Exception:
            self.log.exception("HTTP dashboard request failed")
            try:
                await self._send_response(writer, 500, "text/plain; charset=utf-8", b"Internal Server Error")
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes) -> None:
        reason = {
            200: "OK",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }.get(status, "OK")
        headers = [
            f"HTTP/1.1 {status} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Cache-Control: no-store",
            "",
            "",
        ]
        writer.write("\r\n".join(headers).encode("ascii") + body)
        await writer.drain()

    def _render_dashboard_html(self) -> str:
        snapshot = self.registry.snapshot()
        rows = []
        for ip, info in snapshot.items():
            last_status = info.get("last_status") or {}
            interp = last_status.get("interpreted") or {}
            category = interp.get("category", "-")
            detail = interp.get("detail", "") or ""
            speed = (last_status.get("event") or {}).get("speed_rpm", "")
            picks = (last_status.get("event") or {}).get("total_picks", "")
            rows.append(
                f"<tr>"
                f"<td>{html.escape(str(info['name']))}</td>"
                f"<td>{html.escape(ip)}</td>"
                f"<td>{'yes' if info.get('tcp_connected') else 'no'}</td>"
                f"<td>{html.escape(str(category))}</td>"
                f"<td>{html.escape(str(detail))}</td>"
                f"<td>{html.escape(str(speed))}</td>"
                f"<td>{html.escape(str(picks))}</td>"
                f"<td>{html.escape(str(last_status.get('updated_at_iso', '')))}</td>"
                f"</tr>"
            )

        event_items = []
        if self.store:
            for ev in self.store.recent_events(20):
                event_items.append(
                    f"<li><code>{html.escape(ev['ts_iso'])}</code> "
                    f"<strong>{html.escape(ev['peer_ip'])}</strong> "
                    f"{html.escape(ev['event_type'])} "
                    f"<pre>{html.escape(json.dumps(ev['payload'], ensure_ascii=False, indent=2))}</pre></li>"
                )

        return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Loom Host Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
    th {{ background: #f3f3f3; }}
    code, pre {{ background: #f7f7f7; }}
    pre {{ padding: 8px; overflow-x: auto; }}
  </style>
</head>
<body>
  <h1>Loom Host Dashboard</h1>
  <p>Generated at {html.escape(now_iso())}</p>
  <p><a href="/api/looms">/api/looms</a> | <a href="/api/events">/api/events</a> | <a href="/api/frames">/api/frames</a> | <a href="/health">/health</a></p>
  <h2>Looms</h2>
  <table>
    <thead>
      <tr><th>Name</th><th>IP</th><th>TC TCP</th><th>Category</th><th>Detail</th><th>Speed</th><th>Picks</th><th>Updated</th></tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="8">No looms configured or seen yet.</td></tr>'}
    </tbody>
  </table>
  <h2>Recent events</h2>
  <ul>
    {''.join(event_items) if event_items else '<li>No SQLite event store enabled.</li>'}
  </ul>
</body>
</html>
"""


# -----------------------------
# High-level app
# -----------------------------
class LoomHostApp:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.log = logging.getLogger("loom_host")
        self.registry = LoomRegistry(config.get("looms", []))

        db_path = config.get("sqlite_db_path")
        self.store = SQLiteStore(db_path, self.log) if db_path else None
        self.sink = EventSink(self.store, self.log)

        self.ts_client = TsClient(
            connect_timeout=float(config.get("ts_connect_timeout_seconds", 3.0)),
            reply_timeout=float(config.get("ts_reply_timeout_seconds", 2.0)),
            logger=self.log,
            sink=self.sink,
        )
        self.tcp_server_obj = TcTcpServer(self.registry, self.resolve_declaration, self.log, self.sink)
        self.poll_manager = PollManager(self.registry, self.ts_client, self.log, self.sink)
        self.dashboard = HttpDashboardServer(self.registry, self.sink, self.store, self.log)

        self._tcp_server: Optional[asyncio.AbstractServer] = None
        self._udp_transport: Optional[asyncio.transports.DatagramTransport] = None
        self._http_server: Optional[asyncio.AbstractServer] = None
        self._stop_event = asyncio.Event()
        self.started_at = time.time()
        self.release_version = RELEASE_VERSION
        self.release_date = RELEASE_DATE
        self.log_info = dict(config.get("__log_info") or {})
        self.startup_status: Dict[str, Any] = {"tcp": None, "udp": None, "http": None, "started_at": now_local_iso()}

    def resolve_declaration(self, ip: str, code_str: str) -> Optional[str]:
        return self.registry.declaration_reply_for(ip, code_str)

    async def start(self) -> None:
        host = self.config.get("listen_host", "0.0.0.0")
        port = int(self.config.get("host_port", DEFAULT_HOST_PORT))
        self._tcp_server = await asyncio.start_server(self.tcp_server_obj.handle_client, host=host, port=port, reuse_address=True)
        self.log.info("TC TCP server listening on %s", ", ".join(str(s.getsockname()) for s in (self._tcp_server.sockets or [])))

        if bool(self.config.get("enable_udp_listener", False)):
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(lambda: TcUdpProtocol(self.registry, self.log, self.sink), local_addr=(host, port), family=socket.AF_INET)
            self._udp_transport = transport
            self.log.info("TC UDP listener enabled on %s:%s", host, port)

        if bool(self.config.get("enable_http_dashboard", True)):
            http_host = self.config.get("http_host", host)
            http_port = int(self.config.get("http_port", DEFAULT_HTTP_PORT))
            self._http_server = await asyncio.start_server(self.dashboard.handle_client, host=http_host, port=http_port, reuse_address=True)
            self.log.info("HTTP dashboard listening on %s", ", ".join(str(s.getsockname()) for s in (self._http_server.sockets or [])))

        self.poll_manager.start()

    async def stop(self) -> None:
        self._stop_event.set()
        await self.poll_manager.stop()
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()
            self._http_server = None
        if self.store is not None:
            self.store.close()
        self.log.info("LoomHostApp stopped")

    async def run_until_stopped(self) -> None:
        await self.start()
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def send_popup(self, loom_ip: str, message: str) -> None:
        port = int(self.registry.config_by_ip.get(loom_ip, {}).get("ts_port", DEFAULT_TS_PORT))
        frame = await self.ts_client.transact(loom_ip, port, encode_popup_message(message))
        self.log.info("Popup transaction completed to %s, reply cmd=0x%02X len=%d", loom_ip, frame.cmd, frame.length)

    async def print_snapshot_loop(self, every_seconds: float = 15.0) -> None:
        while True:
            await asyncio.sleep(every_seconds)
            self.log.info("SNAPSHOT %s", json.dumps(self.registry.snapshot(), default=str))


# -----------------------------
# Config / CLI
# -----------------------------
def default_config() -> Dict[str, Any]:
    return {
        "listen_host": "0.0.0.0",
        "host_port": DEFAULT_HOST_PORT,
        "enable_udp_listener": True,
        "ts_reply_timeout_seconds": 2.0,
        "ts_connect_timeout_seconds": 3.0,
        "enable_http_dashboard": True,
        "http_host": "127.0.0.1",
        "http_port": DEFAULT_HTTP_PORT,
        "sqlite_db_path": "loom_events.db",
        "log_dir": "logs",
        "write_actions_enabled": True,
        "looms": [],
    }


def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return default_config()
    with Path(path).open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    base = default_config()
    base.update(cfg)
    for item in base.get("looms", []):
        item.setdefault("enabled", True)
        item.setdefault("poll_status_every_seconds", 5)
        item.setdefault("poll_full_status_every_seconds", 10)
    return base


def setup_logging(level_name: str, log_dir: str = "logs") -> Dict[str, str]:
    level = getattr(logging, level_name.upper(), logging.INFO)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    app_log = str((Path(log_dir) / f"loom_host_{stamp}.log").resolve())
    err_log = str((Path(log_dir) / f"loom_host_error_{stamp}.log").resolve())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_all = RotatingFileHandler(app_log, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_all.setLevel(level)
    file_all.setFormatter(formatter)
    root.addHandler(file_all)

    file_err = RotatingFileHandler(err_log, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_err.setLevel(logging.WARNING)
    file_err.setFormatter(formatter)
    root.addHandler(file_err)

    return {"log_dir": str(Path(log_dir).resolve()), "app_log": app_log, "error_log": err_log}


async def async_main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    log_info = setup_logging(args.log_level, config.get("log_dir", "logs"))
    config["__log_info"] = log_info
    app = LoomHostApp(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    background_tasks: List[asyncio.Task[Any]] = [asyncio.create_task(app.print_snapshot_loop(args.snapshot_interval))]
    try:
        await app.start()
        if args.send_popup:
            loom_ip, message = args.send_popup
            await app.send_popup(loom_ip, message)
        await app._stop_event.wait()
        return 0
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await app.stop()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enhanced SMIT loom host-side TCP/UDP protocol handler")
    parser.add_argument("--config", help="Path to JSON config file", default=None)
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--snapshot-interval", type=float, default=15.0, help="How often to print a state snapshot to the log")
    parser.add_argument("--send-popup", nargs=2, metavar=("LOOM_IP", "MESSAGE"), help="Immediately send a popup to a loom over TS after startup")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
